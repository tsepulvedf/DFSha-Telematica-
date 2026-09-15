"""Contrasenas y tokens.

Contrasenas con bcrypt, nunca en claro ni reversibles. Tokens JWT firmados con
`DFSHA_JWT_SECRET`, con `sub` = id de usuario: el ControlNode es stateless y no guarda
sesiones, asi que el token es lo unico que identifica al llamador.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
from passlib.context import CryptContext

from dfsha.common.errors import AuthenticationError

__all__ = [
    "MAX_PASSWORD_BYTES",
    "hash_password",
    "verify_password",
    "create_access_token",
    "decode_access_token",
    "TokenClaims",
]

#: bcrypt solo mira los primeros 72 bytes. Se rechaza lo que pase de ahi en vez de
#: truncar en silencio: truncar convertiria dos contrasenas largas distintas con el mismo
#: prefijo en la misma credencial.
MAX_PASSWORD_BYTES = 72

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

_ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    _validar_longitud(password)
    return _pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        return False
    return _pwd_context.verify(password, password_hash)


def _validar_longitud(password: str) -> None:
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise AuthenticationError(
            f"la contrasena no puede pasar de {MAX_PASSWORD_BYTES} bytes"
        )


class TokenClaims:
    """Lo que el ControlNode sabe del llamador tras validar el token."""

    __slots__ = ("user_id", "username", "expires_at")

    def __init__(self, user_id: str, username: str, expires_at: datetime) -> None:
        self.user_id = user_id
        self.username = username
        self.expires_at = expires_at


def create_access_token(
    user_id: str, username: str, secret: str, ttl_seconds: int
) -> tuple[str, int]:
    """Devuelve `(token, expires_in)`."""
    ahora = datetime.now(timezone.utc)
    expira = ahora + timedelta(seconds=ttl_seconds)
    token = jwt.encode(
        {
            "sub": user_id,
            "username": username,
            "iat": int(ahora.timestamp()),
            "exp": int(expira.timestamp()),
        },
        secret,
        algorithm=_ALGORITHM,
    )
    return token, ttl_seconds


def decode_access_token(token: str, secret: str) -> TokenClaims:
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[_ALGORITHM],  # lista fija: nada de leer 'alg' del propio token
            options={"require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("el token vencio; vuelve a iniciar sesion") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError("token invalido") from exc

    return TokenClaims(
        user_id=payload["sub"],
        username=payload.get("username", ""),
        expires_at=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
    )
