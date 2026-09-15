"""Esquema de la Etapa 2, punto de partida de las migraciones

Esta primera revision no cambia nada: **reproduce el esquema tal y como lo dejo la
Etapa 2**, que hasta ahora se creaba con `create_all`. A partir de aqui el esquema tiene
historia y deja de recrearse borrando el volumen.

Los indices unicos son parciales y se declaran para los dos dialectos en la misma
llamada, igual que en `models.py`: es lo que sostiene el copy-on-write (una sola fila
COMMITTED por ruta, pero tantas DELETED como versiones haya habido).

Revision ID: 0001
Revises:
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ID_LEN = 36
NAME_LEN = 255


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(ID_LEN), nullable=False),
        sa.Column("username", sa.String(NAME_LEN), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )

    op.create_table(
        "directories",
        sa.Column("id", sa.String(ID_LEN), nullable=False),
        sa.Column("parent_id", sa.String(ID_LEN), nullable=True),
        sa.Column("name", sa.String(NAME_LEN), nullable=False),
        sa.Column("owner_id", sa.String(ID_LEN), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["parent_id"], ["directories.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_directories_owner_id", "directories", ["owner_id"])
    op.create_index("ix_directories_parent", "directories", ["parent_id"])
    # Unicidad solo entre directorios vivos: `rmdir /a` y luego `mkdir /a` tiene que
    # funcionar mientras el /a viejo espera al GC.
    op.create_index(
        "uq_directories_parent_name",
        "directories",
        ["parent_id", "name"],
        unique=True,
        sqlite_where=sa.text("deleted_at IS NULL"),
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    # El indice de arriba no protege las raices: en SQL dos NULL nunca colisionan, asi
    # que nada impediria dos arboles para una sola cuenta.
    op.create_index(
        "uq_directories_root_per_owner",
        "directories",
        ["owner_id"],
        unique=True,
        sqlite_where=sa.text("parent_id IS NULL AND deleted_at IS NULL"),
        postgresql_where=sa.text("parent_id IS NULL AND deleted_at IS NULL"),
    )

    op.create_table(
        "files",
        sa.Column("id", sa.String(ID_LEN), nullable=False),
        sa.Column("directory_id", sa.String(ID_LEN), nullable=False),
        sa.Column("name", sa.String(NAME_LEN), nullable=False),
        sa.Column("owner_id", sa.String(ID_LEN), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("block_size", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("committed_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["directory_id"], ["directories.id"]),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_files_owner_id", "files", ["owner_id"])
    op.create_index("ix_files_directory_state", "files", ["directory_id", "state"])
    op.create_index("ix_files_state_expires", "files", ["state", "expires_at"])
    op.create_index(
        "uq_files_directory_name_committed",
        "files",
        ["directory_id", "name"],
        unique=True,
        sqlite_where=sa.text("state = 'COMMITTED'"),
        postgresql_where=sa.text("state = 'COMMITTED'"),
    )

    op.create_table(
        "data_nodes",
        sa.Column("id", sa.String(ID_LEN), nullable=False),
        sa.Column("advertise_url", sa.String(512), nullable=False),
        sa.Column("fault_domain", sa.String(128), nullable=False),
        sa.Column("boot_id", sa.String(ID_LEN), nullable=False),
        sa.Column("capacity_bytes", sa.BigInteger(), nullable=False),
        sa.Column("used_bytes", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("registered_at", sa.DateTime(), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("last_sequence", sa.BigInteger(), nullable=False),
        sa.Column("stat_used_bytes", sa.BigInteger(), nullable=False),
        sa.Column("stat_capacity_bytes", sa.BigInteger(), nullable=False),
        sa.Column("stat_disk_free_bytes", sa.BigInteger(), nullable=False),
        sa.Column("stat_block_count", sa.Integer(), nullable=False),
        sa.Column("stat_writes_in_flight", sa.Integer(), nullable=False),
        sa.Column("stat_reads_in_flight", sa.Integer(), nullable=False),
        sa.Column("stat_bytes_written_60s", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("advertise_url"),
    )
    op.create_index("ix_data_nodes_state", "data_nodes", ["state"])

    op.create_table(
        "blocks",
        sa.Column("block_id", sa.String(ID_LEN), nullable=False),
        sa.Column("file_id", sa.String(ID_LEN), nullable=False),
        sa.Column("index", sa.Integer(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("checksum_sha256", sa.String(64), nullable=True),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"]),
        sa.PrimaryKeyConstraint("block_id"),
        sa.UniqueConstraint("file_id", "index", name="uq_blocks_file_index"),
    )
    op.create_index("ix_blocks_file_id", "blocks", ["file_id"])

    op.create_table(
        "block_replicas",
        sa.Column("block_id", sa.String(ID_LEN), nullable=False),
        sa.Column("data_node_id", sa.String(ID_LEN), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["block_id"], ["blocks.block_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["data_node_id"], ["data_nodes.id"]),
        sa.PrimaryKeyConstraint("block_id", "data_node_id"),
    )


def downgrade() -> None:
    op.drop_table("block_replicas")
    op.drop_index("ix_blocks_file_id", table_name="blocks")
    op.drop_table("blocks")
    op.drop_index("ix_data_nodes_state", table_name="data_nodes")
    op.drop_table("data_nodes")
    op.drop_index("uq_files_directory_name_committed", table_name="files")
    op.drop_index("ix_files_state_expires", table_name="files")
    op.drop_index("ix_files_directory_state", table_name="files")
    op.drop_index("ix_files_owner_id", table_name="files")
    op.drop_table("files")
    op.drop_index("uq_directories_root_per_owner", table_name="directories")
    op.drop_index("uq_directories_parent_name", table_name="directories")
    op.drop_index("ix_directories_parent", table_name="directories")
    op.drop_index("ix_directories_owner_id", table_name="directories")
    op.drop_table("directories")
    op.drop_table("users")
