"""Punto de entrada del ControlNode.

    uvicorn dfsha.control_node.main:create_app --factory --port 8000

Se arranca por factoria y no con un `app` a nivel de modulo para que importar este
modulo no tenga efectos: sin `--factory`, importarlo sin los secretos en el entorno
terminaria el proceso, incluido el de pytest al recolectar las pruebas.
"""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI, Request

from dfsha.common.logging import configure_logging, get_logger
from dfsha.common.tls import TlsMaterial
from dfsha.control_node.config import ControlNodeSettings, load_settings_or_exit
from dfsha.control_node.repositories.database import (
    build_engine,
    build_session_factory,
    prepare_schema,
)
from dfsha.control_node.domain.leadership import LeaseTimings
from dfsha.control_node.domain.membership import MembershipThresholds
from dfsha.control_node.repositories.sql import SqlUnitOfWork
from dfsha.control_node.commands.rereplication import RereplicationPolicy
from dfsha.control_node.services.leadership import LeadershipService
from dfsha.control_node.services.rereplication_scheduler import RereplicationScheduler
from dfsha.control_node.services.membership_monitor import MembershipMonitor
from dfsha.control_node.services.read_routing import (
    WRITE_LSN_HEADER,
    ReadRouter,
    current_write_lsn,
)

from .api.errors import install_error_handlers
from .api.grpc import ControlPlaneServicer, build_grpc_server
from .api.routers import (
    auth_router,
    cluster_router,
    files_router,
    fs_router,
    internal_router,
)

__all__ = ["create_app", "create_internal_app"]

API_PREFIX = "/api/v1"


def create_internal_app(app_padre: FastAPI) -> FastAPI:
    """App del plano interno: solo `/internal/v1`, en su propio puerto con TLS mutuo.

    ## Por que un puerto aparte y no una ruta protegida

    Uvicorn **no expone a la aplicacion el certificado del cliente**. Eso significa que
    no se puede exigir certificado para `/internal/v1` y no para `/api/v1` dentro del
    mismo puerto: la decision de pedir certificado se toma en el handshake, antes de que
    exista una ruta.

    Se podria haber terminado el mTLS en nginx y pasar el certificado en una cabecera,
    pero entonces la seguridad del plano interno dependeria de que nadie pueda llegar al
    ControlNode sin pasar por nginx, que es una suposicion de red que se rompe sola en
    cuanto alguien publica un puerto por comodidad.

    Con un puerto propio, **la exigencia la hace TLS**: quien no presente un certificado
    firmado por la CA de DFSha no llega a enviar la peticion. Y como este router no lleva
    ninguna dependencia de autenticacion, tampoco hay forma de anadir aqui una ruta y
    olvidarse de protegerla: no hay nada que olvidar.

    Comparte `state` con la app principal a proposito: la misma unidad de trabajo, el
    mismo motor y el mismo servicio de liderazgo. Son dos puertas al mismo proceso, no
    dos procesos.
    """
    interna = FastAPI(
        title="DFSha ControlNode - plano interno",
        version="0.3.0",
        summary=(
            "Solo lo consumen el DataNode y el recolector, y solo con certificado de "
            "la CA de DFSha."
        ),
    )
    interna.state = app_padre.state
    install_error_handlers(interna)
    interna.include_router(internal_router)

    @interna.get("/health", tags=["operacion"])
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "control-node-internal"}

    return interna


class _ServidorInterno:
    """Uvicorn en un hilo para el plano interno.

    En un hilo y no en un proceso aparte porque comparte el estado con la app principal.
    Uvicorn intenta instalar manejadores de senales al arrancar y no puede fuera del hilo
    principal; lo detecta y sigue, asi que la parada se pide con `should_exit`.
    """

    def __init__(self, app: FastAPI, port: int, tls: TlsMaterial) -> None:
        import ssl

        import uvicorn

        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=port,
            log_level="warning",
            access_log=False,
            ssl_certfile=str(tls.cert),
            ssl_keyfile=str(tls.key),
            ssl_ca_certs=str(tls.ca_cert),
            # LA linea. Sin CERT_REQUIRED esto seria TLS sin autenticacion: cifrado con
            # cualquiera, que para un plano de control es casi peor que nada porque
            # parece seguro.
            ssl_cert_reqs=ssl.CERT_REQUIRED,
        )
        self._server = uvicorn.Server(config)
        self._hilo = threading.Thread(
            target=self._server.run, name="dfsha-internal", daemon=True
        )

    def start(self) -> None:
        self._hilo.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._server.should_exit = True
        self._hilo.join(timeout=timeout)


