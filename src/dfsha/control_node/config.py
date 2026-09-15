"""Configuracion del ControlNode.

Todo por variables de entorno. Los secretos no tienen valor por defecto: si faltan, el
proceso no arranca. Un secreto por defecto en un repositorio publico es un hallazgo de
seguridad, y este proyecto se evalua justo en eso.
"""

from __future__ import annotations

import sys
from functools import lru_cache

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["ControlNodeSettings", "get_settings", "load_settings_or_exit"]

#: Longitud minima de un secreto. No evita que alguien use "aaaa...", pero si que se
#: cuele un "cambiame" o el valor vacio de .env.example.
MIN_SECRET_LENGTH = 16


class ControlNodeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DFSHA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    block_size: int = Field(default=64 * 1024 * 1024, gt=0)
    db_url: str = "sqlite:///./dfsha.db"

    # Sin default a proposito: son obligatorios.
    jwt_secret: str
    internal_secret: str

    jwt_ttl_seconds: int = Field(default=3600, gt=0)
    write_ttl_seconds: int = Field(default=600, gt=0)
    log_level: str = "INFO"

    @field_validator("jwt_secret", "internal_secret")
    @classmethod
    def _secreto_con_cuerpo(cls, value: str) -> str:
        if len(value.strip()) < MIN_SECRET_LENGTH:
            raise ValueError(
                f"debe tener al menos {MIN_SECRET_LENGTH} caracteres; genera uno con "
                "python -c \"import secrets; print(secrets.token_urlsafe(48))\""
            )
        return value


def load_settings_or_exit() -> ControlNodeSettings:
    """Carga la configuracion o termina el proceso con un mensaje util.

    Sin esto, faltar `DFSHA_JWT_SECRET` produce un volcado de pydantic en medio del
    arranque de Uvicorn que no dice que hacer.
    """
    try:
        return ControlNodeSettings()
    except ValidationError as exc:
        print("ControlNode: configuracion invalida.", file=sys.stderr)
        for error in exc.errors():
            variable = "DFSHA_" + str(error["loc"][0]).upper()
            motivo = error["msg"].removeprefix("Value error, ")
            print(f"  - {variable}: {motivo}", file=sys.stderr)
        print(
            "\nCopia .env.example a .env y completa los valores que faltan.",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc


@lru_cache(maxsize=1)
def get_settings() -> ControlNodeSettings:
    return load_settings_or_exit()
