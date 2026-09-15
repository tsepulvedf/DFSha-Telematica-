"""Lado lectura del plano de control: estado del cluster.

Es la ventana que hace depurable el resto de la etapa. Sin esto, saber por que un bloque
fue a parar a un nodo y no a otro obliga a leer logs; con esto se ve de un vistazo quien
esta vivo, cuanto le queda y cuando hablo por ultima vez.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from dfsha.control_node.domain.entities import utcnow
from dfsha.control_node.domain.membership import MembershipThresholds, state_for
from dfsha.control_node.repositories.sql import SqlUnitOfWork
from dfsha.control_node.tracing import query

__all__ = ["NodeStatus", "cluster_status"]


@dataclass(frozen=True, slots=True)
class NodeStatus:
    data_node_id: str
    advertise_url: str
    fault_domain: str
    state: str
    used_bytes: int
    capacity_bytes: int
    disk_free_bytes: int
    block_count: int
    replica_count: int
    seconds_since_heartbeat: float | None
    writes_in_flight: int
    reads_in_flight: int


@query("cluster.status")
def cluster_status(
    uow: SqlUnitOfWork,
    thresholds: MembershipThresholds,
    now: datetime | None = None,
) -> list[NodeStatus]:
    """Estado de todos los DataNodes.

    El estado se **deriva** aqui del ultimo heartbeat, no se lee de la columna: asi lo
    que se ve es la verdad en el instante de la consulta, sin depender de que el
    evaluador periodico haya pasado ya. Es lo que hace que la transicion a SUSPECT se vea
    en cuanto ocurre.
    """
    ahora = now or utcnow()

    with uow:
        nodos = uow.data_nodes.list_all()
        replicas = uow.blocks.count_replicas_by_node()

        return [
            NodeStatus(
                data_node_id=n.id,
                advertise_url=n.advertise_url,
                fault_domain=n.fault_domain,
                state=state_for(
                    n.last_heartbeat_at, ahora, thresholds, n.registered_at
                ).value,
                used_bytes=n.stats.used_bytes,
                capacity_bytes=n.stats.capacity_bytes or n.capacity_bytes,
                disk_free_bytes=n.stats.disk_free_bytes,
                block_count=n.stats.block_count,
                replica_count=replicas.get(n.id, 0),
                seconds_since_heartbeat=(
                    round((ahora - n.last_heartbeat_at).total_seconds(), 1)
                    if n.last_heartbeat_at
                    else None
                ),
                writes_in_flight=n.stats.writes_in_flight,
                reads_in_flight=n.stats.reads_in_flight,
            )
            for n in nodos
        ]
