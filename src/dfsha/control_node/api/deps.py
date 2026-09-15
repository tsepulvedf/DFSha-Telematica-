"""Dependencias de FastAPI: configuracion, unidad de trabajo, identidad y plano interno.

Los routers no construyen nada: piden por inyeccion lo que necesitan. Eso es lo que
permite que las pruebas de integracion levanten la app real contra una base de datos
temporal sin parchear modulos.
"""

from __future__ import annotations

import secrets
from typing import Annotated, Callable

from fastapi import Depends, Header, Request

from dfsha.common.errors import AuthenticationError
from dfsha.control_node.config import ControlNodeSettings
from dfsha.control_node.repositories.sql import SqlUnitOfWork
from dfsha.control_node.services.auth import TokenClaims, decode_access_token
from dfsha.control_node.domain.membership import MembershipThresholds
from dfsha.control_node.services.placement import (
    BlockPlacementPolicy,
    LeastLoadedPlacement,
)

__all__ = [
    "INTERNAL_SECRET_HEADER",
    "get_settings_dep",
    "get_uow_factory",
    "get_uow",
    "get_placement",
    "current_user",
    "require_internal_secret",
    "Settings",
    "Uow",
    "UowFactory",
    "Placement",
    "CurrentUser",
]

INTERNAL_SECRET_HEADER = "X-DFSha-Internal-Secret"


def get_settings_dep(request: Request) -> ControlNodeSettings:
    return request.app.state.settings


def get_uow_factory(request: Request) -> Callable[[], SqlUnitOfWork]:
    return request.app.state.uow_factory


def get_uow(
    factory: Annotated[Callable[[], SqlUnitOfWork], Depends(get_uow_factory)],
) -> SqlUnitOfWork:
    """Una unidad de trabajo sin abrir.

    Quien la abre es el caso de uso, con su propio `with`: asi la transaccion dura
    exactamente lo que dura la operacion y no lo que dura la peticion HTTP.
    """
    return factory()


def get_placement(
    uow: Annotated[SqlUnitOfWork, Depends(get_uow)],
    settings: Annotated[ControlNodeSettings, Depends(get_settings_dep)],
) -> BlockPlacementPolicy:
    """La politica lee los DataNodes por la misma unidad de trabajo que el caso de uso,
    para que la eleccion vea el mismo estado que el resto de la operacion."""
    return LeastLoadedPlacement(
        uow.data_nodes,
        thresholds=MembershipThresholds.from_millis(
            settings.suspect_after_ms, settings.dead_after_ms
        ),
        d=settings.placement_d,
        min_free_bytes=settings.min_free_bytes,
    )


def current_user(
    settings: Annotated[ControlNodeSettings, Depends(get_settings_dep)],
    authorization: Annotated[str | None, Header()] = None,
) -> TokenClaims:
    """Valida el Bearer token. El ControlNode no guarda sesiones: el token es todo."""
    if not authorization:
        raise AuthenticationError("falta la cabecera Authorization")

    esquema, _, token = authorization.partition(" ")
    if esquema.lower() != "bearer" or not token:
        raise AuthenticationError("se esperaba 'Authorization: Bearer <token>'")

    return decode_access_token(token.strip(), settings.jwt_secret)


def require_internal_secret(
    settings: Annotated[ControlNodeSettings, Depends(get_settings_dep)],
    x_dfsha_internal_secret: Annotated[str | None, Header()] = None,
) -> None:
    """Protege `/internal/v1`, que solo deben tocar el DataNode y el GC.

    Comparacion en tiempo constante: con `==`, el tiempo de respuesta filtra cuantos
    caracteres iniciales acerto quien prueba. En la Etapa 3 esto pasa a mTLS.
    """
    if not x_dfsha_internal_secret or not secrets.compare_digest(
        x_dfsha_internal_secret, settings.internal_secret
    ):
        raise AuthenticationError("secreto interno invalido o ausente")


Settings = Annotated[ControlNodeSettings, Depends(get_settings_dep)]
Uow = Annotated[SqlUnitOfWork, Depends(get_uow)]
UowFactory = Annotated[Callable[[], SqlUnitOfWork], Depends(get_uow_factory)]
Placement = Annotated[BlockPlacementPolicy, Depends(get_placement)]
CurrentUser = Annotated[TokenClaims, Depends(current_user)]
