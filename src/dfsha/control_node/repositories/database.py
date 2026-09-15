"""Motor, sesiones y estado del esquema.

Dos motores, no uno. El **primario** atiende los comandos y todo lo que tenga que leer
sus propias escrituras; la **replica de lectura**, cuando esta configurada, atiende las
consultas. Si `DFSHA_DB_REPLICA_URL` viene vacia, `read_engine is write_engine` y el
sistema se comporta exactamente como en las etapas anteriores: esa es la propiedad que
hace que prescindir de la replica sea borrar una variable de entorno, no deshacer codigo.

Sobre el esquema, la regla es de una linea: **PostgreSQL se migra con Alembic; SQLite se
crea con `create_all`**. SQLite solo se usa en las pruebas, donde recrear el esquema en
memoria es mas rapido y no hay ningun dato que conservar; en produccion, un esquema que
se crea solo es un esquema del que nadie sabe en que version esta.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

__all__ = [
    "build_engine",
    "build_session_factory",
    "prepare_schema",
    "create_schema",
    "is_sqlite",
    "alembic_head",
    "schema_revision",
    "SchemaTooOldError",
    "SchemaNotMigratedError",
]


def is_sqlite(engine_or_url: Engine | str) -> bool:
    if isinstance(engine_or_url, str):
        return engine_or_url.startswith("sqlite")
    return engine_or_url.dialect.name == "sqlite"


def build_engine(db_url: str, echo: bool = False, readonly: bool = False) -> Engine:
    connect_args: dict = {}
    kwargs: dict = {}

    if is_sqlite(db_url):
        # El ControlNode sirve peticiones desde varios hilos del pool de Uvicorn y una
        # conexion SQLite solo vale para el hilo que la abrio, salvo que se desactive la
        # comprobacion. Cada sesion sigue usando su propia conexion.
        connect_args["check_same_thread"] = False
    else:
        # `pool_pre_ping` manda un SELECT 1 antes de entregar una conexion del pool. Sin
        # el, una conexion que el servidor cerro por su cuenta (reinicio de PostgreSQL,
        # promocion de la replica, timeout de un balanceador) se entrega igual y la
        # peticion muere con un error de red que no dice nada. Con tres ControlNodes
        # contra la misma base, eso deja de ser teorico.
        kwargs["pool_pre_ping"] = True
        kwargs["pool_size"] = 5
        kwargs["max_overflow"] = 10
        kwargs["pool_recycle"] = 1800
        if readonly:
            # La replica rechaza cualquier escritura por si sola, pero declararlo aqui
            # convierte un error del servidor en uno del cliente, con traza util.
            connect_args["options"] = "-c default_transaction_read_only=on"

    engine = create_engine(
        db_url, echo=echo, future=True, connect_args=connect_args, **kwargs
    )

    if is_sqlite(db_url):
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


class SchemaNotMigratedError(RuntimeError):
    """El metadato no esta en la ultima revision de Alembic."""


# --- Alembic ---------------------------------------------------------------


def _alembic_ini() -> Path | None:
    """Busca `alembic.ini` subiendo desde este modulo.

    Funciona igual en el repositorio (raiz del proyecto) y en la imagen de Docker, que
    instala el paquete con `-e .` y conserva la misma estructura bajo `/app`.
    """
    forzado = os.environ.get("DFSHA_ALEMBIC_DIR")
    if forzado:
        ruta = Path(forzado) / "alembic.ini"
        return ruta if ruta.is_file() else None

    aqui = Path(__file__).resolve()
    for padre in aqui.parents[:6]:
        candidato = padre / "alembic.ini"
        if candidato.is_file() and (padre / "alembic").is_dir():
            return candidato
    return None


def alembic_head() -> str | None:
    """La revision mas reciente segun los ficheros de `alembic/versions`.

    `None` si no se encuentran los scripts: puede pasar en un despliegue que solo copio
    el paquete. Entonces no se puede comprobar si la base esta al dia, y se dice.
    """
    ini = _alembic_ini()
    if ini is None:
        return None

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(ini))
    config.set_main_option("script_location", str(ini.parent / "alembic"))
    return ScriptDirectory.from_config(config).get_current_head()


def schema_revision(engine: Engine) -> str | None:
    """En que revision esta la base. `None` si nunca se migro."""
    inspector = inspect(engine)
    if "alembic_version" not in inspector.get_table_names():
        return None
    with engine.connect() as conexion:
        fila = conexion.execute(text("SELECT version_num FROM alembic_version")).first()
    return fila[0] if fila else None


# --- Preparacion del esquema ----------------------------------------------


def prepare_schema(engine: Engine) -> None:
    """Deja el esquema listo, o falla pronto diciendo que hacer.

    SQLite (pruebas) se crea aqui mismo. Cualquier otro motor **no se toca**: se
    comprueba que Alembic ya lo dejo en la ultima revision. Que el ControlNode no migre
    es deliberado: con tres instancias arrancando a la vez contra la misma base,
    competirian por aplicar la misma migracion. Migra el servicio `migrate`, una sola
    vez, y estas comprobaciones son las que garantizan que nadie arranca antes.
    """
    if is_sqlite(engine):
        create_schema(engine)
        return

    _rechazar_esquema_viejo(engine)

    actual = schema_revision(engine)
    cabeza = alembic_head()

    if actual is None:
        raise SchemaNotMigratedError(
            "la base de datos no tiene migraciones aplicadas.\n"
            "\n"
            "Desde la Etapa 3 el esquema lo crea Alembic, no el ControlNode:\n"
            "\n"
            "    docker compose run --rm migrate      # o, sin Docker:\n"
            '    DFSHA_DB_URL="..." alembic upgrade head\n'
            "\n"
            "Si la base ya tenia datos de la Etapa 2 con el esquema correcto, marcala\n"
            "como migrada sin tocar los datos:\n"
            "\n"
            '    DFSHA_DB_URL="..." alembic stamp 0001\n'
        )

    if cabeza is None:
        # Sin los scripts a mano no hay con que comparar. No es motivo para no arrancar:
        # la base dice que fue migrada alguna vez, y negarse aqui dejaria el servicio
        # caido por no encontrar un fichero .py.
        return

    if actual != cabeza:
        raise SchemaNotMigratedError(
            f"la base de datos esta en la revision {actual} y el codigo espera {cabeza}.\n"
            "\n"
            "Aplica las migraciones que faltan antes de arrancar:\n"
            "\n"
            "    docker compose run --rm migrate      # o, sin Docker:\n"
            '    DFSHA_DB_URL="..." alembic upgrade head\n'
        )


def create_schema(engine: Engine) -> None:
    """Crea las tablas que falten. Solo para SQLite: ver `prepare_schema`."""
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
    """Un metadato de la Etapa 1 no tiene las columnas del plano de control.

    `create_all` no anade columnas a una tabla que ya existe, y Alembic no puede
    reconstruir una historia que nunca existio, asi que el sintoma seria un "no such
    column" a mitad de la primera consulta. Se detecta al arrancar.
    """
    inspector = inspect(engine)
    tablas = set(inspector.get_table_names())

    for tabla, requeridas in _COLUMNAS_REQUERIDAS.items():
        if tabla not in tablas:
            continue  # base de datos nueva: se creara bien
        presentes = {c["name"] for c in inspector.get_columns(tabla)}
        faltan = [c for c in requeridas if c not in presentes]
        if not faltan:
            continue

        raise SchemaTooOldError(
            f"el metadato es de la Etapa 1: a la tabla '{tabla}' le faltan las columnas "
            f"{', '.join(faltan)}.\n"
            "\n"
            "La Etapa 2 anade el plano de control (dominios de falla, boot_id, "
            "estadisticas de heartbeat) y no hay migracion que parta de ahi.\n"
            "\n"
            "Para recrearlo desde cero:\n"
            "\n"
            "    docker compose down -v && docker compose up --build -d\n"
            "\n"
            "AVISO: '-v' BORRA los volumenes, y con ellos TODO el metadato y TODOS los\n"
            "bloques ya subidos. Los archivos que hubiera en DFSha se pierden. Si son\n"
            "datos que te importan, bajalos con 'dfsha get' antes de hacerlo."
        )
