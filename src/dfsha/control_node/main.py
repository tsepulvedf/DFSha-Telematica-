"""Punto de entrada del ControlNode.

    uvicorn dfsha.control_node.main:create_app --factory --port 8000

Se arranca por factoria y no con un `app` a nivel de modulo para que importar este
modulo no tenga efectos: sin `--factory`, importarlo sin los secretos en el entorno
terminaria el proceso, incluido el de pytest al recolectar las pruebas.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from dfsha.common.logging import configure_logging, get_logger
from dfsha.control_node.config import ControlNodeSettings, load_settings_or_exit
from dfsha.control_node.repositories.database import (
    build_engine,
    build_session_factory,
    create_schema,
)
from dfsha.control_node.domain.membership import MembershipThresholds
from dfsha.control_node.repositories.sql import SqlUnitOfWork
from dfsha.control_node.services.membership_monitor import MembershipMonitor

from .api.errors import install_error_handlers
from .api.grpc import ControlPlaneServicer, build_grpc_server
from .api.routers import auth_router, files_router, fs_router, internal_router

__all__ = ["create_app"]

API_PREFIX = "/api/v1"


def create_app(settings: ControlNodeSettings | None = None) -> FastAPI:
    """Construye la app.

    Recibe la configuracion por parametro para que las pruebas de integracion puedan
    levantar la app real contra una base de datos temporal sin tocar el entorno.
    """
    settings = settings or load_settings_or_exit()
    configure_logging(service="control-node", level=settings.log_level)
    log = get_logger("control_node")

    engine = build_engine(settings.db_url)
    create_schema(engine)
    session_factory = build_session_factory(engine)

    thresholds = MembershipThresholds.from_millis(
        settings.suspect_after_ms, settings.dead_after_ms
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # El plano de control vive en su propio servidor gRPC, con sus hilos, en el mismo
        # proceso y contra la misma base de datos. Ver la nota de api/grpc.py sobre por
        # que es el servidor sincrono y no grpc.aio.
        servicer = ControlPlaneServicer(
            uow_factory=app.state.uow_factory,
            thresholds=thresholds,
            heartbeat_interval_ms=settings.heartbeat_interval_ms,
            full_report_every_n=settings.full_report_every_n,
        )
        grpc_server = build_grpc_server(
            servicer, settings.grpc_port, max_workers=settings.grpc_max_workers
        )
        grpc_server.start()

        monitor = MembershipMonitor(
            uow_factory=app.state.uow_factory,
            thresholds=thresholds,
            interval_seconds=settings.membership_interval_ms / 1000,
        )
        monitor.start()

        app.state.grpc_server = grpc_server
        app.state.membership_monitor = monitor

        log.info(
            "control_node.start",
            db_url=settings.db_url.split("://", 1)[0],  # sin credenciales en el log
            block_size=settings.block_size,
            write_ttl_seconds=settings.write_ttl_seconds,
            grpc_port=settings.grpc_port,
            heartbeat_interval_ms=settings.heartbeat_interval_ms,
            suspect_after_ms=settings.suspect_after_ms,
            dead_after_ms=settings.dead_after_ms,
            replication_factor=settings.replication_factor,
        )
        yield

        monitor.stop()
        # `grace` da margen a los streams de heartbeat abiertos para cerrarse solos en
        # vez de cortarlos a mitad y llenar los logs de los DataNodes de errores.
        grpc_server.stop(grace=2.0).wait(timeout=5.0)
        engine.dispose()
        log.info("control_node.stop")

    app = FastAPI(
        title="DFSha ControlNode",
        version="0.1.0",
        summary="Metadatos del sistema de archivos distribuido. Los bytes no pasan por aqui.",
        lifespan=lifespan,
    )

    app.state.settings = settings
    app.state.engine = engine
    app.state.uow_factory = lambda: SqlUnitOfWork(session_factory)

    install_error_handlers(app)

    app.include_router(auth_router, prefix=API_PREFIX)
    app.include_router(fs_router, prefix=API_PREFIX)
    app.include_router(files_router, prefix=API_PREFIX)
    app.include_router(internal_router)  # ya trae su propio /internal/v1

    @app.get("/health", tags=["operacion"])
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "control-node"}

    return app
