"""Cola de re-replicacion

La cola se persiste en vez de vivir en la memoria del lider, y ese es el punto: en
memoria se perderia justo cuando mas falta hace, que es cuando el lider cambia de manos.
Ademas el stream de heartbeat del nodo destino lo puede estar atendiendo otra instancia,
que tiene que poder leer la orden para empujarsela.

El indice unico parcial sobre los estados vivos es lo que impide programar dos veces la
misma copia: dos pasadas del planificador, o dos lideres solapados durante un relevo, se
encuentran con la fila que ya existe.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ID_LEN = 36


def upgrade() -> None:
    op.create_table(
        "rereplication_tasks",
        sa.Column("id", sa.String(ID_LEN), nullable=False),
        sa.Column("block_id", sa.String(ID_LEN), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("source_node_id", sa.String(ID_LEN), nullable=True),
        sa.Column("target_node_id", sa.String(ID_LEN), nullable=True),
        sa.Column("replicas_at_schedule", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("dispatched_at", sa.DateTime(), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(255), nullable=True),
        sa.ForeignKeyConstraint(["block_id"], ["blocks.block_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_rereplication_block_activa",
        "rereplication_tasks",
        ["block_id"],
        unique=True,
        sqlite_where=sa.text("state IN ('PENDING', 'IN_FLIGHT')"),
        postgresql_where=sa.text("state IN ('PENDING', 'IN_FLIGHT')"),
    )
    op.create_index("ix_rereplication_state", "rereplication_tasks", ["state"])
    op.create_index(
        "ix_rereplication_target", "rereplication_tasks", ["target_node_id", "state"]
    )


def downgrade() -> None:
    op.drop_index("ix_rereplication_target", table_name="rereplication_tasks")
    op.drop_index("ix_rereplication_state", table_name="rereplication_tasks")
    op.drop_index("uq_rereplication_block_activa", table_name="rereplication_tasks")
    op.drop_table("rereplication_tasks")
