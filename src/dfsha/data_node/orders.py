"""Ejecucion de las ordenes que el ControlNode empuja por el stream de heartbeat.

Dos ordenes: copiar un bloque de otro DataNode (`ReplicateBlock`) y borrar un huerfano
(`DeleteBlock`).

## El destino TIRA, no el origen empuja

La orden le llega a quien tiene que hacer el trabajo, y lo hace descargando el bloque del
origen con el mismo `GET /blocks/{id}` que usa cualquier cliente. Tres consecuencias que
lo justifican:

- **El que decide si puede es el que sabe.** Solo este nodo conoce su disco; si no le
  cabe, falla aqui y la tarea vuelve a la cola para otro destino.
- **No se carga de escrituras a un nodo que quiza esta sirviendo lecturas.** El origen
  solo ve una descarga mas, que es lo que ya sabe hacer.
- **Un camino menos.** No hay endpoint nuevo en el origen: se reutiliza la lectura.

## Las ordenes se ejecutan FUERA del hilo del heartbeat

Copiar un bloque de 64 MB tarda; hacerlo en el hilo que atiende el stream dejaria de
mandar latidos mientras dura, y el ControlNode daria el nodo por SUSPECT y luego por
DEAD. Un nodo ocupado copiando parecerian un nodo muerto, y la respuesta a eso seria
programar todavia mas copias. Por eso hay un pool propio, y acotado: sin tope, veinte
ordenes a la vez saturarian el disco del nodo que precisamente se acaba de ofrecer como
destino.

## La confirmacion NO viaja de vuelta por el stream

Cuando la copia termina, el bloque se anuncia en el siguiente block report, que es el
mismo camino por el que el ControlNode se entera de cualquier otro bloque. El
`notify_stored` sincrono tambien lo cubre. Una orden que falla simplemente no se
confirma: la tarea vence en el ControlNode y se vuelve a despachar.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import httpx

from dfsha.common.checksum import CHUNK_SIZE, Sha256Accumulator, checksum_matches
from dfsha.common.errors import BlockAlreadyExistsError, ChecksumMismatchError
from dfsha.common.logging import get_logger, timed

__all__ = ["OrderExecutor"]

CHECKSUM_HEADER = "X-DFSha-Checksum"


class OrderExecutor:
    """Ejecuta ordenes del plano de control en hilos propios.

    `app_state` se pasa entero y no pieza a pieza porque el DataNode construye su estado
    en el arranque y este objeto vive desde antes: guardar referencias sueltas obligaria
    a un orden de construccion fragil.
    """

    def __init__(self, app_state, max_workers: int = 2, timeout: float = 300.0) -> None:
        self._state = app_state
        self._pool = ThreadPoolExecutor(
            max_workers=max(1, max_workers), thread_name_prefix="dfsha-orders"
        )
        self._timeout = timeout
        self._log = get_logger("data_node")
        #: Tareas ya vistas, para no repetir trabajo si el ControlNode reenvia la orden
        #: antes de enterarse de que ya esta hecha.
        self._en_curso: set[str] = set()
        self._candado = threading.Lock()
        #: Contadores, para que las pruebas no tengan que leer logs.
        self.replicated = 0
        self.deleted = 0
        self.failed = 0

    # --- Entrada -----------------------------------------------------------

    def submit_replicate(self, orden) -> bool:
        """Encola una copia. `False` si ya estaba en marcha."""
        if not self._reservar(orden.task_id):
            return False
        self._pool.submit(self._replicar, orden)
        return True

    def submit_delete(self, orden) -> bool:
        if not self._reservar(orden.task_id):
            return False
        self._pool.submit(self._borrar, orden)
        return True

    def _reservar(self, task_id: str) -> bool:
        with self._candado:
            if task_id in self._en_curso:
                return False
            self._en_curso.add(task_id)
            return True

    def _liberar(self, task_id: str) -> None:
        with self._candado:
            self._en_curso.discard(task_id)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    # --- Ejecucion ---------------------------------------------------------

    def _replicar(self, orden) -> None:
        storage = self._state.storage
        control = self._state.control
        data_node_id = self._state.data_node_id

        try:
            if storage.exists(orden.block_id):
                # Ya lo tenemos. Pasa si la orden se repitio antes de que el ControlNode
                # viera la confirmacion. Se avisa igualmente, que es lo que cierra la
                # tarea alli.
                self._confirmar(orden.block_id, data_node_id)
                return

            with timed(
                "rereplication.pull",
                logger=self._log,
                task_id=orden.task_id,
                block_id=orden.block_id,
                source=orden.source_node_id,
                size_bytes=orden.size,
            ):
                datos, calculado = self._descargar(orden)

            # Se verifica contra el checksum del METADATO, no contra el que anuncie el
            # origen: si el origen tuviera el bloque corrupto, copiarlo sin comprobar
            # convertiria una copia mala en dos.
            if orden.checksum_sha256 and not checksum_matches(
                orden.checksum_sha256, calculado
            ):
                raise ChecksumMismatchError(
                    "el bloque copiado no coincide con el checksum del metadato",
                    block_id=orden.block_id,
                    expected=orden.checksum_sha256,
                    actual=calculado,
                )

            with self._state.load.write() as escritura:
                meta = storage.write(
                    orden.block_id, iter([datos]), orden.checksum_sha256 or calculado
                )
                escritura.size = meta.size

            control.notify_stored(
                orden.block_id, data_node_id, meta.size, meta.checksum_sha256
            )
            self._state.changes.block_added(orden.block_id)
            self.replicated += 1
            self._log.info(
                "rereplication.completed",
                task_id=orden.task_id,
                block_id=orden.block_id,
                data_node_id=data_node_id,
                source=orden.source_node_id,
                size_bytes=meta.size,
                duration_ms=0.0,
            )

        except BlockAlreadyExistsError:
            # Carrera con otra copia del mismo bloque. No es un fallo: el bloque esta.
            self._confirmar(orden.block_id, data_node_id)
        except Exception as exc:
            self.failed += 1
            self._log.error(
                "rereplication.failed",
                task_id=orden.task_id,
                block_id=orden.block_id,
                source=orden.source_node_id,
                error=type(exc).__name__,
                error_detail=str(exc),
                duration_ms=0.0,
                detail="la tarea vencera en el ControlNode y se despachara de nuevo",
            )
        finally:
            self._liberar(orden.task_id)

    def _descargar(self, orden) -> tuple[bytes, str]:
        """Descarga el bloque del origen y devuelve (bytes, sha256).

        El checksum sale del mismo recorrido de los bytes, sin una segunda pasada: es
        para lo que existe el acumulador.
        """
        url = f"{orden.source_base_url.rstrip('/')}/api/v1/blocks/{orden.block_id}"
        acumulador = Sha256Accumulator()
        trozos: list[bytes] = []

        with httpx.stream("GET", url, timeout=self._timeout) as respuesta:
            if respuesta.status_code != 200:
                respuesta.read()
                raise RuntimeError(
                    f"el origen devolvio {respuesta.status_code} para {orden.block_id}"
                )
            for trozo in respuesta.iter_bytes(CHUNK_SIZE):
                acumulador.update(trozo)
                trozos.append(trozo)

        return b"".join(trozos), acumulador.hexdigest

    def _borrar(self, orden) -> None:
        try:
            if self._state.storage.delete(orden.block_id):
                self._state.changes.block_removed(orden.block_id)
                self.deleted += 1
            # Borrar lo que ya no esta tambien es exito: el GC puede reintentar una
            # pasada a medias sin que falle entera.
            self._log.info(
                "block.deleted_by_order",
                task_id=orden.task_id,
                block_id=orden.block_id,
                reason=orden.reason,
            )
        except Exception as exc:
            self.failed += 1
            self._log.error(
                "block.delete_failed",
                task_id=orden.task_id,
                block_id=orden.block_id,
                error=type(exc).__name__,
            )
        finally:
            self._liberar(orden.task_id)

    def _confirmar(self, block_id: str, data_node_id: str) -> None:
        """Vuelve a avisar de un bloque que ya teniamos, para que se cierre la tarea."""
        try:
            meta = self._state.storage.read_meta(block_id)
            self._state.control.notify_stored(
                block_id, data_node_id, meta.size, meta.checksum_sha256
            )
        except Exception as exc:  # pragma: no cover - el block report lo cubre igual
            self._log.warning(
                "rereplication.reconfirm_failed",
                block_id=block_id,
                error=type(exc).__name__,
            )
