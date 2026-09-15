"""Punto de entrada del DataNode.

    uvicorn dfsha.data_node.main:create_app --factory --port 8001
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from dfsha.common.logging import configure_logging, get_logger
from dfsha.control_node.api.errors import install_error_handlers
from dfsha.data_node.config import DataNodeSettings, load_settings_or_exit
from dfsha.data_node.control_client import ControlClient, NodeIdentity
from dfsha.data_node.storage import BlockStorage

from .api.routers import blocks_router, health_router

__all__ = ["create_app"]


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
    identity = NodeIdentity(settings.data_dir)
    capacity = settings.resolved_capacity_bytes()
    control = control or ControlClient(settings.control_url, settings.internal_secret)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if register:
            # El id que devuelve el ControlNode manda sobre el guardado en disco: si el
            # metadato se recreo, seguir usando el id viejo dejaria replicas apuntando a
            # un nodo que ya no existe.
            data_node_id = control.register(
                base_url=settings.datanode_base_url,
                capacity_bytes=capacity,
                max_attempts=settings.register_max_attempts,
                retry_seconds=settings.register_retry_seconds,
            )
            if data_node_id != identity.read():
                identity.write(data_node_id)
            app.state.data_node_id = data_node_id

        stats = storage.stats()
        log.info(
            "data_node.start",
            data_node_id=app.state.data_node_id,
            data_dir=str(storage.root),
            capacity_bytes=capacity,
            used_bytes=stats.used_bytes,
            block_count=stats.block_count,
            disk_free_bytes=stats.disk_free_bytes,
        )
        yield
        log.info("data_node.stop", data_node_id=app.state.data_node_id)

    app = FastAPI(
        title="DFSha DataNode",
        version="0.1.0",
        summary="Almacena bloques opacos por block_id. No conoce rutas ni usuarios.",
        lifespan=lifespan,
    )

    app.state.settings = settings
    app.state.storage = storage
    app.state.control = control
    app.state.capacity_bytes = capacity
    app.state.data_node_id = identity.read() or "sin-registrar"

    # Misma tabla de traduccion que el ControlNode: los codigos de error de DFSha
    # significan lo mismo en los dos servicios.
    install_error_handlers(app)

    app.include_router(blocks_router)
    app.include_router(health_router)

    return app
