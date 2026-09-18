"""Comandos del plano interno REST: los que invocan el DataNode y el GC, no el cliente.

El registro del DataNode vivia aqui en la Etapa 1 y ahora esta en `control_plane.py`,
detras de gRPC. Lo que queda es lo que tiene que seguir siendo sincrono con la peticion
del cliente (la confirmacion de bloque) y lo que usa un script humano (el GC).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from dfsha.common.errors import NotFoundError
from dfsha.common.logging import get_logger
from dfsha.control_node.commands.rereplication import complete_for_block
from dfsha.control_node.domain.entities import utcnow
from dfsha.control_node.repositories.sql import SqlUnitOfWork
from dfsha.control_node.tracing import command

__all__ = ["mark_block_stored", "confirm_gc"]


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

    Tambien es por donde se cierra una re-replicacion: el nodo destino avisa igual que
    cualquier otro, sin un camino propio. Cerrar una tarea NO exige liderazgo, porque no
    es una decision sino registrar un hecho que ya ocurrio; ver `complete_for_block`.
    """
    with uow:
        if uow.data_nodes.get(data_node_id) is None:
            raise NotFoundError("DataNode desconocido", data_node_id=data_node_id)

        if not uow.blocks.mark_stored(block_id, data_node_id, size, checksum_sha256):
            # El bloque ya no interesa a nadie (la reserva se abandono o se aborto). No es
            # un error del DataNode: sus bytes quedan en disco y el GC los recogera.
            raise NotFoundError("bloque desconocido en el metadato", block_id=block_id)

        uow.data_nodes.add_used_bytes(data_node_id, size)
        complete_for_block(uow, block_id, data_node_id)
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


@dataclass(frozen=True, slots=True)
class GcDispatch:
    blocks: int
    orders: int
    skipped: int


@command("internal.gc_dispatch")
def dispatch_gc_deletions(
    uow: SqlUnitOfWork, ttl_seconds: int = 600
) -> GcDispatch:
    """Encola el borrado de cada huerfano en cada nodo que lo tenga.

    Una orden por (bloque, nodo): el DataNode que la recibe borra su copia y la anuncia
    como quitada en su siguiente block report. Como `DELETE` ya era idempotente desde la
    Etapa 1, borrar lo que ya no esta tampoco es un error aqui.

    No exige liderazgo: lo dispara una peticion externa del recolector, que es una accion
    deliberada de un operador, no una decision automatica del cluster. Lo que si exige
    liderazgo es lo que el cluster decide por su cuenta.
    """
    ahora = utcnow()
    vence = ahora + timedelta(seconds=ttl_seconds)
    bloques = orders = saltados = 0

    with uow:
        for bloque, replicas in uow.blocks.list_orphans(ahora):
            bloques += 1
            encolado_alguno = False
            for replica in replicas:
                task_id = uow.rereplication.enqueue_delete(
                    bloque.block_id, replica.data_node_id, ahora, vence
                )
                if task_id is not None:
                    orders += 1
                    encolado_alguno = True
            if not encolado_alguno and replicas:
                saltados += 1
        uow.commit()

    get_logger("control_node").info(
        "gc.dispatched",
        blocks=bloques,
        orders=orders,
        skipped=saltados,
        duration_ms=0.0,
    )
    return GcDispatch(blocks=bloques, orders=orders, skipped=saltados)
