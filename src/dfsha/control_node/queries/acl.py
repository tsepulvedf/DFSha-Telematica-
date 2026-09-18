"""Lado lectura de los permisos: quien tiene que sobre una ruta, y que me comparten.

`acl_of` devuelve **dos cosas distintas** y conviene no confundirlas: el permiso
EFECTIVO de quien pregunta —ya resuelto, con herencia y grupos— y las concesiones puestas
en ese directorio concreto. La primera responde «¿que puedo hacer aqui?»; la segunda,
«¿quien mas puede?». Mezclarlas seria la forma mas rapida de que nadie entendiera ninguna.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from dfsha.control_node.domain.acl import Permission, PrincipalType
from dfsha.control_node.domain.path import Path
from dfsha.control_node.repositories.sql import SqlUnitOfWork
from dfsha.control_node.services.access import directory_for
from dfsha.control_node.services.resolver import absolute_path
from dfsha.control_node.services.shared import SHARED_ROOT, shared_entries
from dfsha.control_node.tracing import query

__all__ = ["AclView", "GrantView", "SharedView", "acl_of", "shared_with_me", "groups_of"]


@dataclass(frozen=True, slots=True)
class GrantView:
    principal: str
    principal_type: str
    permission: str
    granted_by: str
    granted_at: datetime


@dataclass(frozen=True, slots=True)
class AclView:
    path: str
    effective: str | None
    source: str | None
    inherited_from: str | None
    grants: list[GrantView]


@dataclass(frozen=True, slots=True)
class SharedView:
    owner: str
    name: str
    permission: str
    via_group: str
    path: str


def _nombre_de(uow: SqlUnitOfWork, tipo: int, principal_id: str) -> str:
    if tipo == PrincipalType.GROUP.value:
        grupo = uow.acl.get_group(principal_id)
        return grupo.name if grupo else principal_id
    usuario = uow.users.get(principal_id)
    return usuario.username if usuario else principal_id


@query("acl.show")
def acl_of(uow: SqlUnitOfWork, user_id: str, raw_path: str) -> AclView:
    """Lo que puede quien pregunta, y quien mas tiene concesiones aqui.

    Ver las concesiones ajenas exige READ sobre el directorio: quien puede leerlo ya sabe
    que existe, asi que saber con quien mas se comparte no revela nada nuevo sobre el
    arbol. Lo que si revela son nombres de otros usuarios, y por eso no se expone a quien
    no tiene ningun permiso: ese ni llega aqui, porque `directory_for` le da 404.
    """
    path = Path.parse(raw_path)

    with uow:
        resuelto = directory_for(uow, user_id, path, Permission.READ)
        directorio = resuelto.directory
        grant = resuelto.access.grant

        heredado = None
        if grant is not None and grant.source != "owner" and grant.directory_id != directorio.id:
            heredado = str(absolute_path(uow.directories, grant.directory_id))

        concesiones = [
            GrantView(
                principal=_nombre_de(uow, fila.principal_type, fila.principal_id),
                principal_type=PrincipalType(fila.principal_type).name,
                permission=Permission(fila.permission).name,
                granted_by=_nombre_de(uow, PrincipalType.USER.value, fila.granted_by),
                granted_at=fila.granted_at,
            )
            for fila in uow.acl.entries_on(directorio.id)
        ]

        return AclView(
            path=str(path),
            effective=grant.permission.name if grant else None,
            source=grant.source if grant else None,
            inherited_from=heredado,
            grants=sorted(concesiones, key=lambda g: (g.principal_type, g.principal)),
        )


@query("acl.shared_with_me")
def shared_with_me(uow: SqlUnitOfWork, user_id: str) -> list[SharedView]:
    """Lo que otros comparten con este usuario. NO se mezcla con su arbol propio."""
    with uow:
        return [
            SharedView(
                owner=e.owner_username,
                name=e.share_name,
                permission=e.permission.name,
                via_group=(
                    uow.acl.get_group(e.via_group).name
                    if e.via_group and uow.acl.get_group(e.via_group)
                    else ""
                ),
                path=f"/{SHARED_ROOT}/{e.owner_username}/{e.share_name}",
            )
            for e in shared_entries(uow, user_id)
        ]


@query("acl.groups")
def groups_of(uow: SqlUnitOfWork, owner_id: str) -> list[tuple[str, list[str]]]:
    """Los grupos que este usuario administra, con sus miembros."""
    with uow:
        salida = []
        for grupo in uow.acl.list_groups(owner_id):
            miembros = [
                (uow.users.get(uid).username if uow.users.get(uid) else uid)
                for uid in uow.acl.list_members(grupo.id)
            ]
            salida.append((grupo.name, sorted(miembros)))
        return salida
