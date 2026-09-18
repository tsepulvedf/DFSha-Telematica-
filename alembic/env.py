"""Contexto de migracion de Alembic.

Dos decisiones que importan y que no son las del esqueleto por defecto:

1. **La URL sale de `DFSHA_DB_URL`**, la misma variable que lee el ControlNode, no de
   `alembic.ini`. Con dos fuentes acabas migrando una base y arrancando contra otra, y el
   sintoma es una columna que falta en produccion y esta en tu portatil.
2. **`compare_type=True`** al autogenerar: sin eso, cambiar un `String(16)` por un
   `String(32)` no produce ninguna operacion y la migracion miente en silencio.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# El paquete vive en src/ y no siempre esta instalado en el entorno que corre alembic
# (por ejemplo, el servicio `migrate` del compose antes de instalar el proyecto).
RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ / "src") not in sys.path:
    sys.path.insert(0, str(RAIZ / "src"))

from dfsha.control_node.repositories.models import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    url = os.environ.get("DFSHA_DB_URL")
    if url:
        return url
    url = config.get_main_option("sqlalchemy.url", "")
    if url:
        return url
    raise SystemExit(
        "falta DFSHA_DB_URL.\n"
        "\n"
        "Alembic usa la misma variable que el ControlNode, a proposito. Ejemplo:\n"
        "\n"
        '    DFSHA_DB_URL="postgresql+psycopg://dfsha:...@localhost:5432/dfsha" \\\n'
        "        alembic upgrade head\n"
    )


def run_migrations_offline() -> None:
    """Genera el SQL sin conectarse. Util para revisar que hara una migracion en AWS."""
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    seccion = config.get_section(config.config_ini_section, {})
    seccion["sqlalchemy.url"] = _url()

    connectable = engine_from_config(
        seccion, prefix="sqlalchemy.", poolclass=pool.NullPool
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            # SQLite no sabe hacer ALTER de casi nada; el modo batch recrea la tabla.
            # Aqui solo aplica a las pruebas: en produccion el metadato es PostgreSQL.
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
