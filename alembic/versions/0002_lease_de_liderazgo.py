"""Lease de liderazgo del ControlNode

Una sola fila, con clave primaria fija. La migracion la **siembra** aqui mismo, y eso no
es un detalle: si la fila se creara en el primer arranque, tres ControlNodes arrancando a
la vez competirian por insertarla, y `SELECT ... FOR UPDATE` no puede bloquear una fila
que todavia no existe. Sembrarla en la migracion elimina la carrera en vez de resolverla.

Epoca inicial 0 y `leader_id` NULL: nadie manda todavia, y el primero en llegar tomara el
lease con epoca 1.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ID_LEN = 36


def upgrade() -> None:
    leadership = op.create_table(
        "leadership",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("leader_id", sa.String(ID_LEN), nullable=True),
        sa.Column("epoch", sa.BigInteger(), nullable=False),
        sa.Column("acquired_at", sa.DateTime(), nullable=True),
        sa.Column("renewed_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    op.bulk_insert(
        leadership,
        [
            {
                "id": 1,
                "leader_id": None,
                "epoch": 0,
                "acquired_at": None,
                "renewed_at": None,
                "expires_at": None,
            }
        ],
    )


def downgrade() -> None:
    op.drop_table("leadership")
