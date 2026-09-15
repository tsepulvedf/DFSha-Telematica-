"""Politica de colocacion de bloques.

El ControlNode ELIGE el DataNode de cada bloque y registra la eleccion en
`block_replicas`. No se calcula por hash del `block_id`: con hash, anadir un nodo
reubicaria bloques existentes y no habria forma de colocar dos replicas en dominios de
falla distintos.

La Etapa 1 trae una sola implementacion, `SingleNodePlacement`. La Etapa 2 anade *power
of d choices* implementando este mismo `select` y nada mas.
"""

from __future__ import annotations

import random
from typing import Protocol

from dfsha.common.errors import NoDataNodeAvailableError
from dfsha.control_node.domain.entities import DataNode
from dfsha.control_node.repositories.base import DataNodeRepository

__all__ = ["BlockPlacementPolicy", "SingleNodePlacement"]


class BlockPlacementPolicy(Protocol):
    """Un unico metodo, a proposito: es la costura por la que entra la Etapa 2."""

    def select(self, block_size: int, replication_factor: int) -> list[DataNode]:
        """Devuelve los nodos donde colocar un bloque de `block_size` bytes.

        Lanza `NoDataNodeAvailableError` si no puede satisfacer el factor pedido.
        """
        ...


class SingleNodePlacement:
    """Un DataNode, una replica.

    Si hubiera varios nodos registrados elige uno al azar entre los que tengan sitio, en
    vez de coger siempre el primero: asi, si alguien levanta un segundo DataNode antes de
    la Etapa 2, el reparto no se concentra en uno solo.
    """

    def __init__(self, data_nodes: DataNodeRepository, rng: random.Random | None = None):
        self._data_nodes = data_nodes
        self._rng = rng or random.Random()

    def select(self, block_size: int, replication_factor: int = 1) -> list[DataNode]:
        if replication_factor != 1:
            # No se simula replicacion: devolver la misma replica dos veces daria una
            # durabilidad falsa en el metadato. R>1 llega en la Etapa 3.
            raise NoDataNodeAvailableError(
                "la Etapa 1 solo soporta factor de replicacion 1",
                requested=replication_factor,
            )

        vivos = self._data_nodes.list_alive()
        if not vivos:
            raise NoDataNodeAvailableError(
                "no hay ningun DataNode registrado; arranca el DataNode y espera a que "
                "se registre"
            )

        con_sitio = [nodo for nodo in vivos if nodo.has_room_for(block_size)]
        if not con_sitio:
            raise NoDataNodeAvailableError(
                "ningun DataNode tiene espacio para el bloque",
                block_size=block_size,
                candidates=len(vivos),
            )

        return [self._rng.choice(con_sitio)]
