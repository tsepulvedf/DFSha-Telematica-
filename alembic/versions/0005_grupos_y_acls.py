"""Grupos planos y concesiones de permisos

Tres tablas y **ninguna** columna de denegacion, que es la decision de diseño que hace
revisable el modelo: la ausencia de concesion es denegacion. Introducir un «denegar»
obligaria a definir precedencia entre permitir y denegar a distintos niveles del arbol,
y ahi es donde estos sistemas dejan de poder explicarse.

Dos restricciones de unicidad que no son adorno:

- `uq_groups_owner_name`: los grupos se nombran POR DUENO. Que Ana tenga un «equipo» no
  puede impedir que Beto tenga el suyo.
- `uq_acl_directory_principal`: una sola concesion por (directorio, principal). Conceder
  otra vez ACTUALIZA el permiso. Sin esto, bajar un permiso dejaria la concesion vieja
  debajo, y como el permiso efectivo es el maximo, bajarlo no bajaria nada.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ID_LEN = 36
NAME_LEN = 255


def upgrade() -> None:
    op.create_table(
        "groups",
        sa.Column("id", sa.String(ID_LEN), nullable=False),
        sa.Column("name", sa.String(NAME_LEN), nullable=False),
        sa.Column("owner_id", sa.String(ID_LEN), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "name", name="uq_groups_owner_name"),
    )
    op.create_index("ix_groups_owner_id", "groups", ["owner_id"])

    op.create_table(
        "group_members",
        sa.Column("group_id", sa.String(ID_LEN), nullable=False),
        sa.Column("user_id", sa.String(ID_LEN), nullable=False),
        sa.Column("added_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("group_id", "user_id"),
    )
    op.create_index("ix_group_members_user", "group_members", ["user_id"])

    op.create_table(
        "acl_entries",
        sa.Column("id", sa.String(ID_LEN), nullable=False),
        sa.Column("directory_id", sa.String(ID_LEN), nullable=False),
        sa.Column("principal_type", sa.Integer(), nullable=False),
        sa.Column("principal_id", sa.String(ID_LEN), nullable=False),
        sa.Column("permission", sa.Integer(), nullable=False),
        sa.Column("granted_by", sa.String(ID_LEN), nullable=False),
        sa.Column("granted_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["directory_id"], ["directories.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["granted_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "directory_id",
            "principal_type",
            "principal_id",
            name="uq_acl_directory_principal",
        ),
    )
    op.create_index("ix_acl_directory", "acl_entries", ["directory_id"])
    op.create_index(
        "ix_acl_principal", "acl_entries", ["principal_type", "principal_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_acl_principal", table_name="acl_entries")
    op.drop_index("ix_acl_directory", table_name="acl_entries")
    op.drop_table("acl_entries")
    op.drop_index("ix_group_members_user", table_name="group_members")
    op.drop_table("group_members")
    op.drop_index("ix_groups_owner_id", table_name="groups")
    op.drop_table("groups")
