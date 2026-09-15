"""Servicio gRPC del plano de control.

Misma disciplina que los routers HTTP: esta capa solo traduce. Protobuf entra, caso de
uso se ejecuta, Protobuf sale. Ninguna regla vive aqui.

Se usa el servidor **sincrono** de gRPC, con su propio pool de hilos, no `grpc.aio`. La
razon es que los casos de uso y SQLAlchemy son sincronos: montarlos sobre un servidor
asincrono obligaria a saltar a un executor en cada llamada, o bloquearia el bucle de
eventos de FastAPI. Con el servidor sincrono, el plano de control vive en sus hilos y el
plano REST en el suyo, compartiendo solo la base de datos.
"""

from __future__ import annotations

from concurrent import futures
from typing import Callable, Iterator

import grpc

from dfsha.common.errors import DFShaError, NotFoundError
from dfsha.common.logging import get_logger
from dfsha.common.proto.gen import control_pb2, control_pb2_grpc
from dfsha.control_node.commands import control_plane as commands
from dfsha.control_node.domain.entities import NodeStats
from dfsha.control_node.domain.membership import MembershipThresholds
from dfsha.control_node.repositories.sql import SqlUnitOfWork

__all__ = ["ControlPlaneServicer", "build_grpc_server"]

UowFactory = Callable[[], SqlUnitOfWork]


def _to_stats(stats: control_pb2.NodeStats) -> NodeStats:
    return NodeStats(
        used_bytes=stats.used_bytes,
        capacity_bytes=stats.capacity_bytes,
        disk_free_bytes=stats.disk_free_bytes,
        block_count=stats.block_count,
        writes_in_flight=stats.writes_in_flight,
        reads_in_flight=stats.reads_in_flight,
        bytes_written_60s=stats.bytes_written_60s,
    )


class ControlPlaneServicer(control_pb2_grpc.ControlPlaneServicer):
    def __init__(
        self,
        uow_factory: UowFactory,
        thresholds: MembershipThresholds,
        heartbeat_interval_ms: int,
        full_report_every_n: int,
    ) -> None:
        self._uow = uow_factory
        self._thresholds = thresholds
        self._heartbeat_interval_ms = heartbeat_interval_ms
        self._full_report_every_n = full_report_every_n
        self._log = get_logger("control_node")

    # --- Register ----------------------------------------------------------

    def Register(self, request, context):
        try:
            resultado = commands.register_node(
                self._uow(),
                advertise_url=request.advertise_url,
                fault_domain=request.fault_domain,
                boot_id=request.boot_id,
                capacity_bytes=request.capacity_bytes,
                data_node_id=request.data_node_id,
            )
        except DFShaError as exc:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, exc.message)
            raise  # inalcanzable: abort() lanza

        return control_pb2.RegisterResponse(
            data_node_id=resultado.node.id,
            heartbeat_interval_ms=self._heartbeat_interval_ms,
            full_report_every_n=self._full_report_every_n,
        )

    # --- Heartbeat ---------------------------------------------------------

    def Heartbeat(self, request_iterator, context) -> Iterator[control_pb2.ControlMessage]:
        """Stream bidireccional, un hilo por DataNode conectado.

        Hoy el ControlNode solo responde `Ack` y, cuando su vision no cuadra con lo que
        el nodo reporta, `FullReportReq`. El canal es bidireccional desde ya porque es
        por donde la Etapa 3 empujara las ordenes de re-replicacion: anadirlas sera
        meter un caso mas en el `oneof`, no rehacer el transporte.
        """
        data_node_id = ""
        try:
            for peticion in request_iterator:
                data_node_id = peticion.data_node_id
                try:
                    resultado = commands.record_heartbeat(
                        self._uow(),
                        data_node_id=peticion.data_node_id,
                        boot_id=peticion.boot_id,
                        sequence=peticion.sequence,
                        stats=_to_stats(peticion.stats),
                        added_block_ids=list(peticion.added_block_ids),
                        removed_block_ids=list(peticion.removed_block_ids),
                        thresholds=self._thresholds,
                    )
                except NotFoundError:
                    # El nodo cree estar registrado y el ControlNode no le conoce: pasa
                    # si el metadato se recreo. Se le corta el stream para que vuelva a
                    # registrarse en vez de latir al vacio para siempre.
                    context.abort(
                        grpc.StatusCode.NOT_FOUND,
                        "DataNode no registrado; vuelve a llamar a Register",
                    )
                    return

                yield control_pb2.ControlMessage(
                    ack=control_pb2.Ack(acked_sequence=peticion.sequence)
                )
                if resultado.request_full_report:
                    yield control_pb2.ControlMessage(
                        full_report=control_pb2.FullReportReq(reason=resultado.reason)
                    )
        except grpc.RpcError:
            # El nodo colgo. No es un error del ControlNode: la deteccion de caidas es
            # por ausencia de heartbeat, no por el cierre del stream, precisamente para
            # que una red que se corta y vuelve no cambie el estado del cluster.
            self._log.info("heartbeat.stream_closed", data_node_id=data_node_id)

    # --- BlockReport -------------------------------------------------------

    def BlockReport(self, request, context):
        try:
            informe = commands.apply_block_report(
                self._uow(),
                data_node_id=request.data_node_id,
                boot_id=request.boot_id,
                is_full=request.is_full,
                reported_block_ids=[b.block_id for b in request.blocks],
            )
        except NotFoundError as exc:
            context.abort(grpc.StatusCode.NOT_FOUND, exc.message)
            raise

        return control_pb2.BlockReportResponse(
            unknown_blocks=informe.unknown_count,
            missing_blocks=informe.missing_count,
        )


def build_grpc_server(
    servicer: ControlPlaneServicer,
    port: int,
    max_workers: int = 16,
) -> grpc.Server:
    """Servidor gRPC escuchando en `port`.

    `max_workers` tiene que ser holgadamente mayor que el numero de DataNodes: cada
    stream de heartbeat ocupa un hilo mientras esta abierto. Con el pool justo, el nodo
    N+1 se queda esperando un hilo libre y el ControlNode lo declararia muerto sin que
    le pase nada.
    """
    servidor = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))
    control_pb2_grpc.add_ControlPlaneServicer_to_server(servicer, servidor)
    servidor.add_insecure_port(f"[::]:{port}")
    return servidor
