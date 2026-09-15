"""Lado lectura del namespace: ls y stat.

Paquete aparte de `commands/` a proposito. Hoy ambos golpean la misma base de datos, pero
la separacion es lo que permitira en la Etapa 3 apuntar estas consultas a una replica de
lectura sin tocar el lado de escritura. Las trazas lo reflejan: aqui se emite
`metadata.query` y alli `metadata.command`.

Nada de este paquete escribe. Ni siquiera la comprobacion perezosa de reservas vencidas:
una consulta que viera una reserva vencida se limita a ignorarla, y sera el proximo
`create` sobre ese nombre el que la marque DELETED.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from dfsha.common.errors import NotFoundError
from dfsha.control_node.domain.entities import utcnow
from dfsha.control_node.domain.partition import block_count_for
from dfsha.control_node.domain.path import Path
from dfsha.control_node.repositories.sql import SqlUnitOfWork
from dfsha.control_node.services.resolver import resolve_directory, resolve_entry
from dfsha.control_node.tracing import query

__all__ = ["Entry", "Stat", "ls", "stat"]


@dataclass(frozen=True, slots=True)
class Entry:
    name: str
    type: str  # "file" | "directory"
    size: int
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Stat:
    path: str
    type: str
    size: int
    created_at: datetime
    block_size: int | None = None
    block_count: int | None = None


@query("fs.ls")
def ls(uow: SqlUnitOfWork, owner_id: str, raw_path: str) -> list[Entry]:
    path = Path.parse(raw_path)

    with uow:
        directorio = resolve_directory(uow.directories, owner_id, path)

        entradas = [
            Entry(name=d.name, type="directory", size=0, created_at=d.created_at)
            for d in uow.directories.list_children(directorio.id)
        ]
        # list_committed ya deja fuera las reservas WRITING y los archivos borrados: un
        # archivo a medio subir no existe para el resto del mundo.
        entradas.extend(
            Entry(name=f.name, type="file", size=f.size, created_at=f.created_at)
            for f in uow.files.list_committed(directorio.id)
        )
        return sorted(entradas, key=lambda e: (e.type != "directory", e.name))


@query("fs.stat")
def stat(uow: SqlUnitOfWork, owner_id: str, raw_path: str) -> Stat:
    path = Path.parse(raw_path)

    with uow:
        encontrado = resolve_entry(uow.directories, uow.files, owner_id, path)

        if encontrado.directory is not None:
            return Stat(
                path=str(path),
                type="directory",
                size=0,
                created_at=encontrado.directory.created_at,
            )

        archivo = encontrado.file
        if archivo is None or not archivo.is_visible(utcnow()):
            raise NotFoundError("no existe la ruta", path=str(path))

        return Stat(
            path=str(path),
            type="file",
            size=archivo.size,
            created_at=archivo.created_at,
            block_size=archivo.block_size,
            # Derivado del tamano en vez de contar filas: es el mismo numero, porque el
            # plan de bloques se genero con esta misma funcion, y ahorra una consulta.
            block_count=block_count_for(archivo.size, archivo.block_size),
        )
