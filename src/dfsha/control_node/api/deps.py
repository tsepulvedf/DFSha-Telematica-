"""Dependencias de FastAPI: configuracion, unidad de trabajo, identidad y plano interno.

Los routers no construyen nada: piden por inyeccion lo que necesitan. Eso es lo que
permite que las pruebas de integracion levanten la app real contra una base de datos
temporal sin parchear modulos.
"""

from __future__ import annotations

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
from dfsha.control_node.services.read_routing import ReadRouter

__all__ = [
    "get_settings_dep",
    "get_uow_factory",
    "get_uow",
    "get_read_router",
    "get_query_uow",
    "get_placement",
    "current_user",
    "Settings",
    "Uow",
    "QueryUow",
    "UowFactory",
    "Placement",
    "CurrentUser",
]

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


def get_read_router(request: Request) -> ReadRouter:
    return request.app.state.read_router


def get_query_uow(
    router: Annotated[ReadRouter, Depends(get_read_router)],
    x_dfsha_read_lsn: Annotated[str | None, Header()] = None,
) -> SqlUnitOfWork:
    """Unidad de trabajo para el LADO DE CONSULTA.

    Va a la replica salvo que el cliente traiga un LSN que la replica todavia no ha
    reproducido, en cuyo caso se atiende desde el primario. Ver `services/read_routing`.

    Que sea una dependencia distinta de `get_uow` y no un parametro de esta es lo que
    hace imposible que un comando acabe por error contra la replica: el tipo del
    parametro del endpoint dice a que lado pertenece.
    """
    return router.for_read(x_dfsha_read_lsn).uow


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


# El `require_internal_secret` de las Etapas 1 y 2 ya no existe. El plano interno vive
# ahora en un puerto propio con TLS mutuo, asi que la puerta la guarda el propio TLS:
# quien no presente un certificado firmado por la CA de DFSha no llega a enviar la
# peticion, y no hay ninguna comprobacion en el codigo que se pueda olvidar en una ruta
# nueva. Ver `common/tls.py` y `create_internal_app`.


Settings = Annotated[ControlNodeSettings, Depends(get_settings_dep)]
Uow = Annotated[SqlUnitOfWork, Depends(get_uow)]
QueryUow = Annotated[SqlUnitOfWork, Depends(get_query_uow)]
UowFactory = Annotated[Callable[[], SqlUnitOfWork], Depends(get_uow_factory)]
Placement = Annotated[BlockPlacementPolicy, Depends(get_placement)]
CurrentUser = Annotated[TokenClaims, Depends(current_user)]
