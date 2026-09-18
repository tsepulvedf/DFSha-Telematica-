"""Sal del usuario y clave de archivo envuelta

Las dos unicas columnas que el cifrado extremo a extremo necesita en el servidor, y las
dos son inofensivas por separado:

- `users.kdf_salt` no es secreta. Su trabajo es que dos usuarios con la misma contrasena
  tengan claves distintas; por eso se puede devolver en el login.
- `files.wrapped_key` es la clave del archivo cifrada con la clave maestra del usuario,
  que **nunca sale del cliente**. Sin ella es ruido.

Con las dos, la contrasena hasheada y todos los bloques del disco, el servidor sigue sin
poder descifrar un solo byte. Esa es la diferencia entre cifrar en el cliente y cifrar en
el servidor.

**Las dos nacen vacias** (`server_default=''`), y eso no es pereza: un archivo de las
Etapas 1 o 2 no esta cifrado, y `wrapped_key` vacia es exactamente como el cliente lo
sabe. Poner NOT NULL sin default habria obligado a inventarse un valor para filas que
representan algo real: archivos en claro.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("kdf_salt", sa.String(64), nullable=False, server_default=""),
    )
    op.add_column(
        "files",
        sa.Column("wrapped_key", sa.String(255), nullable=False, server_default=""),
    )
    op.add_column(
        "files",
        sa.Column("key_algo", sa.String(32), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("files", "key_algo")
    op.drop_column("files", "wrapped_key")
    op.drop_column("users", "kdf_salt")
