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

__all__ = ["NodeStatus", "ReplicationHealth", "cluster_status", "replication_health"]


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


@dataclass(frozen=True, slots=True)
class ReplicationHealth:
    """Cuanta replicacion le falta al cluster entero.

    Dos numeros y no uno, porque no todos los huecos cuestan lo mismo: un bloque con dos
    copias de tres sigue tolerando una caida, y uno con una sola esta a un fallo de
    desaparecer. Son los dos numeros que ordenan la cola de re-replicacion.
    """

    under_replicated: int
    critical: int
    total_blocks: int


@query("cluster.replication_health")
def replication_health(uow: SqlUnitOfWork, replication_factor: int) -> ReplicationHealth:
    with uow:
        cuentas = uow.blocks.live_replica_counts()

    return ReplicationHealth(
        under_replicated=sum(1 for c in cuentas if c < replication_factor),
        critical=sum(1 for c in cuentas if c == 1),
        total_blocks=len(cuentas),
    )
