"""Comandos del plano interno: los que invocan el DataNode y el GC, no el cliente."""

from __future__ import annotations

from dfsha.common.errors import NotFoundError
from dfsha.control_node.domain.entities import utcnow
from dfsha.control_node.repositories.sql import SqlUnitOfWork
from dfsha.control_node.tracing import command

__all__ = ["register_data_node", "mark_block_stored", "confirm_gc"]


@command("internal.datanode_register")
def register_data_node(uow: SqlUnitOfWork, base_url: str, capacity_bytes: int) -> str:
    with uow:
        nodo = uow.data_nodes.register(base_url, capacity_bytes, utcnow())
        uow.commit()
        return nodo.id


@command("internal.block_stored")
def mark_block_stored(
    uow: SqlUnitOfWork,
    block_id: str,
    data_node_id: str,
    size: int,
    checksum_sha256: str,
) -> None:
    """Confirma que un bloque esta en disco en ese DataNode.

    Lo llama el DataNode antes de responder 201 al cliente, asi que cuando el cliente ve
    su bloque subido, el ControlNode ya lo sabe y el commit no puede fallar por una
    carrera entre ambos.
    """
    with uow:
        if uow.data_nodes.get(data_node_id) is None:
            raise NotFoundError("DataNode desconocido", data_node_id=data_node_id)

        if not uow.blocks.mark_stored(block_id, data_node_id, size, checksum_sha256):
            # El bloque ya no interesa a nadie (la reserva se abandono o se aborto). No es
            # un error del DataNode: sus bytes quedan en disco y el GC los recogera.
            raise NotFoundError("bloque desconocido en el metadato", block_id=block_id)

        uow.data_nodes.add_used_bytes(data_node_id, size)
        uow.commit()


@command("internal.gc_confirm")
def confirm_gc(uow: SqlUnitOfWork, block_ids: list[str]) -> int:
    """Borra del metadato los bloques que el GC ya elimino del disco.

    Se descuenta el tamano del `used_bytes` de cada nodo antes de borrar las filas: una
    vez borradas no habria forma de saber cuanto ocupaban ni donde estaban.
    """
    if not block_ids:
        return 0

    with uow:
        por_nodo: dict[str, int] = {}
        replicas = uow.blocks.list_replicas(block_ids)
        for block_id, bloque_replicas in replicas.items():
            tamano = uow.blocks.total_size([block_id])
            for replica in bloque_replicas:
                por_nodo[replica.data_node_id] = por_nodo.get(replica.data_node_id, 0) + tamano

        for data_node_id, bytes_liberados in por_nodo.items():
            uow.data_nodes.add_used_bytes(data_node_id, -bytes_liberados)

        borrados = uow.blocks.delete_blocks(block_ids)
        uow.commit()
        return borrados
