"""Modelos SQLAlchemy 2.x: la unica parte del ControlNode que sabe como se ven las tablas.

Nada fuera de `repositories/` importa este modulo. Los casos de uso hablan con las
entidades de `domain/`, que no tienen relacion con SQLAlchemy.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

__all__ = [
    "Base",
    "UserRow",
    "DirectoryRow",
    "FileRow",
    "BlockRow",
    "BlockReplicaRow",
    "DataNodeRow",
]

ID_LEN = 36
NAME_LEN = 255


class UtcDateTime(TypeDecorator):
    """Guarda siempre en UTC y devuelve siempre con tzinfo.

    SQLite no tiene tipo fecha: devuelve datetimes naive y comparar uno naive con uno
    aware lanza TypeError. Como toda la logica de vencimiento de reservas son
    comparaciones de fechas, ese fallo aparecerian justo en la ruta que decide si una
    reserva sigue viva. Se normaliza aqui, una vez, en lugar de en cada consulta.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("solo se guardan datetimes con zona horaria")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    username: Mapped[str] = mapped_column(String(NAME_LEN), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class DirectoryRow(Base):
    """El root de cada usuario es la fila con `parent_id IS NULL`.

    El UNIQUE(parent_id, name) no cubre los roots, porque en SQL dos NULL no colisionan.
    Eso es justo lo que hace falta: un root por usuario, todos con parent_id NULL.
    """

    __tablename__ = "directories"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    parent_id: Mapped[str | None] = mapped_column(
        String(ID_LEN), ForeignKey("directories.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(NAME_LEN), nullable=False)
    owner_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("users.id"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("parent_id", "name", name="uq_directories_parent_name"),
        Index("ix_directories_parent", "parent_id"),
    )


class FileRow(Base):
    __tablename__ = "files"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    directory_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("directories.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(NAME_LEN), nullable=False)
    owner_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("users.id"), nullable=False, index=True
    )
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    block_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    committed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    __table_args__ = (
        # Unicidad solo entre los archivos vivos: la misma ruta puede acumular versiones
        # DELETED (copy-on-write) y reservas WRITING abandonadas sin colisionar.
        # Los dos dialectos se declaran juntos a proposito, para que la migracion a
        # PostgreSQL de la Etapa 3 no tenga que tocar el esquema.
        Index(
            "uq_files_directory_name_committed",
            "directory_id",
            "name",
            unique=True,
            sqlite_where=text("state = 'COMMITTED'"),
            postgresql_where=text("state = 'COMMITTED'"),
        ),
        Index("ix_files_directory_state", "directory_id", "state"),
        Index("ix_files_state_expires", "state", "expires_at"),
    )


class BlockRow(Base):
    __tablename__ = "blocks"

    block_id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    file_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("files.id"), nullable=False, index=True
    )
    index: Mapped[int] = mapped_column(Integer, nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (UniqueConstraint("file_id", "index", name="uq_blocks_file_index"),)


class BlockReplicaRow(Base):
    """Una fila por copia. Hoy hay una por bloque; la cardinalidad N ya esta soportada,
    asi que la Etapa 3 solo inserta mas filas."""

    __tablename__ = "block_replicas"

    block_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("blocks.block_id", ondelete="CASCADE"), primary_key=True
    )
    data_node_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("data_nodes.id"), primary_key=True
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class DataNodeRow(Base):
    __tablename__ = "data_nodes"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    base_url: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    capacity_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    used_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    registered_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
