"""Conceder, revocar y administrar grupos.

Todo lo que escribe permisos pasa por aqui, y todo exige `ADMIN` sobre el directorio, que
se comprueba con la **misma** funcion que usa el resto del sistema (`permissions.require`).
Un modulo de permisos que comprobara sus propios permisos con su propia logica seria el
sitio mas facil de equivocarse y el menos probable de revisar.
"""

from __future__ import annotations

from dfsha.common.errors import (
    AlreadyExistsError,
    InvalidPathError,
    NotFoundError,
)
from dfsha.common.logging import get_logger
from dfsha.control_node.domain.acl import Permission, PrincipalType
from dfsha.control_node.domain.entities import utcnow
from dfsha.control_node.domain.path import Path
from dfsha.control_node.repositories.sql import SqlUnitOfWork
from dfsha.control_node.services.access import directory_for
from dfsha.control_node.tracing import command

__all__ = [
    "create_group",
    "add_member",
    "remove_member",
    "share",
    "unshare",
]


# --- Grupos -----------------------------------------------------------------


@command("acl.group_create")
def create_group(uow: SqlUnitOfWork, owner_id: str, name: str) -> str:
    """Crea un grupo. El nombre es unico POR DUENO, no globalmente."""
    limpio = name.strip()
    if not limpio or "/" in limpio:
        raise InvalidPathError("nombre de grupo invalido", name=name)

    with uow:
        if uow.acl.find_group(owner_id, limpio) is not None:
            raise AlreadyExistsError("ya tienes un grupo con ese nombre", name=limpio)
        group_id = uow.acl.create_group(limpio, owner_id, utcnow())
        uow.commit()

    get_logger("control_node").info(
        "acl.group_created", group_id=group_id, name=limpio, owner=owner_id
    )
    return group_id


def _grupo_propio(uow: SqlUnitOfWork, owner_id: str, name: str):
    grupo = uow.acl.find_group(owner_id, name.strip())
    if grupo is None:
        # 404 y no 403: decir "ese grupo es de otro" confirmaria que existe.
        raise NotFoundError("no existe ese grupo", name=name)
    return grupo


@command("acl.group_add")
def add_member(uow: SqlUnitOfWork, owner_id: str, group_name: str, username: str) -> None:
    with uow:
        grupo = _grupo_propio(uow, owner_id, group_name)
        usuario = uow.users.get_by_username(username.strip())
        if usuario is None:
            raise NotFoundError("no existe ese usuario", username=username)

        anadido = uow.acl.add_member(grupo.id, usuario.id, utcnow())
        uow.commit()

    if anadido:
        get_logger("control_node").info(
            "acl.group_member_added", group=group_name, user=username
        )


@command("acl.group_remove")
def remove_member(
    uow: SqlUnitOfWork, owner_id: str, group_name: str, username: str
) -> None:
    with uow:
        grupo = _grupo_propio(uow, owner_id, group_name)
        usuario = uow.users.get_by_username(username.strip())
        if usuario is None:
            raise NotFoundError("no existe ese usuario", username=username)

        quitado = uow.acl.remove_member(grupo.id, usuario.id)
        uow.commit()

    if quitado:
        get_logger("control_node").info(
            "acl.group_member_removed", group=group_name, user=username
        )


# --- Concesiones ------------------------------------------------------------


def _principal(uow: SqlUnitOfWork, actor_id: str, nombre: str) -> tuple[PrincipalType, str]:
    """Traduce un nombre a (tipo, id).

    Se busca primero como GRUPO propio y luego como usuario. El orden importa y es
    arbitrario, asi que se avisa: si alguien tiene un grupo con el mismo nombre que un
    usuario, gana el grupo. Prefijar con `@` seria mas explicito; se deja el nombre a
    secas porque es lo que pide el enunciado, y el caso de colision es raro y visible en
    `dfsha acl`, que dice de que tipo es cada concesion.
    """
    limpio = nombre.strip()
    grupo = uow.acl.find_group(actor_id, limpio)
    if grupo is not None:
        return PrincipalType.GROUP, grupo.id

    usuario = uow.users.get_by_username(limpio)
    if usuario is not None:
        return PrincipalType.USER, usuario.id

    raise NotFoundError("no existe ese usuario ni grupo", principal=nombre)


@command("acl.share")
def share(
    uow: SqlUnitOfWork,
    actor_id: str,
    raw_path: str,
    principal: str,
    permission: str,
) -> None:
    """Concede un permiso sobre un directorio.

    Exige `ADMIN` sobre ese directorio, comprobado con la misma funcion que todo lo demas.
    Conceder otra vez al mismo principal **actualiza** el permiso en vez de anadir una
    concesion nueva: si se acumularan, bajar un permiso no bajaria nada, porque el
    efectivo es el maximo.
    """
    path = Path.parse(raw_path)
    permiso = Permission.parse(permission)

    with uow:
        # `directory_for` resuelve Y autoriza en un paso, y entiende
        # `/compartido-conmigo/...`. Con el resolver de las etapas anteriores, alguien a
        # quien le dieron ADMIN sobre un directorio ajeno no podria ni nombrarlo para
        # repartirlo, que es justo lo que ADMIN significa.
        directorio = directory_for(uow, actor_id, path, Permission.ADMIN).directory

        tipo, principal_id = _principal(uow, actor_id, principal)
        if tipo is PrincipalType.USER and principal_id == actor_id:
            raise InvalidPathError(
                "no tiene sentido compartir contigo mismo: ya eres el dueno",
                principal=principal,
            )

        uow.acl.grant(
            directory_id=directorio.id,
            principal_type=int(tipo),
            principal_id=principal_id,
            permission=int(permiso),
            granted_by=actor_id,
            now=utcnow(),
        )
        uow.commit()

    get_logger("control_node").info(
        "acl.granted",
        path=str(path),
        principal=principal,
        principal_type=tipo.name,
        permission=permiso.name,
        granted_by=actor_id,
    )


@command("acl.unshare")
def unshare(uow: SqlUnitOfWork, actor_id: str, raw_path: str, principal: str) -> None:
    """Quita una concesion. Idempotente: quitar lo que no estaba no es un error.

    **Lo que ya se bajo no se recupera.** Si el principal ya habia descargado los
    archivos, revocar no los borra de su disco — es lo que pasa con cualquier sistema de
    permisos, y con cifrado extremo a extremo es ademas irreversible: la clave del
    directorio ya la tuvo. Ver la simplificacion documentada en CLAUDE.md.
    """
    path = Path.parse(raw_path)

    with uow:
        directorio = directory_for(uow, actor_id, path, Permission.ADMIN).directory

        tipo, principal_id = _principal(uow, actor_id, principal)
        quitado = uow.acl.revoke(directorio.id, int(tipo), principal_id)
        uow.commit()

    if quitado:
        get_logger("control_node").info(
            "acl.revoked", path=str(path), principal=principal, revoked_by=actor_id
        )