def create_app(settings: ControlNodeSettings | None = None) -> FastAPI:
    """Construye la app.

    Recibe la configuracion por parametro para que las pruebas de integracion puedan
    levantar la app real contra una base de datos temporal sin tocar el entorno.
    """
    settings = settings or load_settings_or_exit()
    configure_logging(service="control-node", level=settings.log_level)
    log = get_logger("control_node")

    engine = build_engine(settings.db_url)
    prepare_schema(engine)
    session_factory = build_session_factory(engine)

    # Lado de lectura de CQRS. Sin replica configurada, `read_engine is engine` y el
    # sistema se comporta exactamente como en las etapas anteriores. El esquema de la
    # replica NO se prepara aqui: lo recibe del primario por replicacion, y tocarlo
    # seria escribir en una base de solo lectura.
    read_engine = engine
    read_session_factory = session_factory
    if settings.db_replica_url:
        read_engine = build_engine(settings.db_replica_url, readonly=True)
        read_session_factory = build_session_factory(read_engine)

    thresholds = MembershipThresholds.from_millis(
        settings.suspect_after_ms, settings.dead_after_ms
    )
    lease_timings = LeaseTimings.from_millis(
        settings.lease_ttl_ms, settings.lease_renew_ms
    )
    # `None` solo cuando la configuracion viene de una prueba que no monta CA. En el
    # arranque real los tres ficheros son obligatorios y los valida `config.py`.
    tls = (
        TlsMaterial.from_paths(settings.tls_ca_cert, settings.tls_cert, settings.tls_key)
        if settings.tls_ca_cert and settings.tls_cert and settings.tls_key
        else None
    )
    rereplication_policy = RereplicationPolicy(
        replication_factor=settings.replication_factor,
        grace=timedelta(milliseconds=settings.rereplication_grace_ms),
        max_per_node=settings.rereplication_max_per_node,
        max_per_pass=settings.rereplication_max_per_pass,
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
            servicer,
            settings.grpc_port,
            max_workers=settings.grpc_max_workers,
            tls=tls,
        )
        grpc_server.start()

        # El liderazgo arranca ANTES que el evaluador: si fuera al reves, la primera
        # pasada del evaluador encontraria `current_fencing()` a None y se saltaria sin
        # motivo. No es un fallo, pero retrasa la primera evaluacion un intervalo entero.
        liderazgo = app.state.leadership
        liderazgo.start()

        monitor = MembershipMonitor(
            uow_factory=app.state.uow_factory,
            thresholds=thresholds,
            interval_seconds=settings.membership_interval_ms / 1000,
            # Aqui es donde el evaluador de la Etapa 2 pasa a exigir liderazgo. Lo que
            # se le pasa es la vista LOCAL de la epoca, que puede estar obsoleta; quien
            # la verifica de verdad es la transaccion, dentro de evaluate_membership.
            fencing_provider=liderazgo.current_fencing,
        )
        monitor.start()

        # La tercera y ultima tarea en background. Va aparte del evaluador de
        # pertenencia porque sus cadencias son muy distintas (1 s frente a 5 s con una
        # gracia de 5 minutos) y porque un fallo escaneando la replicacion no debe
        # impedir que se siga detectando que un nodo se cayo.
        replicador = RereplicationScheduler(
            uow_factory=app.state.uow_factory,
            policy=rereplication_policy,
            thresholds=thresholds,
            interval_seconds=settings.rereplication_interval_ms / 1000,
            fencing_provider=liderazgo.current_fencing,
        )
        replicador.start()

        # Plano interno en su propio puerto, con TLS mutuo. Se arranca aqui y no como
        # un proceso aparte porque comparte todo el estado con la app principal: el
        # motor, la unidad de trabajo y el servicio de liderazgo.
        servidor_interno = None
        if tls is not None:
            servidor_interno = _ServidorInterno(
                create_internal_app(app), settings.internal_port, tls
            )
            servidor_interno.start()

        app.state.grpc_server = grpc_server
        app.state.membership_monitor = monitor
        app.state.rereplication = replicador
        app.state.internal_server = servidor_interno

        log.info(
            "control_node.start",
            db_url=settings.db_url.split("://", 1)[0],  # sin credenciales en el log
            read_replica=bool(settings.db_replica_url),
            block_size=settings.block_size,
            write_ttl_seconds=settings.write_ttl_seconds,
            grpc_port=settings.grpc_port,
            heartbeat_interval_ms=settings.heartbeat_interval_ms,
            suspect_after_ms=settings.suspect_after_ms,
            dead_after_ms=settings.dead_after_ms,
            replication_factor=settings.replication_factor,
            instance_id=liderazgo.instance_id,
            lease_ttl_ms=settings.lease_ttl_ms,
            write_quorum=settings.write_quorum,
            rereplication_grace_ms=settings.rereplication_grace_ms,
            mtls=tls is not None,
            internal_port=settings.internal_port if tls is not None else None,
        )
        yield

        # El evaluador para primero: si parara despues, podria arrancar una pasada con
        # una epoca que esta instancia acaba de soltar.
        replicador.stop()
        monitor.stop()
        liderazgo.stop()
        if servidor_interno is not None:
            servidor_interno.stop()
        # `grace` da margen a los streams de heartbeat abiertos para cerrarse solos en
        # vez de cortarlos a mitad y llenar los logs de los DataNodes de errores.
        grpc_server.stop(grace=2.0).wait(timeout=5.0)
        if read_engine is not engine:
            read_engine.dispose()
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
    app.state.read_engine = read_engine
    app.state.uow_factory = lambda: SqlUnitOfWork(session_factory)
    app.state.query_uow_factory = (
        (lambda: SqlUnitOfWork(read_session_factory))
        if settings.db_replica_url
        else None
    )
    app.state.read_router = ReadRouter(
        app.state.uow_factory, app.state.query_uow_factory
    )
    # Se construye aqui, fuera del lifespan, para que `instance_id` exista desde que se
    # crea la app: el endpoint /cluster/leadership lo necesita para decir quien atendio
    # la peticion, y las pruebas lo consultan sin levantar el lifespan.
    app.state.leadership = LeadershipService(
        uow_factory=app.state.uow_factory, timings=lease_timings
    )

    @app.middleware("http")
    async def sellar_lsn_de_escritura(request: Request, call_next):
        """Tras un comando, devuelve el LSN del primario en una cabecera.

        El cliente lo guarda y lo reenvia en sus consultas, y eso es lo que le garantiza
        leer sus propias escrituras aunque le atienda otro ControlNode contra la replica.
        Va en un middleware y no en cada caso de uso porque tiene que medirse DESPUES del
        commit: el LSN que importa es el que ya incluye la transaccion recien confirmada.

        Con SQLite no hay LSN y no se anade cabecera; el mecanismo queda inerte.
        """
        respuesta = await call_next(request)

        if request.method in ("GET", "HEAD", "OPTIONS") or respuesta.status_code >= 400:
            return respuesta
        if not settings.db_replica_url:
            return respuesta

        uow = app.state.uow_factory()
        try:
            with uow:
                lsn = current_write_lsn(uow)
            if lsn:
                respuesta.headers[WRITE_LSN_HEADER] = lsn
        except Exception as exc:  # pragma: no cover - no puede tumbar una respuesta ok
            log.warning("write_lsn.unavailable", error=type(exc).__name__)
        return respuesta

    install_error_handlers(app)

    app.include_router(auth_router, prefix=API_PREFIX)
    app.include_router(cluster_router, prefix=API_PREFIX)
    app.include_router(fs_router, prefix=API_PREFIX)
    app.include_router(files_router, prefix=API_PREFIX)
    # `internal_router` NO se monta aqui: vive en `create_internal_app`, que se sirve en
    # otro puerto con TLS mutuo. Montarlo tambien aqui abriria el plano interno al
    # puerto de cliente, que es justo lo que el puerto aparte existe para impedir.

    @app.get("/health", tags=["operacion"])
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "control-node"}

    return app
