"""Lado escritura del namespace: mkdir, rmdir, rm, mv.

Cada caso de uso abre una unidad de trabajo, consulta lo que necesita, deja que el
dominio decida y confirma. Ninguna de estas funciones sabe que existe HTTP.
"""

from __future__ import annotations

from dfsha.common.errors import (
    AlreadyExistsError,
    InvalidPathError,
    NotFoundError,
)
from dfsha.control_node.domain.entities import Directory, utcnow
from dfsha.control_node.domain.path import Path
from dfsha.control_node.domain.rules import (
    MoveKind,
    decide_move,
    ensure_directory_is_empty,
    ensure_move_is_legal,
    ensure_name_is_free,
    ensure_visible,
)
from dfsha.control_node.domain.acl import Permission
from dfsha.control_node.repositories.sql import SqlUnitOfWork, new_id
from dfsha.control_node.services.access import directory_for, entry_for
from dfsha.control_node.services.permissions import require
from dfsha.control_node.services.resolver import find_directory
from dfsha.control_node.services.shared import resolve_scope
from dfsha.control_node.tracing import command

__all__ = ["mkdir", "rmdir", "rm", "mv"]


@command("mkdir")
def mkdir(uow: SqlUnitOfWork, owner_id: str, raw_path: str, parents: bool = False) -> None:
    path = Path.parse(raw_path)
    if path.is_root:
        raise AlreadyExistsError("la raiz ya existe", path=str(path))

    with uow:
        # `mkdir` arranca en la raiz del SCOPE, que para una ruta normal es la raiz del
        # usuario y para una compartida es el directorio que le compartieron. Asi crear
        # dentro de lo ajeno funciona igual que dentro de lo propio, con el permiso
        # comprobado abajo.
        alcance = resolve_scope(uow, owner_id, path)
        actual = alcance.start
        require(uow, owner_id, actual, Permission.WRITE, path=raw_path)
        ahora = utcnow()

        for indice, nombre in enumerate(path.segments):
            es_ultimo = indice == len(path.segments) - 1
            siguiente = uow.directories.get_child(actual.id, nombre)

            if siguiente is not None:
                if es_ultimo and not parents:
                    raise AlreadyExistsError(
                        "ya existe el directorio", path=str(path)
                    )
                actual = siguiente
                continue

            if not es_ultimo and not parents:
                raise NotFoundError(
                    "no existe el directorio padre; usa parents=true para crearlo",
                    path=str(Path(path.segments[: indice + 1])),
                )

            # Un archivo con ese nombre bloquea la creacion del directorio.
            ensure_name_is_free(
                Path(path.segments[: indice + 1]),
                uow.files.get_live_by_name(actual.id, nombre),
                ahora,
            )

            nuevo = Directory(
                id=new_id(),
                parent_id=actual.id,
                name=nombre,
                owner_id=owner_id,
                created_at=ahora,
            )
            uow.directories.add(nuevo)
            actual = nuevo

        uow.commit()


@command("rmdir")
def rmdir(uow: SqlUnitOfWork, owner_id: str, raw_path: str, recursive: bool = False) -> None:
    path = Path.parse(raw_path)
    if path.is_root:
        raise InvalidPathError("no se puede borrar la raiz")

    with uow:
        # Borrar un directorio es escribir en su padre. Se pide WRITE sobre el propio
        # directorio, que por la herencia hacia arriba implica tenerlo en el padre salvo
        # que alguien haya puesto una regla mas fina ahi, que es justo lo que se quiere
        # respetar.
        directorio = directory_for(uow, owner_id, path, Permission.WRITE).directory
        ensure_directory_is_empty(
            path, uow.directories.count_children(directorio.id), recursive
        )
        ahora = utcnow()

        a_borrar = [directorio, *uow.directories.list_descendants(directorio.id)]
        ids = [d.id for d in a_borrar]

        # Los archivos se marcan DELETED, no se borran: sus bloques siguen en disco hasta
        # que alguien corra el GC, y estas filas son lo que le dice al GC que ir a buscar.
        uow.files.mark_many_deleted(uow.files.list_ids_in_directories(ids), ahora)
        uow.directories.mark_many_deleted(ids, ahora)

        uow.commit()


@command("rm")
def rm(uow: SqlUnitOfWork, owner_id: str, raw_path: str) -> None:
    path = Path.parse(raw_path)

    with uow:
        encontrado = entry_for(uow, owner_id, path, Permission.WRITE)
        if encontrado.is_directory:
            raise InvalidPathError("es un directorio; usa rmdir", path=str(path))

        archivo = ensure_visible(path, encontrado.file)
        uow.files.mark_deleted(archivo.id, utcnow())
        uow.commit()


@command("mv")
def mv(uow: SqlUnitOfWork, owner_id: str, raw_src: str, raw_dst: str) -> None:
    """Metadato puro. No se transfiere un solo byte: los `block_id` no cambian, y por eso
    mover un archivo de 50 GB cuesta lo mismo que mover uno de 50 bytes."""
    src = Path.parse(raw_src)
    dst = Path.parse(raw_dst)

    with uow:
        # Mover exige WRITE en los DOS extremos: se quita algo de un sitio y se pone en
        # otro. Con permiso solo en el destino se podria sacar de un directorio ajeno.
        origen = entry_for(uow, owner_id, src, Permission.WRITE)
        if not origen.exists:
            raise NotFoundError("no existe el origen", path=str(src))
        if origen.file is not None:
            ensure_visible(src, origen.file)

        destino = entry_for(uow, owner_id, dst, Permission.WRITE)
        if destino.is_directory:
            clase = MoveKind.DIRECTORY
        elif destino.file is not None and destino.file.holds_name():
            clase = MoveKind.FILE
        else:
            clase = MoveKind.MISSING

        decision = decide_move(src, dst, clase)
        ensure_move_is_legal(src, decision, src_is_directory=origen.is_directory)

        padre = find_directory(uow.directories, owner_id, decision.parent)
        if padre is None:
            raise NotFoundError(
                "no existe el directorio destino", path=str(decision.parent)
            )

        # El nombre final tiene que estar libre incluso cuando el destino era un
        # directorio: mover /a/x a /b falla si /b/x ya existe.
        if uow.directories.get_child(padre.id, decision.name) is not None:
            raise AlreadyExistsError("ya existe una entrada con ese nombre",
                                     path=str(decision.target))
        ensure_name_is_free(
            decision.target, uow.files.get_live_by_name(padre.id, decision.name)
        )

        if origen.is_directory:
            uow.directories.move(origen.directory.id, padre.id, decision.name)
        else:
            uow.files.move(origen.file.id, padre.id, decision.name)

        uow.commit()
