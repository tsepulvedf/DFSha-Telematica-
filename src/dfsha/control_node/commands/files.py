"""Lado escritura de la transferencia: create, commit, abort.

La escritura tiene tres fases porque los bloques son inmutables (WORM). El ControlNode
reserva el plan, el cliente sube los bytes directamente a los DataNodes, y solo entonces
se confirma. Mientras tanto el archivo esta en WRITING: invisible para todos y con fecha
de caducidad, para que un cliente que se cae no deje el nombre bloqueado para siempre.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from dfsha.common.errors import InvalidPathError, NotFoundError
from dfsha.control_node.domain.entities import (
    Block,
    BlockReplica,
    File,
    FileState,
    ReplicaState,
    utcnow,
)
from dfsha.control_node.domain.partition import plan_blocks
from dfsha.control_node.domain.path import Path
from dfsha.control_node.domain.rules import (
    ensure_can_abort,
    ensure_can_commit,
    ensure_no_live_reservation,
)
from dfsha.control_node.repositories.sql import SqlUnitOfWork, new_id
from dfsha.control_node.services.placement import BlockPlacementPolicy
from dfsha.control_node.services.resolver import absolute_path, resolve_directory
from dfsha.control_node.tracing import command

__all__ = ["CreatedFile", "PlannedBlock", "CommittedFile", "create_file", "commit_file", "abort_file"]


@dataclass(frozen=True, slots=True)
class PlannedBlock:
    block_id: str
    index: int
    size: int
    replicas: list[tuple[str, str]]  # (data_node_id, base_url)


@dataclass(frozen=True, slots=True)
class CreatedFile:
    file_id: str
    block_size: int
    expires_at: object
    blocks: list[PlannedBlock]


@dataclass(frozen=True, slots=True)
class CommittedFile:
    path: str
    size: int
    block_count: int


@command("files.create")
def create_file(
    uow: SqlUnitOfWork,
    placement: BlockPlacementPolicy,
    owner_id: str,
    raw_path: str,
    size: int,
    default_block_size: int,
    write_ttl_seconds: int,
    block_size: int | None = None,
) -> CreatedFile:
    path = Path.parse(raw_path)
    if path.is_root:
        raise InvalidPathError("la raiz es un directorio, no un archivo")

    efectivo = block_size or default_block_size

    with uow:
        padre = resolve_directory(uow.directories, owner_id, path.parent)
        ahora = utcnow()

        existente = uow.files.get_live_by_name(padre.id, path.name)
        ensure_no_live_reservation(path, existente, ahora)
        if existente is not None and existente.state is FileState.WRITING:
            # Reserva vencida: se cierra aqui mismo para que no vuelva a aparecer, y sus
            # bloques quedan a la vista del GC.
            uow.files.mark_deleted(existente.id, ahora)

        if uow.directories.get_child(padre.id, path.name) is not None:
            raise InvalidPathError("ya existe un directorio con ese nombre", path=str(path))

        archivo = File(
            id=new_id(),
            directory_id=padre.id,
            name=path.name,
            owner_id=owner_id,
            size=size,
            block_size=efectivo,
            state=FileState.WRITING,
            created_at=ahora,
            expires_at=ahora + timedelta(seconds=write_ttl_seconds),
        )
        uow.files.add(archivo)

        bloques: list[Block] = []
        replicas: list[BlockReplica] = []
        planificados: list[PlannedBlock] = []

        for spec in plan_blocks(size, efectivo):
            block_id = new_id()
            # La colocacion se decide aqui y se registra: no se recalcula nunca por hash.
            destinos = placement.select(spec.size, replication_factor=1)
            bloques.append(
                Block(block_id=block_id, file_id=archivo.id, index=spec.index, size=spec.size)
            )
            replicas.extend(
                BlockReplica(
                    block_id=block_id,
                    data_node_id=nodo.id,
                    state=ReplicaState.PENDING,
                    created_at=ahora,
                )
                for nodo in destinos
            )
            planificados.append(
                PlannedBlock(
                    block_id=block_id,
                    index=spec.index,
                    size=spec.size,
                    replicas=[(nodo.id, nodo.base_url) for nodo in destinos],
                )
            )

        uow.blocks.add_plan(bloques, replicas)
        uow.commit()

        return CreatedFile(
            file_id=archivo.id,
            block_size=efectivo,
            expires_at=archivo.expires_at,
            blocks=planificados,
        )


@command("files.commit")
def commit_file(uow: SqlUnitOfWork, owner_id: str, file_id: str) -> CommittedFile:
    """Confirma la reserva y, si habia un archivo en esa ruta, lo retira.

    Las dos cosas ocurren en la misma transaccion. Si se hicieran en dos pasos, una caida
    entre ellos dejaria la ruta sin ningun archivo visible: el viejo ya retirado y el
    nuevo todavia sin confirmar.
    """
    with uow:
        archivo = uow.files.get(file_id)
        if archivo is None or archivo.owner_id != owner_id:
            raise NotFoundError("no existe la reserva", file_id=file_id)

        ahora = utcnow()
        ensure_can_commit(archivo, uow.blocks.pending_block_ids(file_id), ahora)

        anterior = uow.files.get_live_by_name(archivo.directory_id, archivo.name)
        if anterior is not None and anterior.id != archivo.id:
            if anterior.state is FileState.COMMITTED:
                # Copy-on-write: la version anterior sale de escena y sus bloques pasan a
                # ser huerfanos para el GC. Los bytes nuevos ya estan en disco.
                uow.files.mark_deleted(anterior.id, ahora)

        uow.files.mark_committed(file_id, ahora)
        uow.commit()

        ruta = absolute_path(uow.directories, archivo.directory_id).child(archivo.name)
        return CommittedFile(
            path=str(ruta),
            size=archivo.size,
            block_count=len(uow.blocks.list_for_file(file_id)),
        )


@command("files.abort")
def abort_file(uow: SqlUnitOfWork, owner_id: str, file_id: str) -> None:
    """Cancela una reserva. Idempotente: abortar dos veces no es un error.

    Los bloques ya subidos se quedan en disco hasta que se corra el GC; abortar no habla
    con los DataNodes.
    """
    with uow:
        archivo = uow.files.get(file_id)
        if archivo is None or archivo.owner_id != owner_id:
            raise NotFoundError("no existe la reserva", file_id=file_id)

        ensure_can_abort(archivo)
        if archivo.state is FileState.WRITING:
            uow.files.mark_deleted(file_id, utcnow())
            uow.commit()
