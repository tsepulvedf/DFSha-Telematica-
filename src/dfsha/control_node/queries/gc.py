"""Consulta de bloques huerfanos para el recolector."""

from __future__ import annotations

from dataclasses import dataclass

from dfsha.control_node.domain.entities import utcnow
from dfsha.control_node.repositories.sql import SqlUnitOfWork
from dfsha.control_node.tracing import query

__all__ = ["OrphanBlockView", "orphan_blocks"]


@dataclass(frozen=True, slots=True)
class OrphanBlockView:
    block_id: str
    size: int
    replicas: list[tuple[str, str]]  # (data_node_id, base_url)


@query("gc.orphan_blocks")
def orphan_blocks(uow: SqlUnitOfWork) -> list[OrphanBlockView]:
    """Bloques de archivos DELETED y de reservas vencidas.

    Solo consulta: no marca ni borra nada. El metadato se toca en `/gc/confirm`, y solo
    despues de que el script haya comprobado que los bytes ya no estan en disco. Al
    reves, un fallo del script dejaria bloques invisibles ocupando disco para siempre.
    """
    with uow:
        ahora = utcnow()
        vistas: list[OrphanBlockView] = []

        for bloque, replicas in uow.blocks.list_orphans(ahora):
            nodos = [
                (r.data_node_id, nodo.advertise_url)
                for r in replicas
                if (nodo := uow.data_nodes.get(r.data_node_id)) is not None
            ]
            vistas.append(
                OrphanBlockView(block_id=bloque.block_id, size=bloque.size, replicas=nodos)
            )

        return vistas
