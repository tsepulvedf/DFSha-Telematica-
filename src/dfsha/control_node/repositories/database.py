"""Motor y sesiones de SQLAlchemy.

Aislado en su propio modulo para que cambiar SQLite por PostgreSQL en la Etapa 3 sea
cambiar una URL y borrar los `PRAGMA` de aqui.
"""

from __future__ import annotations

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

__all__ = ["build_engine", "build_session_factory", "create_schema"]


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


def create_schema(engine: Engine) -> None:
    """Crea las tablas si faltan.

    Basta para la Etapa 1. Cuando la Etapa 3 traiga PostgreSQL y un esquema que
    evoluciona, esto se sustituye por migraciones de verdad.
    """
    Base.metadata.create_all(engine)
