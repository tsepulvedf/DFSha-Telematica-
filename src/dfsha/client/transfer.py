"""Transferencia de bloques entre el cliente y los DataNodes.

Los bytes van directos: el ControlNode entrega un plan con la URL de cada DataNode y no
vuelve a aparecer hasta el commit. Esa es la parte "hibrida" del routing, y es lo que
hace que anadir DataNodes en la Etapa 2 sume ancho de banda en vez de saturar un nodo
central.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

import httpx

from dfsha.common.checksum import CHUNK_SIZE, Sha256Accumulator, checksum_matches
from dfsha.common.dto import BlockReadPlan, BlockWritePlan
from dfsha.common.errors import ChecksumMismatchError, DFShaError, StorageError
from dfsha.common.logging import get_logger, timed

from .chunker import checksum_block, read_block

__all__ = [
    "CHECKSUM_HEADER",
    "PIPELINE_HEADER",
    "ACKED_HEADER",
    "upload_blocks",
    "download_blocks",
    "TransferError",
]

CHECKSUM_HEADER = "X-DFSha-Checksum"
#: El RESTO de la cadena de replicas, por orden. El cliente sube a la primera y esa
#: reenvia a la siguiente: los bytes salen del cliente UNA sola vez.
PIPELINE_HEADER = "X-DFSha-Pipeline"
#: Cuantas replicas confirmo la cadena. Informativo: quien decide el quorum es el
#: ControlNode en el `commit`, que recibe un aviso de cada nodo por separado.
ACKED_HEADER = "X-DFSha-Replicas-Acked"


class TransferError(DFShaError):
    code = "transfer_failed"


@dataclass(frozen=True, slots=True)
class _Slot:
    """Un bloque y donde va o de donde viene, con su posicion en el archivo local."""

    block_id: str
    index: int
    size: int
    offset: int
    base_url: str
    data_node_id: str
    checksum: str | None = None
    #: URL de las replicas 2..R, por orden. Vacia con R=1.
    pipeline: tuple[str, ...] = ()


def _slots_de_escritura(blocks: Sequence[BlockWritePlan]) -> list[_Slot]:
    slots: list[_Slot] = []
    offset = 0
    for bloque in sorted(blocks, key=lambda b: b.index):
        if not bloque.replicas:
            raise TransferError(
                "el plan no trae ningun DataNode para el bloque", block_id=bloque.block_id
            )
        # La primera replica recibe los bytes del cliente; las demas viajan en la
        # cabecera de pipeline. El orden del plan se respeta tal cual: lo decidio la
        # politica de colocacion del ControlNode, que es quien conoce la carga y los
        # dominios de falla.
        #
        # La cadena se copia de `bloque.pipeline`, NO se deduce de `replicas`. Y esa
        # diferencia costo un fallo entero: `replicas` lleva las direcciones alcanzables
        # por el CLIENTE, que en contenedores son `localhost:800N` y desde dentro de un
        # DataNode resuelven al propio nodo. Ver "Dos direcciones por nodo" en CLAUDE.md.
        replica = bloque.replicas[0]
        slots.append(
            _Slot(
                block_id=bloque.block_id,
                index=bloque.index,
                size=bloque.size,
                offset=offset,
                base_url=replica.base_url,
                data_node_id=replica.data_node_id,
                pipeline=tuple(getattr(bloque, "pipeline", ()) or ()),
            )
        )
        offset += bloque.size
    return slots


def upload_blocks(
    local_path: str | Path,
    blocks: Sequence[BlockWritePlan],
    parallel: int = 4,
    timeout: float = 300.0,
    on_block: Callable[[int], None] | None = None,
) -> int:
    """Sube todos los bloques y devuelve los bytes enviados.

    Cada bloque viaja a su DataNode con el checksum en una cabecera; el DataNode lo
    verifica y rechaza con 422 si no cuadra, de modo que una corrupcion en transito nunca
    llega a confirmarse como un archivo bueno.

    Con R>1 los bytes **siguen saliendo una sola vez** del cliente: van a la primera
    replica del plan con las demas en `X-DFSha-Pipeline`, y la cadena se encarga del
    resto. Subir R veces desde aqui multiplicaria por R el tiempo de un `put` y el ancho
    de banda de subida, que es el recurso mas escaso del lado del cliente.
    """
    slots = _slots_de_escritura(blocks)
    if not slots:
        return 0

    log = get_logger("client")
    enviados = 0

    def subir(slot: _Slot) -> int:
        checksum = checksum_block(local_path, slot.offset, slot.size)
        cabeceras = {
            CHECKSUM_HEADER: checksum,
            "Content-Type": "application/octet-stream",
            "Content-Length": str(slot.size),
        }
        if slot.pipeline:
            cabeceras[PIPELINE_HEADER] = ",".join(slot.pipeline)

        with timed(
            "block.upload",
            logger=log,
            block_id=slot.block_id,
            size_bytes=slot.size,
            data_node_id=slot.data_node_id,
            replicas_planned=1 + len(slot.pipeline),
        ) as t:
            respuesta = httpx.put(
                f"{slot.base_url.rstrip('/')}/api/v1/blocks/{slot.block_id}",
                content=read_block(local_path, slot.offset, slot.size),
                headers=cabeceras,
                timeout=timeout,
            )
            if respuesta.status_code == 201:
                t.bind(replicas_acked=_acked(respuesta))

        if respuesta.status_code != 201:
            raise TransferError(
                "el DataNode rechazo el bloque",
                block_id=slot.block_id,
                status=respuesta.status_code,
                detail=respuesta.text[:200],
            )

        # Menos replicas de las planificadas NO es un fallo de la subida: con W=2 el
        # commit pasa igual y la copia que falta la recupera la re-replicacion. Se avisa
        # porque es lo que explica un `stat` que sale UNDER_REPLICATED despues.
        confirmadas = _acked(respuesta)
        if confirmadas < 1 + len(slot.pipeline):
            log.warning(
                "replication.partial",
                block_id=slot.block_id,
                planned=1 + len(slot.pipeline),
                acked=confirmadas,
                detail=(
                    "la cadena no confirmo todas las replicas; el commit decidira si "
                    "hay quorum y la re-replicacion recuperara el resto"
                ),
            )
        return slot.size

    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        futuros = {pool.submit(subir, slot): slot for slot in slots}
        for futuro in as_completed(futuros):
            enviados += futuro.result()
            if on_block:
                on_block(futuros[futuro].size)

    return enviados


def _acked(respuesta: httpx.Response) -> int:
    """Replicas que confirmo la cadena. Ante una cabecera ausente o rara, 1.

    Un 1 es lo unico seguro: respondio 201, o sea que el primer nodo tiene el bloque.
    Contar de menos provoca como mucho un aviso de mas; contar de mas haria creer al
    cliente que hay un quorum que no existe.
    """
    valor = respuesta.headers.get(ACKED_HEADER)
    if not valor:
        return 1
    try:
        return max(1, int(valor))
    except ValueError:
        return 1


def download_blocks(
    destination: str | Path,
    blocks: Sequence[BlockReadPlan],
    parallel: int = 4,
    timeout: float = 300.0,
    on_block: Callable[[int], None] | None = None,
    chunk_size: int = CHUNK_SIZE,
) -> int:
    """Descarga los bloques y reconstruye el archivo.

    Los bloques se piden en paralelo pero se escriben cada uno en su offset, asi que el
    archivo queda en orden de `index` sin necesidad de juntarlos al final. Cada bloque se
    verifica contra el checksum del metadato antes de darlo por bueno: si un DataNode
    devolviera bytes corruptos, el `get` falla en vez de escribir basura.
    """
    ordenados = sorted(blocks, key=lambda b: b.index)
    destino = Path(destination)
    destino.parent.mkdir(parents=True, exist_ok=True)

    total = sum(b.size for b in ordenados)
    # Se reserva el archivo completo por adelantado para poder escribir cada bloque en su
    # sitio desde varios hilos.
    with open(destino, "wb") as fh:
        fh.truncate(total)

    if not ordenados:
        return 0

    log = get_logger("client")
    offsets: dict[str, int] = {}
    acumulado = 0
    for bloque in ordenados:
        offsets[bloque.block_id] = acumulado
        acumulado += bloque.size

    def bajar(bloque: BlockReadPlan) -> int:
        """Descarga un bloque probando sus replicas por orden hasta que una responde.

        Con R=3 esto es lo que convierte "hay tres copias" en "el archivo se sigue
        leyendo": sin reintento en otra replica, perder el primer nodo del plan haria
        fallar el `get` aunque los otros dos tuvieran los bytes intactos, y las tres
        copias solo servirian para ocupar disco.

        Una replica que devuelve bytes CORRUPTOS tambien se descarta y se pasa a la
        siguiente, en vez de abandonar: es exactamente el caso para el que existe tener
        mas de una copia. Solo se falla cuando se acabaron todas.
        """
        if not bloque.replicas:
            raise TransferError(
                "el plan no trae ninguna replica para el bloque", block_id=bloque.block_id
            )

        offset = offsets[bloque.block_id]
        fallos: list[str] = []

        for intento, replica in enumerate(bloque.replicas, start=1):
            try:
                return _bajar_de(
                    bloque, replica, offset, destino, timeout, chunk_size, log
                )
            except (TransferError, ChecksumMismatchError, StorageError, httpx.HTTPError) as exc:
                motivo = getattr(exc, "message", str(exc))
                fallos.append(f"{replica.data_node_id[:8]} ({replica.base_url}): {motivo}")
                log.warning(
                    "block.replica_failed",
                    block_id=bloque.block_id,
                    data_node_id=replica.data_node_id,
                    attempt=intento,
                    replicas=len(bloque.replicas),
                    error=type(exc).__name__,
                    detail=(
                        "se prueba la siguiente replica"
                        if intento < len(bloque.replicas)
                        else "no quedan replicas"
                    ),
                )

        raise TransferError(
            f"ninguna de las {len(bloque.replicas)} replicas pudo servir el bloque",
            block_id=bloque.block_id,
            failures=fallos,
        )

    recibidos = 0
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        futuros = {pool.submit(bajar, bloque): bloque for bloque in ordenados}
        for futuro in as_completed(futuros):
            recibidos += futuro.result()
            if on_block:
                on_block(futuros[futuro].size)

    return recibidos


def _bajar_de(
    bloque: BlockReadPlan,
    replica,
    offset: int,
    destino: Path,
    timeout: float,
    chunk_size: int,
    log,
) -> int:
    """Un intento contra UNA replica. Lanza si no sirve; el que reintenta es `bajar`."""
    acumulador = Sha256Accumulator()

    with timed(
        "block.download",
        logger=log,
        block_id=bloque.block_id,
        size_bytes=bloque.size,
        data_node_id=replica.data_node_id,
    ):
        with httpx.stream(
            "GET",
            f"{replica.base_url.rstrip('/')}/api/v1/blocks/{bloque.block_id}",
            timeout=timeout,
        ) as respuesta:
            if respuesta.status_code != 200:
                respuesta.read()
                raise TransferError(
                    "el DataNode no devolvio el bloque",
                    block_id=bloque.block_id,
                    status=respuesta.status_code,
                )
            with open(destino, "r+b") as fh:
                fh.seek(offset)
                for trozo in respuesta.iter_bytes(chunk_size):
                    acumulador.update(trozo)
                    fh.write(trozo)

    if not checksum_matches(bloque.checksum_sha256, acumulador.hexdigest):
        raise ChecksumMismatchError(
            "el bloque descargado no coincide con su checksum",
            block_id=bloque.block_id,
            expected=bloque.checksum_sha256,
            actual=acumulador.hexdigest,
        )
    if acumulador.size != bloque.size:
        raise StorageError(
            "el bloque descargado no tiene el tamano esperado",
            block_id=bloque.block_id,
            expected=bloque.size,
            actual=acumulador.size,
        )
    return acumulador.size


def iter_sizes(blocks: Iterable) -> int:
    return sum(b.size for b in blocks)
