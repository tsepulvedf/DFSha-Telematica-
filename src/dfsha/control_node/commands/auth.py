"""Alta de usuarios y emision de tokens."""

from __future__ import annotations

from dfsha.common.errors import AlreadyExistsError, AuthenticationError
from dfsha.common.crypto import new_salt
from dfsha.control_node.domain.entities import Directory, User, utcnow
from dfsha.control_node.repositories.sql import SqlUnitOfWork, new_id
from dfsha.control_node.services.auth import (
    create_access_token,
    hash_password,
    verify_password,
)
from dfsha.control_node.tracing import command, query

__all__ = ["register_user", "login"]


@command("auth.register")
def register_user(uow: SqlUnitOfWork, username: str, password: str) -> str:
    """Crea el usuario y su arbol, en la misma transaccion.

    Un usuario sin raiz no podria hacer nada: ni `ls /` funcionaria. Por eso las dos
    filas entran o no entran juntas.
    """
    with uow:
        if uow.users.get_by_username(username) is not None:
            raise AlreadyExistsError("ese nombre de usuario ya esta tomado")

        ahora = utcnow()
        usuario = User(
            id=new_id(),
            username=username,
            password_hash=hash_password(password),
            # La sal del KDF se genera AQUI y se guarda, no en el cliente: si la
            # generara el cliente, dos sesiones del mismo usuario derivarian claves
            # distintas y la segunda no podria abrir lo que subio la primera.
            #
            # No es secreta y se devuelve en el login. Lo que la hace util es que sea
            # distinta por usuario, no que este escondida.
            kdf_salt=new_salt().hex(),
            created_at=ahora,
        )
        uow.users.add(usuario)
        uow.directories.add(
            Directory(
                id=new_id(),
                parent_id=None,  # raiz del usuario
                name="",
                owner_id=usuario.id,
                created_at=ahora,
            )
        )
        uow.commit()
        return usuario.id


@query("auth.login")
def login(
    uow: SqlUnitOfWork, username: str, password: str, secret: str, ttl_seconds: int
) -> tuple[str, int, str]:
    """Devuelve `(token, expires_in, kdf_salt)`.

    El mismo mensaje para usuario inexistente y contrasena incorrecta: distinguirlos
    convertiria el login en un oraculo de que cuentas existen.

    **La sal viaja en la respuesta, y no pasa nada.** Una sal no es un secreto: su unico
    trabajo es que dos usuarios con la misma contrasena no compartan clave y que no se
    puedan precalcular tablas contra todo el sistema. Con la sal y sin la contrasena no
    se deriva nada. Y el cliente la necesita justo aqui, porque sin ella no puede
    reconstruir su clave maestra en una sesion nueva.
    """
    with uow:
        usuario = uow.users.get_by_username(username)
        if usuario is None or not verify_password(password, usuario.password_hash):
            raise AuthenticationError("usuario o contrasena incorrectos")

        token, expira = create_access_token(
            usuario.id, usuario.username, secret, ttl_seconds
        )
        return token, expira, usuario.kdf_salt
