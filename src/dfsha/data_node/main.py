"""Punto de entrada del DataNode.

    uvicorn dfsha.data_node.main:create_app --factory --port 8001
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from dfsha.common.logging import configure_logging, get_logger
from dfsha.common.proto.gen import control_pb2
from dfsha.control_node.api.errors import install_error_handlers
from dfsha.data_node.config import DataNodeSettings, load_settings_or_exit
from dfsha.data_node.control_client import ControlClient, Identity, NodeIdentity
from dfsha.data_node.heartbeat import BlockChangeLog, HeartbeatClient
from dfsha.data_node.orders import OrderExecutor
from dfsha.data_node.runtime import LoadTracker
from dfsha.data_node.storage import BlockStorage

from .api.routers import blocks_router, health_router

__all__ = ["create_app"]


class _EstadoDiferido:
    """Referencia al `app.state` que todavia no existe cuando se construye el ejecutor.

    El `data_node_id` definitivo lo devuelve el ControlNode al registrarse, dentro del
    lifespan, asi que el ejecutor de ordenes no puede quedarse con una copia de los
    valores: tiene que leerlos cuando los use. Esto es esa indireccion, y nada mas.
    """

    def __init__(self) -> None:
        self._state = None

    def bind(self, state) -> None:
        self._state = state

    def __getattr__(self, nombre: str):
        if self._state is None:
            raise RuntimeError("el estado del DataNode todavia no esta construido")
        return getattr(self._state, nombre)


def create_app(
    settings: DataNodeSettings | None = None,
    control: ControlClient | None = None,
    register: bool = True,
) -> FastAPI:
    """Construye la app del DataNode.

    `control` y `register` se pueden inyectar para que las pruebas levanten un DataNode
    real sin un ControlNode detras.
    """
    settings = settings or load_settings_or_exit()
    configure_logging(service="data-node", level=settings.log_level)
    log = get_logger("data_node")

    storage = BlockStorage(settings.data_dir)
    identity_store = NodeIdentity(settings.data_dir)
    identity = identity_store.load_or_create()
    capacity = settings.resolved_capacity_bytes()
    control = control or ControlClient(settings.control_url, settings.internal_secret)
    load = LoadTracker()
    changes = BlockChangeLog()

    def stats_proto() -> control_pb2.NodeStats:
        estado = storage.stats()
        return control_pb2.NodeStats(
            used_bytes=estado.used_bytes,
            capacity_bytes=capacity,
            disk_free_bytes=estado.disk_free_bytes,
            block_count=estado.block_count,
            writes_in_flight=load.writes_in_flight,
            reads_in_flight=load.reads_in_flight,
            bytes_written_60s=load.bytes_written_60s(),
        )

    # El ejecutor se construye antes que el heartbeat porque este lo necesita, pero
    # recibe `app.state` y no las piezas sueltas: el `data_node_id` definitivo no se
    # conoce hasta despues del registro, que ocurre en el lifespan.
    app_state_holder = _EstadoDiferido()
    orders = OrderExecutor(app_state_holder, max_workers=settings.order_workers)

    heartbeat = HeartbeatClient(
        grpc_url=settings.control_grpc_url,
        advertise_url=settings.datanode_advertise_url,
        fault_domain=settings.datanode_fault_domain,
        boot_id=identity.boot_id,
        capacity_bytes=capacity,
        stats_provider=stats_proto,
        block_ids_provider=storage.list_block_ids,
        changes=changes,
        data_node_id=identity.data_node_id,
        retry_seconds=settings.register_retry_seconds,
        orders=orders,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if register:
            info = heartbeat.register(max_attempts=settings.register_max_attempts)
            # El id que devuelve el ControlNode manda sobre el guardado en disco: si el
            # metadato se recreo, seguir con el id viejo dejaria replicas apuntando a un
            # nodo que ya no existe.
            if info.data_node_id != identity.data_node_id:
                identity_store.write(
                    Identity(data_node_id=info.data_node_id, boot_id=identity.boot_id)
                )
            elif identity_store.read() is None:
                identity_store.write(identity)
            app.state.data_node_id = info.data_node_id

            # Un report completo nada mas registrarse: es lo que pone al ControlNode al
            # dia sobre lo que este nodo tiene de verdad, incluido lo que cambio
            # mientras estuvo desconectado.
            try:
                heartbeat.send_full_report(reason="arranque")
            except Exception as exc:  # el bucle lo reintentara
                log.warning("block_report.startup_failed", error=type(exc).__name__)

            heartbeat.start()

        estado = storage.stats()
        log.info(
            "data_node.start",
            data_node_id=app.state.data_node_id,
            data_dir=str(storage.root),
            advertise_url=settings.datanode_advertise_url,
            fault_domain=settings.datanode_fault_domain,
            boot_id=identity.boot_id,
            capacity_bytes=capacity,
            used_bytes=estado.used_bytes,
            block_count=estado.block_count,
            disk_free_bytes=estado.disk_free_bytes,
        )
        yield

        heartbeat.stop()
        orders.shutdown()
        log.info("data_node.stop", data_node_id=app.state.data_node_id)

    app = FastAPI(
        title="DFSha DataNode",
        version="0.2.0",
        summary="Almacena bloques opacos por block_id. No conoce rutas ni usuarios.",
        lifespan=lifespan,
    )

    app_state_holder.bind(app.state)

    app.state.settings = settings
    app.state.orders = orders
    app.state.storage = storage
    app.state.control = control
    app.state.capacity_bytes = capacity
    app.state.data_node_id = identity.data_node_id or "sin-registrar"
    app.state.boot_id = identity.boot_id
    app.state.fault_domain = settings.datanode_fault_domain
    app.state.advertise_url = settings.datanode_advertise_url
    app.state.load = load
    app.state.changes = changes
    app.state.heartbeat = heartbeat

    # Misma tabla de traduccion que el ControlNode: los codigos de error de DFSha
    # significan lo mismo en los dos servicios.
    install_error_handlers(app)

    app.include_router(blocks_router)
    app.include_router(health_router)

    return app
