"""Bloqueo de archivos por lease, y bloques desligados por el append

El RF3. La tabla guarda locks VIVOS y VENCIDOS indistintamente, y eso es deliberado: el
vencimiento se evalua al consultar, nunca con un barrido en background. Es la regla
transversal de la seccion 1 —comprobaciones perezosas— y la misma que ya rige las reservas
de escritura.

Dos columnas que no son adorno:

- `epoch`: token de aislamiento, igual que el del liderazgo. SOLO SUBE. Es lo que impide
  que un cliente congelado despierte pasado el vencimiento y escriba encima de quien tomo
  el lock despues. Sin ella, el lock no sirve de nada en el unico caso que importa.
- `expires_at`: sin vencimiento, el primer cliente que se caiga sin soltar el lock deja el
  archivo bloqueado para siempre y hace falta un administrador.

`uq_file_locks_file_holder` —un lock por (archivo, titular)— hace que pedir dos veces el
mismo lock sea una renovacion y no una fila nueva. Sin ella, un cliente que reintenta tras
un timeout de red se dejaria filas sueltas que cuentan como titulares distintos, y un
EXCLUSIVE acabaria en conflicto consigo mismo.

**No hay clave foranea al usuario a proposito.** El titular es una SESION, no una persona:
Ana desde dos maquinas son dos titulares, y tiene que ser asi o el lock no excluiria nada
entre sus propios procesos.

## Y una segunda cosa, que va aqui porque la exige el mismo RF3

`blocks.file_id` pasa a ser NULLABLE. Lo obliga el `append`: cuando el ultimo bloque de un
archivo esta a medias, anadir datos lo REESCRIBE, y como los bloques son inmutables
(decision 1) el nuevo nace con otro `block_id` y ocupa el mismo indice. El viejo tiene que
salir de `UNIQUE(file_id, index)` sin dejar de existir, porque sus `block_replicas` son lo
unico que sabe en que discos estan sus bytes. Borrar la fila dejaria los bytes en disco sin
que nadie supiera donde: exactamente el huerfano que el GC existe para evitar.

Con esto, «huerfano» gana su tercer y mas puro caso: **un bloque que no pertenece a ningun
archivo**. Los otros dos —archivo DELETED y reserva vencida— siguen igual.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ID_LEN = 36


def upgrade() -> None:
    op.create_table(
        "file_locks",
        sa.Column("id", sa.String(ID_LEN), nullable=False),
        sa.Column("file_id", sa.String(ID_LEN), nullable=False),
        # Identificador de SESION, no de usuario. Ver el docstring.
        sa.Column("holder_id", sa.String(ID_LEN), nullable=False),
        # Quien es, para poder decirlo en el mensaje de conflicto: «lo tiene ana».
        sa.Column("holder_name", sa.String(64), nullable=False, server_default=""),
        # "shared" | "exclusive"
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("epoch", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_file_locks"),
        sa.UniqueConstraint("file_id", "holder_id", name="uq_file_locks_file_holder"),
    )
    # La consulta de todo el mecanismo es «dame los locks de este archivo», y ademas se
    # hace con FOR UPDATE en cada adquisicion y en cada escritura: sin indice, cada
    # `append` recorreria la tabla entera.
    op.create_index("ix_file_locks_file", "file_locks", ["file_id"])

    # batch_alter_table y no alter_column a secas: SQLite no sabe alterar una columna y
    # alembic lo resuelve recreando la tabla. En PostgreSQL es un ALTER normal.
    with op.batch_alter_table("blocks") as batch:
        batch.alter_column("file_id", existing_type=sa.String(ID_LEN), nullable=True)


def downgrade() -> None:
    # Los bloques desligados no tienen archivo al que volver: se borran sus filas. Los
    # bytes siguen en disco y ya no los recoge nadie, asi que conviene pasar el GC ANTES
    # de bajar esta revision.
    op.execute("DELETE FROM blocks WHERE file_id IS NULL")
    with op.batch_alter_table("blocks") as batch:
        batch.alter_column("file_id", existing_type=sa.String(ID_LEN), nullable=False)

    op.drop_index("ix_file_locks_file", table_name="file_locks")
    op.drop_table("file_locks")
