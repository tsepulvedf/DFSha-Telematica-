"""Las migraciones y los modelos describen el mismo esquema.

Esta es la prueba que hace util tener Alembic. Sin ella, anadir una columna a
`models.py` y olvidar la revision produce un sistema que pasa todas las pruebas (que
crean el esquema con `create_all`) y falla al arrancar contra PostgreSQL, que es el
unico sitio donde el esquema viene de las migraciones.

Corre sobre SQLite porque es lo que hay en las pruebas rapidas. No cubre las diferencias
propias de PostgreSQL; para eso esta el arranque real contra el compose.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from dfsha.control_node.repositories.database import (
    SchemaNotMigratedError,
    alembic_head,
    build_engine,
    create_schema,
    prepare_schema,
    schema_revision,
)
from dfsha.control_node.repositories.models import Base

RAIZ = Path(__file__).resolve().parents[2]


def _config(db_url: str) -> Config:
    config = Config(str(RAIZ / "alembic.ini"))
    config.set_main_option("script_location", str(RAIZ / "alembic"))
    config.set_main_option("sqlalchemy.url", db_url)
    return config


@pytest.fixture()
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'migrada.db'}"


def test_migracion_deja_el_mismo_esquema_que_los_modelos(db_url: str) -> None:
    """`alembic upgrade head` y `create_all` tienen que producir lo mismo."""
    command.upgrade(_config(db_url), "head")

    engine = create_engine(db_url, future=True)
    with engine.connect() as conexion:
        contexto = MigrationContext.configure(conexion)
        diferencias = compare_metadata(contexto, Base.metadata)

    assert diferencias == [], (
        "el esquema migrado no coincide con models.py; genera una revision nueva "
        f"para estas diferencias: {diferencias}"
    )


def test_la_migracion_crea_los_indices_parciales(db_url: str) -> None:
    """Los indices unicos parciales son el esquema, no un adorno: sostienen el
    copy-on-write y la unicidad de la raiz por usuario."""
    command.upgrade(_config(db_url), "head")
    inspector = inspect(create_engine(db_url, future=True))

    indices_files = {i["name"] for i in inspector.get_indexes("files")}
    indices_dirs = {i["name"] for i in inspector.get_indexes("directories")}

    assert "uq_files_directory_name_committed" in indices_files
    assert "uq_directories_parent_name" in indices_dirs
    assert "uq_directories_root_per_owner" in indices_dirs


def test_downgrade_deja_la_base_vacia(db_url: str) -> None:
    """Una migracion que no sabe deshacerse no sirve para un despliegue que no se puede
    recrear, que es justo el caso de AWS."""
    config = _config(db_url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")

    tablas = set(inspect(create_engine(db_url, future=True)).get_table_names())
    assert tablas <= {"alembic_version"}


def test_revision_de_la_base_coincide_con_la_cabeza(db_url: str) -> None:
    command.upgrade(_config(db_url), "head")
    engine = create_engine(db_url, future=True)

    assert schema_revision(engine) == alembic_head()
    assert alembic_head() is not None, "no se encontraron los scripts de alembic"


def test_sqlite_no_exige_migraciones(db_url: str) -> None:
    """SQLite es el motor de las pruebas: `prepare_schema` lo crea y sigue.

    Exigirle migraciones obligaria a correr alembic en cada prueba, que es exactamente
    el coste que se quiso evitar al dejar SQLite solo para las pruebas.
    """
    engine = build_engine(db_url)
    prepare_schema(engine)

    assert "files" in inspect(engine).get_table_names()
    assert schema_revision(engine) is None


def test_una_base_sin_migrar_no_arranca(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """El caso que esta comprobacion existe para atrapar: una base de PostgreSQL recien
    creada contra la que alguien arranca el ControlNode sin migrar.

    Se simula con SQLite forzando la rama de "no es sqlite", porque levantar PostgreSQL
    en una prueba unitaria la dejaria de ser.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'sin_migrar.db'}", future=True)
    create_schema(engine)  # tablas si, historia de migraciones no

    monkeypatch.setattr(
        "dfsha.control_node.repositories.database.is_sqlite", lambda _: False
    )

    with pytest.raises(SchemaNotMigratedError) as exc:
        prepare_schema(engine)

    # El mensaje tiene que traer el comando exacto: un error de esquema al arrancar es
    # justo cuando nadie tiene tiempo de ir a buscarlo a la documentacion.
    assert "alembic upgrade head" in str(exc.value)
    assert "alembic stamp" in str(exc.value)


def test_la_0008_da_sal_a_los_usuarios_que_la_0006_dejo_sin_ella(db_url: str) -> None:
    """El estado de DATOS que deja una migracion, no solo el esquema.

    La 0006 anadio `kdf_salt` vacia a los usuarios existentes, y un usuario sin sal subia
    sus archivos EN CLARO sin que nada lo dijera. Ninguna prueba lo vio porque todas
    registran usuarios nuevos, que siempre tienen sal: el estado que produce migrar una
    base con filas no lo montaba nadie. Esta lo monta.
    """
    from sqlalchemy import text

    config = _config(db_url)
    command.upgrade(config, "0007")

    engine = create_engine(db_url, future=True)
    with engine.begin() as conexion:
        for nombre, sal in (("viejo-1", ""), ("viejo-2", ""), ("nuevo", "ab" * 16)):
            conexion.execute(
                text(
                    "INSERT INTO users (id, username, password_hash, kdf_salt, created_at) "
                    "VALUES (:id, :nombre, 'x', :sal, '2026-09-16 06:02:24')"
                ),
                {"id": f"id-{nombre}", "nombre": nombre, "sal": sal},
            )

    command.upgrade(config, "head")

    with engine.connect() as conexion:
        sales = dict(conexion.execute(text("SELECT username, kdf_salt FROM users")).all())

    assert sales["viejo-1"] and sales["viejo-2"], "quedan usuarios sin sal tras migrar"
    assert len(bytes.fromhex(sales["viejo-1"])) == 16
    assert sales["viejo-1"] != sales["viejo-2"], "la sal tiene que ser distinta por usuario"
    assert sales["nuevo"] == "ab" * 16, "la migracion no debe tocar una sal que ya existe"

