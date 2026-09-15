"""Motor y sesiones de SQLAlchemy.

Aislado en su propio modulo para que cambiar SQLite por PostgreSQL en la Etapa 3 sea
cambiar una URL y borrar los `PRAGMA` de aqui.
"""

from __future__ import annotations

from sqlalchemy import Engine, create_engine, event, inspect
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

__all__ = [
    "build_engine",
    "build_session_factory",
    "create_schema",
    "SchemaTooOldError",
]


def build_engine(db_url: str, echo: bool = False) -> Engine:
    connect_args: dict = {}
    if db_url.startswith("sqlite"):
        # El ControlNode sirve peticiones desde varios hilos del pool de Uvicorn y una
        # conexion SQLite solo vale para el hilo que la abrio, salvo que se desactive la
        # comprobacion. Cada sesion sigue usando su propia conexion.
        connect_args["check_same_thread"] = False

    engine = create_engine(db_url, echo=echo, future=True, connect_args=connect_args)

    if db_url.startswith("sqlite"):
        _apply_sqlite_pragmas(engine)

    return engine


def _apply_sqlite_pragmas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_connection, _record) -> None:  # pragma: no cover - efecto de E/S
        cursor = dbapi_connection.cursor()
        # SQLite trae las claves foraneas desactivadas por defecto. Sin esto, una replica
        # podria quedar apuntando a un bloque borrado y el GC no tendria como notarlo.
        cursor.execute("PRAGMA foreign_keys=ON")
        # WAL: los lectores (ls, stat, open) no se bloquean mientras hay una escritura en
        # curso. Es la aproximacion mas cercana a la separacion CQRS sin un segundo motor.
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        # Espera en vez de devolver "database is locked" ante escrituras concurrentes.
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


class SchemaTooOldError(RuntimeError):
    """El metadato existe pero es de una etapa anterior."""


def create_schema(engine: Engine) -> None:
    """Crea las tablas que falten y rechaza un metadato de una etapa anterior.

    `create_all` crea tablas nuevas, pero **no anade columnas a una tabla que ya existe**.
    Un volumen de metadato de la Etapa 1 tiene `data_nodes` sin `fault_domain`, `boot_id`
    ni las columnas del heartbeat, y el arranque parece ir bien hasta que la primera
    consulta falla con un "no such column" que no explica nada.

    No se hace migracion: no hay herramienta de migraciones en el proyecto, y la Etapa 3
    va a rehacer esto al pasar a PostgreSQL. Se falla pronto y con instrucciones.
    """
    _rechazar_esquema_viejo(engine)
    Base.metadata.create_all(engine)


#: Columnas que la Etapa 2 anadio a tablas ya existentes en la Etapa 1.
_COLUMNAS_REQUERIDAS: dict[str, tuple[str, ...]] = {
    "data_nodes": (
        "advertise_url",
        "fault_domain",
        "boot_id",
        "last_heartbeat_at",
        "stat_disk_free_bytes",
    ),
}


def _rechazar_esquema_viejo(engine: Engine) -> None:
    inspector = inspect(engine)
    tablas = set(inspector.get_table_names())

    for tabla, requeridas in _COLUMNAS_REQUERIDAS.items():
        if tabla not in tablas:
            continue  # base de datos nueva: create_all la creara bien
        presentes = {c["name"] for c in inspector.get_columns(tabla)}
        faltan = [c for c in requeridas if c not in presentes]
        if not faltan:
            continue

        raise SchemaTooOldError(
            f"el metadato es de la Etapa 1: a la tabla '{tabla}' le faltan las columnas "
            f"{', '.join(faltan)}.\n"
            "\n"
            "La Etapa 2 anade el plano de control (dominios de falla, boot_id, "
            "estadisticas de heartbeat) y no hay migracion automatica.\n"
            "\n"
            "Para recrearlo desde cero:\n"
            "\n"
            "    docker compose down -v && docker compose up --build -d\n"
            "\n"
            "AVISO: '-v' BORRA los volumenes, y con ellos TODO el metadato y TODOS los\n"
            "bloques ya subidos. Los archivos que hubiera en DFSha se pierden. Si son\n"
            "datos que te importan, bajalos con 'dfsha get' antes de hacerlo."
        )
