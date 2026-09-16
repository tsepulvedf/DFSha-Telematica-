"""Cliente gRPC del DataNode hacia el plano de control.

Tres responsabilidades: registrarse al arrancar, latir cada 3 s, y mandar un block report
completo cada N latidos o cuando el ControlNode lo pida.

El bucle vive en un hilo propio y no puede tumbar el DataNode: si el ControlNode se cae,
el nodo sigue sirviendo bloques a los clientes que ya tienen su plan, y reintenta la
conexion hasta que vuelve. Un DataNode que dejara de servir por no poder hablar con el
ControlNode convertiria una caida del plano de control en una caida del plano de datos.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator

import grpc

from dfsha.common.logging import get_logger
from dfsha.common.proto.gen import control_pb2, control_pb2_grpc

__all__ = ["BlockChangeLog", "HeartbeatClient", "RegistrationInfo"]


@dataclass
class BlockChangeLog:
    """Lo que cambio en el disco desde el ultimo latido.

    El DataNode no recorre el disco en cada heartbeat: los handlers de `PUT` y `DELETE`
    anotan aqui, y el latido se lleva la lista y la vacia. Recorrer el disco cada 3 s
    seria caro con muchos bloques, y el report completo periodico ya cubre lo que se
    escape por otra via (un `.blk` borrado a mano, por ejemplo).
    """

    _added: set[str] = field(default_factory=set)
    _removed: set[str] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def block_added(self, block_id: str) -> None:
        with self._lock:
            self._removed.discard(block_id)
            self._added.add(block_id)

    def block_removed(self, block_id: str) -> None:
        with self._lock:
            self._added.discard(block_id)
            self._removed.add(block_id)

    def drain(self) -> tuple[list[str], list[str]]:
        """Devuelve lo acumulado y lo limpia, de forma atomica."""
        with self._lock:
            anadidos = sorted(self._added)
            quitados = sorted(self._removed)
            self._added.clear()
            self._removed.clear()
        return anadidos, quitados

    def restore(self, added: list[str], removed: list[str]) -> None:
        """Devuelve al pendiente lo que no se pudo entregar.

        Si el latido que llevaba estos cambios fallo, perderlos dejaria al ControlNode
        creyendo que unos bloques no existen hasta el proximo report completo.
        """
        with self._lock:
            self._added.update(added)
            self._removed.update(removed)


@dataclass(frozen=True, slots=True)
class RegistrationInfo:
    data_node_id: str
    heartbeat_interval_ms: int
    full_report_every_n: int


class HeartbeatClient:
    def __init__(
        self,
        grpc_url: str,
        advertise_url: str,
        fault_domain: str,
        boot_id: str,
        capacity_bytes: int,
        stats_provider: Callable[[], control_pb2.NodeStats],
        block_ids_provider: Callable[[], list[str]],
        changes: BlockChangeLog,
        data_node_id: str = "",
        retry_seconds: float = 2.0,
        orders=None,
        peer_url: str = "",
    ) -> None:
        self._grpc_url = grpc_url
        self._advertise_url = advertise_url
        #: Con que direccion se anuncia a sus PARES. Vacia = la misma del cliente.
        self._peer_url = peer_url
        self._fault_domain = fault_domain
        self._boot_id = boot_id
        self._capacity_bytes = capacity_bytes
        self._stats_provider = stats_provider
        self._block_ids_provider = block_ids_provider
        self._changes = changes
        self._retry_seconds = retry_seconds
        #: Ejecutor de las ordenes del plano de control (Etapa 3). Opcional: sin el, el
        #: nodo late igual y simplemente ignora las ordenes, que es lo que hace falta en
        #: las pruebas que solo miran el heartbeat.
        self._orders = orders

        self.data_node_id = data_node_id
        self.heartbeat_interval_ms = 3000
        self.full_report_every_n = 20

        self._channel: grpc.Channel | None = None
        self._stub: control_pb2_grpc.ControlPlaneStub | None = None
        self._parar = threading.Event()
        self._hilo: threading.Thread | None = None
        self._sequence = 0
        self._log = get_logger("data_node")
        #: Para poder comprobar en las pruebas que el bucle sigue vivo tras un fallo.
        self.heartbeats_sent = 0
        self.reconnects = 0
        self.full_reports_sent = 0
        self._acks = 0

    # --- Conexion ----------------------------------------------------------

    def connect(self) -> None:
        if self._channel is None:
            self._channel = grpc.insecure_channel(self._grpc_url)
            self._stub = control_pb2_grpc.ControlPlaneStub(self._channel)

    def close(self) -> None:
        if self._channel is not None:
            self._channel.close()
            self._channel = None
            self._stub = None

    def register(self, max_attempts: int = 30) -> RegistrationInfo:
        """Se registra reintentando: en compose el DataNode suele arrancar antes de que
        el ControlNode acepte conexiones, y eso no es un error."""
        self.connect()
        assert self._stub is not None

        ultimo: Exception | None = None
        for intento in range(1, max_attempts + 1):
            try:
                respuesta = self._stub.Register(
                    control_pb2.RegisterRequest(
                        advertise_url=self._advertise_url,
                        peer_url=self._peer_url,
                        fault_domain=self._fault_domain,
                        boot_id=self._boot_id,
                        capacity_bytes=self._capacity_bytes,
                        data_node_id=self.data_node_id,
                    ),
                    timeout=10,
                )
            except grpc.RpcError as exc:
                ultimo = exc
                self._log.warning(
                    "datanode.register_retry",
                    attempt=intento,
                    max_attempts=max_attempts,
                    grpc_url=self._grpc_url,
                    code=exc.code().name if exc.code() else "UNKNOWN",
                )
                if intento < max_attempts:
                    time.sleep(self._retry_seconds)
                continue

            self.data_node_id = respuesta.data_node_id
            self.heartbeat_interval_ms = respuesta.heartbeat_interval_ms or 3000
            self.full_report_every_n = respuesta.full_report_every_n or 20
            self._log.info(
                "datanode.registered",
                data_node_id=self.data_node_id,
                advertise_url=self._advertise_url,
                fault_domain=self._fault_domain,
                boot_id=self._boot_id,
                attempts=intento,
            )
            return RegistrationInfo(
                data_node_id=self.data_node_id,
                heartbeat_interval_ms=self.heartbeat_interval_ms,
                full_report_every_n=self.full_report_every_n,
            )

        raise RuntimeError(
            f"no se pudo registrar contra {self._grpc_url} tras {max_attempts} intentos: {ultimo}"
        )

    # --- Block report ------------------------------------------------------

    def send_full_report(self, reason: str = "periodico") -> control_pb2.BlockReportResponse:
        """Manda todo lo que hay en disco. Es lo que cierra la divergencia: un `.blk`
        borrado por fuera solo se detecta aqui, porque los incrementales no lo mencionan."""
        self.connect()
        assert self._stub is not None

        bloques = [control_pb2.BlockInfo(block_id=b) for b in self._block_ids_provider()]
        respuesta = self._stub.BlockReport(
            control_pb2.BlockReportRequest(
                data_node_id=self.data_node_id,
                boot_id=self._boot_id,
                is_full=True,
                blocks=bloques,
            ),
            timeout=30,
        )
        self.full_reports_sent += 1
        self._log.info(
            "block_report.sent",
            data_node_id=self.data_node_id,
            blocks=len(bloques),
            reason=reason,
            unknown_blocks=respuesta.unknown_blocks,
            missing_blocks=respuesta.missing_blocks,
        )
        return respuesta

    # --- Bucle de latidos --------------------------------------------------

    def _peticiones(self) -> Iterator[control_pb2.HeartbeatRequest]:
        intervalo = self.heartbeat_interval_ms / 1000
        while not self._parar.is_set():
            anadidos, quitados = self._changes.drain()
            self._sequence += 1
            try:
                yield control_pb2.HeartbeatRequest(
                    data_node_id=self.data_node_id,
                    boot_id=self._boot_id,
                    sequence=self._sequence,
                    stats=self._stats_provider(),
                    added_block_ids=anadidos,
                    removed_block_ids=quitados,
                )
            except GeneratorExit:
                # El stream se corto con cambios ya sacados de la cola: se devuelven
                # para que el siguiente latido los lleve.
                self._changes.restore(anadidos, quitados)
                raise
            self.heartbeats_sent += 1
            self._parar.wait(intervalo)

    def _sesion(self) -> None:
        """Un stream completo, hasta que se corta."""
        self.connect()
        assert self._stub is not None

        for mensaje in self._stub.Heartbeat(self._peticiones()):
            if mensaje.HasField("ack"):
                # El contador es del cliente, no de la sesion. Si se reiniciara con cada
                # stream, un nodo que reconecta cada pocos segundos no llegaria nunca al
                # latido N y no mandaria un report completo jamas. El ControlNode pide
                # uno al reconectar, y este contador cubre el caso contrario: un stream
                # largo y estable.
                self._acks += 1
                if self.full_report_every_n and self._acks % self.full_report_every_n == 0:
                    self.send_full_report(reason="cada N latidos")
            elif mensaje.HasField("full_report"):
                self.send_full_report(reason=mensaje.full_report.reason or "peticion")
            elif mensaje.HasField("replicate_block"):
                # Se encola y se sigue. Copiar aqui mismo dejaria de mandar latidos
                # mientras dura la copia, y el ControlNode daria por muerto justo al
                # nodo que esta haciendo el trabajo. Ver el docstring de orders.py.
                self._despachar_orden(mensaje.replicate_block, replicar=True)
            elif mensaje.HasField("delete_block"):
                self._despachar_orden(mensaje.delete_block, replicar=False)

            if self._parar.is_set():
                return

    def _despachar_orden(self, orden, replicar: bool) -> None:
        """Entrega la orden al ejecutor sin bloquear el stream.

        Nunca lanza: una orden que no se puede encolar no puede cortar el heartbeat. El
        ControlNode la volvera a mandar cuando venza la tarea.
        """
        if self._orders is None:
            self._log.debug(
                "control.order_ignored",
                block_id=orden.block_id,
                detail="este nodo no tiene ejecutor de ordenes",
            )
            return
        try:
            if replicar:
                self._orders.submit_replicate(orden)
            else:
                self._orders.submit_delete(orden)
        except Exception as exc:
            self._log.error(
                "control.order_dispatch_failed",
                block_id=orden.block_id,
                error=type(exc).__name__,
            )

    def _bucle(self) -> None:
        while not self._parar.is_set():
            try:
                self._sesion()
            except grpc.RpcError as exc:
                if self._parar.is_set():
                    return
                self.reconnects += 1
                codigo = exc.code().name if exc.code() else "UNKNOWN"
                self._log.warning(
                    "heartbeat.stream_failed",
                    data_node_id=self.data_node_id,
                    code=codigo,
                    reconnects=self.reconnects,
                )
                # El ControlNode pudo recrear su metadato: si ya no nos conoce, hay que
                # volver a registrarse antes de seguir latiendo al vacio.
                if exc.code() is grpc.StatusCode.NOT_FOUND:
                    try:
                        self.register(max_attempts=5)
                    except Exception:
                        pass
                self.close()
            except Exception as exc:
                # Nada puede matar este hilo: un DataNode sin heartbeat sigue sirviendo
                # bloques, pero uno con el hilo muerto no vuelve nunca al cluster.
                if self._parar.is_set():
                    return
                self._log.error(
                    "heartbeat.loop_error",
                    error=type(exc).__name__,
                    error_detail=str(exc),
                )
                self.close()

            self._parar.wait(self._retry_seconds)

    def start(self) -> None:
        if self._hilo is not None:
            return
        self._parar.clear()
        self._hilo = threading.Thread(
            target=self._bucle, name="dfsha-heartbeat", daemon=True
        )
        self._hilo.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._parar.set()
        if self._hilo is not None:
            self._hilo.join(timeout=timeout)
            self._hilo = None
        self.close()
