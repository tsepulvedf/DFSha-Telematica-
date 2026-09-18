"""Sal de cifrado para los usuarios creados antes de la 0006

La 0006 anadio `users.kdf_salt` con `server_default=""` y **no relleno las filas que ya
existian**. Una sal vacia hacia que el login no derivara clave maestra, y el `put` del
cliente, al no tener clave, **subia el archivo en claro sin decir nada**. Cualquier usuario
registrado antes del Bloque C se quedo asi para siempre: en la base del compose de
desarrollo, ningun archivo llego a cifrarse nunca, con el criterio 11 dado por cumplido y
todas las pruebas de cifrado en verde. Se descubrio con el guion de demostracion
`cifrado_en_reposo.py`, que encontro la frase en claro dentro de un `.blk`.

Las pruebas no lo vieron por dos motivos que conviene no confundir:

1. **Todas crean sus usuarios en el momento**, y un usuario recien registrado siempre tiene
   sal. El estado que produce una MIGRACION sobre datos existentes no lo monta nadie:
   `test_migracion_deja_el_mismo_esquema_que_los_modelos` compara el esquema, no las filas.
2. **Las de cifrado no pasan por el `put` del cliente**: reimplementan sus pasos y deciden
   ellas cifrar, asi que la rama que decidia NO cifrar no la recorria ninguna.

Esta revision da a cada usuario sin sal una propia, aleatoria y distinta, igual que la que
recibiria al registrarse. **Es seguro**: los archivos que ya subieron en claro tienen
`wrapped_key` vacia y se siguen bajando, porque el cliente mira ese campo en vez de
suponerlo; lo que cambia es que a partir del siguiente login sus subidas se cifran.

**Lo que NO hace**: re-cifrar lo que ya esta en claro. Eso exige la clave, que solo existe
en el cliente. Esos archivos siguen en claro en disco hasta que se vuelvan a subir.

La sal se genera en Python y no con SQL: `gen_random_bytes` es de `pgcrypto`, que no esta
instalada, y las pruebas migran sobre SQLite.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

import secrets
from typing import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Copiado y no importado de `dfsha.common.crypto.SALT_BYTES`: una migracion describe el
#: esquema de SU momento y no puede cambiar porque cambie el codigo de la aplicacion.
_SALT_BYTES = 16


def upgrade() -> None:
    conexion = op.get_bind()
    usuarios = sa.table("users", sa.column("id", sa.String), sa.column("kdf_salt", sa.String))
    sin_sal = conexion.execute(
        sa.select(usuarios.c.id).where(
            sa.or_(usuarios.c.kdf_salt == "", usuarios.c.kdf_salt.is_(None))
        )
    ).scalars().all()
    for user_id in sin_sal:
        conexion.execute(
            usuarios.update()
            .where(usuarios.c.id == user_id)
            .values(kdf_salt=secrets.token_bytes(_SALT_BYTES).hex())
        )


def downgrade() -> None:
    # Nada que deshacer: vaciar la sal dejaria sin abrir los archivos cifrados despues.
    pass
