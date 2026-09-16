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

from datetime import timedelta

from concurrent import futures
from typing import Callable, Iterator

import grpc

from dfsha.common.errors import DFShaError, NotFoundError
from dfsha.common.logging import get_logger
from dfsha.common.proto.gen import control_pb2, control_pb2_grpc
from dfsha.control_node.commands import control_plane as commands
from dfsha.control_node.commands import rereplication
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
        order_resend_after: timedelta = timedelta(seconds=30),
    ) -> None:
        self._uow = uow_factory
        self._thresholds = thresholds
        self._heartbeat_interval_ms = heartbeat_interval_ms
        self._full_report_every_n = full_report_every_n
        #: Cada cuanto se le repite una orden que no ha confirmado. El nodo late cada
        #: 3 s y copiar un bloque tarda mucho mas que eso: sin este margen se le
        #: repetiria la misma orden veinte veces por minuto.
        self._order_resend_after = order_resend_after
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
        primer_latido = True
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

                razones: list[str] = []
                if primer_latido:
                    # Un report completo por CADA stream que se abre, no solo al
                    # arrancar el nodo. Sin esto hay un agujero real: el contador de
                    # "cada N latidos" vive en la sesion del DataNode, asi que un stream
                    # que se rompe cada pocos segundos no llega nunca al latido N y el
                    # ControlNode se queda con una vision que nadie vuelve a contrastar.
                    # Una reconexion es justo cuando mas probable es que su vision haya
                    # quedado vieja: entre medias pudo perderse cualquier cambio.
                    razones.append("reconexion: primer latido de este stream")
                    primer_latido = False
                if resultado.request_full_report and resultado.reason:
                    razones.append(resultado.reason)

                if razones:
                    # Un solo mensaje con las razones juntas, en vez de dos seguidos que
                    # provocarian dos reports completos identicos.
                    yield control_pb2.ControlMessage(
                        full_report=control_pb2.FullReportReq(reason="; ".join(razones))
                    )

                # Etapa 3: las ordenes pendientes para este nodo viajan por aqui. Es la
                # costura que la Etapa 2 dejo hecha al declarar el stream bidireccional.
                #
                # Se leen de la BASE, no de la memoria de este proceso: este stream lo
                # puede estar atendiendo una instancia que no es la lider, y quien
                # programo la copia fue el lider. Por eso la cola se persiste.
                for mensaje in self._ordenes_para(peticion.data_node_id):
                    yield mensaje
        except grpc.RpcError:
            # El nodo colgo. No es un error del ControlNode: la deteccion de caidas es
            # por ausencia de heartbeat, no por el cierre del stream, precisamente para
            # que una red que se corta y vuelve no cambie el estado del cluster.
            self._log.info("heartbeat.stream_closed", data_node_id=data_node_id)

    def _ordenes_para(self, data_node_id: str) -> list[control_pb2.ControlMessage]:
        """Ordenes pendientes de este nodo, ya marcadas como enviadas.

        Nunca lanza: un fallo leyendo la cola no puede cortar el heartbeat. Perder una
        ronda de ordenes cuesta 3 s de retraso; perder el stream cuesta que el nodo
        parezca muerto y se dispare una re-replicacion de todo su disco.
        """
        try:
            uow = self._uow()
            with uow:
                ordenes = rereplication.pending_orders(
                    uow, data_node_id, self._order_resend_after
                )
                if ordenes:
                    uow.commit()
        except Exception as exc:
            self._log.warning(
                "rereplication.orders_unavailable",
                data_node_id=data_node_id,
                error=type(exc).__name__,
                error_detail=str(exc),
            )
            return []

        mensajes: list[control_pb2.ControlMessage] = []
        for copia in ordenes.replicate:
            self._log.info(
                "rereplication.order_sent",
                task_id=copia.task_id,
                block_id=copia.block_id,
                target=data_node_id,
                source=copia.source_node_id,
            )
            mensajes.append(
                control_pb2.ControlMessage(
                    replicate_block=control_pb2.ReplicateBlock(
                        task_id=copia.task_id,
                        block_id=copia.block_id,
                        source_base_url=copia.source_base_url,
                        source_node_id=copia.source_node_id,
                        size=copia.size,
                        checksum_sha256=copia.checksum_sha256,
                    )
                )
            )
        for borrado in ordenes.delete:
            mensajes.append(
                control_pb2.ControlMessage(
                    delete_block=control_pb2.DeleteBlock(
                        task_id=borrado.task_id,
                        block_id=borrado.block_id,
                        reason=borrado.reason,
                    )
                )
            )
        return mensajes

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
