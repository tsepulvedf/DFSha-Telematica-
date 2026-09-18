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
from dfsha.control_node.domain.acl import Permission
from dfsha.control_node.services.access import directory_for, entry_for
from dfsha.control_node.services.shared import (
    SHARED_ROOT,
    _VirtualRoot,
    _VirtualUser,
    shared_entries,
)
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
    #: Copias del bloque PEOR replicado del archivo. Es el minimo y no la media a
    #: proposito: la durabilidad de un archivo la marca su bloque mas debil, y una media
    #: de 2,9 sobre 3 esconde que hay un bloque con una sola copia.
    min_replicas: int | None = None
    #: Copias del bloque mejor replicado. Solo para poder decir "2-3 de 3" cuando la
    #: re-replicacion va por la mitad, en vez de dar un numero plano que parece estancado.
    max_replicas: int | None = None
    replication_factor: int | None = None

    @property
    def replication_state(self) -> str | None:
        """Derivado, nunca almacenado.

        Misma disciplina que el estado de un DataNode en la Etapa 2: guardarlo en una
        columna obliga a que alguien se acuerde de actualizarla cuando una
        re-replicacion termina, y el dia que se olvide el archivo se quedara marcado
        sub-replicado para siempre sin que nada lo desmienta.
        """
        if self.type != "file" or self.min_replicas is None:
            return None
        if self.replication_factor and self.min_replicas >= self.replication_factor:
            return "FULLY_REPLICATED"
        if self.min_replicas == 0:
            # Ningun bloque localizable. No deberia pasar en un archivo COMMITTED, pero
            # si todos sus nodos perdieron el disco a la vez, esto es lo que se ve.
            return "UNAVAILABLE"
        return "UNDER_REPLICATED"


@query("fs.ls")
def ls(uow: SqlUnitOfWork, owner_id: str, raw_path: str) -> list[Entry]:
    path = Path.parse(raw_path)

    with uow:
        # Los dos niveles virtuales de `/compartido-conmigo` no son directorios reales,
        # asi que `resolve_scope` avisa con una excepcion y se listan aqui.
        try:
            resuelto = directory_for(uow, owner_id, path, Permission.READ)
        except _VirtualRoot:
            return _listar_quien_comparte(uow, owner_id)
        except _VirtualUser as virtual:
            return _listar_lo_compartido_por(virtual)

        directorio = resuelto.directory

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
def stat(
    uow: SqlUnitOfWork, owner_id: str, raw_path: str, replication_factor: int = 1
) -> Stat:
    path = Path.parse(raw_path)

    with uow:
        encontrado = entry_for(uow, owner_id, path, Permission.READ)

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

        # Aqui si se cuentan filas: el numero de copias de cada bloque es justo lo que
        # no se puede derivar del tamano.
        copias = uow.blocks.stored_replica_counts(archivo.id)

        return Stat(
            path=str(path),
            type="file",
            size=archivo.size,
            created_at=archivo.created_at,
            block_size=archivo.block_size,
            # Derivado del tamano en vez de contar filas: es el mismo numero, porque el
            # plan de bloques se genero con esta misma funcion, y ahorra una consulta.
            block_count=block_count_for(archivo.size, archivo.block_size),
            min_replicas=min(copias.values()) if copias else 0,
            max_replicas=max(copias.values()) if copias else 0,
            replication_factor=replication_factor,
        )


def _listar_quien_comparte(uow: SqlUnitOfWork, user_id: str) -> list[Entry]:
    """`ls /compartido-conmigo`: un directorio por cada persona que te comparte algo.

    Se agrupa por usuario y no se listan los directorios directamente porque dos personas
    pueden compartirte cosas que se llamen igual. Con el usuario delante, siguen siendo
    rutas distintas sin que nadie tenga que renombrar nada.
    """
    ahora = utcnow()
    duenos = {e.owner_username for e in shared_entries(uow, user_id)}
    return [
        Entry(name=username, type="directory", size=0, created_at=ahora)
        for username in sorted(duenos)
    ]


def _listar_lo_compartido_por(virtual: "_VirtualUser") -> list[Entry]:
    """`ls /compartido-conmigo/ana`: lo que esa persona te comparte."""
    return [
        Entry(
            name=entrada.share_name,
            type="directory",
            size=0,
            created_at=entrada.directory.created_at,
        )
        for entrada in virtual.entradas
    ]
