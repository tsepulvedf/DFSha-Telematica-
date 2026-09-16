"""Direccion de par del DataNode

Hasta la Etapa 2, un DataNode solo tenia que ser alcanzable por el CLIENTE, asi que una
sola direccion bastaba. Desde el Bloque B los DataNodes hablan **entre si** (pipeline de
escritura y re-replicacion), y los dos grupos pueden estar en redes distintas: en compose
el cliente esta fuera (`localhost:800N`) y los vecinos dentro (`data-node-N:8001`).

`peer_url` vacia significa "la misma que la del cliente", que es el despliegue donde
cliente y nodos comparten red. Por eso la columna nace con `server_default=''` y no
NULL: una fila existente de la Etapa 2 sigue siendo valida sin tocarla, y su
comportamiento no cambia.

**Sin restriccion de unicidad**, al contrario que `advertise_url`. Dos nodos con la misma
direccion de par serian un error de despliegue, pero una restriccion impediria el caso
legitimo de dejarla vacia en varios nodos a la vez.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "data_nodes",
        sa.Column("peer_url", sa.String(512), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("data_nodes", "peer_url")
