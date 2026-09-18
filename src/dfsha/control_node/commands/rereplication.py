"""Deteccion, programacion y despacho de copias de recuperacion.

Tres pasos separados a proposito, porque fallan de formas distintas y conviene poder
mirarlos por separado en los logs:

1. `scan` — que bloques van cortos de copias, y desde hace cuanto.
2. `dispatch` — a quien se le encarga cada copia, respetando los topes.
3. `complete` / `expire` — cerrar lo que salio bien y devolver a la cola lo que no.

**Los tres exigen liderazgo, con epoca verificada dentro de su transaccion.** Es la misma
regla del Bloque A y por el mismo motivo, agravado aqui: dos lideres programando a la vez
no duplicarian un evento de log, duplicarian el trafico de copia de un cluster que ya
esta recuperandose de una caida.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from dfsha.common.logging import get_logger
from dfsha.control_node.commands.leadership import require_leadership
from dfsha.control_node.domain.entities import DataNodeState, ReplicaState, utcnow
from dfsha.control_node.domain.leadership import Fencing
from dfsha.control_node.domain.membership import MembershipThresholds, state_for
from dfsha.control_node.domain.rereplication import (
    Assignment,
    ReplicationGap,
    assign_targets,
)
from dfsha.control_node.repositories.sql import SqlUnitOfWork

__all__ = [
    "RereplicationPolicy",
    "ReplicationOrder",
    "DeleteOrder",
    "PendingOrders",
    "scan_and_enqueue",
    "dispatch",
    "pending_orders",
    "complete_for_block",
]


@dataclass(frozen=True, slots=True)
class RereplicationPolicy:
    replication_factor: int
    grace: timedelta
    max_per_node: int
    #: Tope por pasada. El planificador no vacia la cola de golpe: reparte por tandas
    #: para que el cluster respire entre una y otra.
    max_per_pass: int = 8
    #: Cuanto se le da al destino para confirmar antes de devolver la tarea a la cola.
    task_ttl: timedelta = timedelta(minutes=10)
    max_attempts: int = 3


@dataclass(frozen=True, slots=True)
class ReplicationOrder:
    """Copia que hay que empujarle a un DataNode por su stream."""

    task_id: str
    block_id: str
    source_base_url: str
    source_node_id: str
    size: int
    checksum_sha256: str
    #: Token de LECTURA sobre el bloque. El destino descarga del origen por REST, que es
    #: un camino sin autenticar: sin esto, activar el token de bloque romperia la
    #: re-replicacion con 403 y el cluster dejaria de repararse solo.
    block_token: str = ""


@dataclass(frozen=True, slots=True)
class DeleteOrder:
    """Borrado de un huerfano por el mismo canal."""

    task_id: str
    block_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class PendingOrders:
    replicate: list[ReplicationOrder]
    delete: list[DeleteOrder]

    def __bool__(self) -> bool:
        return bool(self.replicate or self.delete)


# --- 1. Deteccion -----------------------------------------------------------


def _replica_utilizable(replica: dict, ahora: datetime, umbrales: MembershipThresholds) -> bool:
    """Una copia con la que de verdad se puede contar.

    STORED en un nodo que no esta DEAD. SUSPECT si cuenta, que es la decision 2 de la
    Etapa 2: un hipo de red saca al nodo de la colocacion pero sus bloques se siguen
    sirviendo, y darlos por perdidos costaria una re-replicacion entera por nada.
    """
    if replica["state"] != ReplicaState.STORED.value:
        return False
    estado = state_for(replica["last_heartbeat_at"], ahora, umbrales)
    return estado is not DataNodeState.DEAD


def _hueco_asentado(
    bloque: dict, ahora: datetime, umbrales: MembershipThresholds, gracia: timedelta
) -> bool:
    """Si el hueco lleva ahi lo bastante como para actuar.

    **La espera de gracia, que es el freno mas importante de los tres.** Reiniciar un
    contenedor tarda segundos; copiar su disco entero por un reinicio es el error clasico
    y ademas se encadena: la copia satura la red, otro nodo deja de latir a tiempo, y se
    dispara otra copia.

    Un hueco cuenta como asentado si TODAS sus copias ausentes lo llevan siendo desde
    hace mas de la gracia. Dos formas de estar ausente, y las dos se miden distinto:

    - El nodo murio: se cuenta desde su ultimo latido. Un nodo pasa a DEAD en
      `dead_after`, asi que exigir `dead_after + gracia` de silencio es exactamente
      "gracia desde que entro en DEAD".
    - La copia nunca llego a escribirse (PENDING que se quedo ahi porque fallo un salto
      del pipeline): se cuenta desde que se planifico.
    """
    for replica in bloque["replicas"]:
        if _replica_utilizable(replica, ahora, umbrales):
            continue

        if replica["state"] == ReplicaState.PENDING.value:
            desde = replica["created_at"]
            if desde is None or ahora - desde < gracia:
                return False
            continue

        ultimo = replica["last_heartbeat_at"]
        if ultimo is None:
            # Nodo registrado que nunca latio. No hay reloj desde el que medir: se deja
            # correr y ya se vera en la proxima pasada.
            return False
        if ahora - ultimo < umbrales.dead_after + gracia:
            return False

    return True


def _a_hueco(
    bloque: dict, ahora: datetime, umbrales: MembershipThresholds, objetivo: int
) -> ReplicationGap:
    vivas = [r for r in bloque["replicas"] if _replica_utilizable(r, ahora, umbrales)]
    return ReplicationGap(
        block_id=bloque["block_id"],
        size=bloque["size"],
        checksum_sha256=bloque["checksum_sha256"],
        current_replicas=len(vivas),
        target_replicas=objetivo,
        holders=tuple(r["data_node_id"] for r in vivas),
        holder_domains=tuple(r["fault_domain"] for r in vivas),
    )


def scan_and_enqueue(
    uow: SqlUnitOfWork,
    fencing: Fencing,
    policy: RereplicationPolicy,
    thresholds: MembershipThresholds,
    now: datetime | None = None,
) -> int:
    """Busca huecos asentados y los mete en la cola. Devuelve cuantos encolo."""
    ahora = now or utcnow()
    log = get_logger("control_node")
    inicio = time.perf_counter()
    encolados = 0

    with uow:
        require_leadership(uow, fencing, ahora)

        for bloque in uow.rereplication.blocks_missing_replicas(policy.replication_factor):
            if not _hueco_asentado(bloque, ahora, thresholds, policy.grace):
                continue

            hueco = _a_hueco(bloque, ahora, thresholds, policy.replication_factor)
            if hueco.missing <= 0:
                continue

            task_id = uow.rereplication.enqueue(
                hueco.block_id, hueco.current_replicas, ahora
            )
            if task_id is None:
                continue  # ya habia una tarea viva

            encolados += 1
            log.info(
                "rereplication.scheduled",
                task_id=task_id,
                block_id=hueco.block_id,
                current_replicas=hueco.current_replicas,
                target_replicas=hueco.target_replicas,
                critical=hueco.critical,
                epoch=fencing.epoch,
                duration_ms=round((time.perf_counter() - inicio) * 1000, 3),
            )

        if encolados:
            uow.commit()

    return encolados


# --- 2. Despacho ------------------------------------------------------------


def dispatch(
    uow: SqlUnitOfWork,
    fencing: Fencing,
    policy: RereplicationPolicy,
    thresholds: MembershipThresholds,
    now: datetime | None = None,
) -> list[Assignment]:
    """Asigna origen y destino a las tareas pendientes, respetando los topes.

    El destino se elige con los mismos criterios que la colocacion de un bloque nuevo:
    nodos vivos, dominios de falla distintos, y nunca un nodo que ya tenga el bloque.
    """
    ahora = now or utcnow()
    log = get_logger("control_node")

    with uow:
        require_leadership(uow, fencing, ahora)

        caducadas = uow.rereplication.expire_stale(ahora, policy.max_attempts)
        for block_id, target_node_id in caducadas.abandoned_targets:
            # La copia no llego a escribirse en ese destino. Se quita la fila PENDING
            # para que el proximo despacho pueda elegir otro nodo sin que este siga
            # contando como "ya lo tiene".
            uow.blocks.drop_replica(block_id, target_node_id)

        devueltas, rendidas = caducadas.requeued, caducadas.gave_up
        if devueltas or rendidas:
            log.warning(
                "rereplication.expired",
                requeued=devueltas,
                gave_up=rendidas,
                detail="el destino no confirmo la copia a tiempo",
            )

        pendientes = uow.rereplication.list_pending()
        if not pendientes:
            if devueltas or rendidas:
                uow.commit()
            return []

        # Estado del bloque de cada tarea, ahora mismo: entre que se encolo y ahora pudo
        # volver el nodo que faltaba, y entonces no hay nada que copiar.
        por_bloque = {
            b["block_id"]: b
            for b in uow.rereplication.blocks_missing_replicas(policy.replication_factor)
        }

        huecos: list[ReplicationGap] = []
        tarea_de: dict[str, str] = {}
        for tarea in pendientes:
            bloque = por_bloque.get(tarea.block_id)
            if bloque is None:
                # Ya no le falta nada: se cierra la tarea en vez de copiar de mas.
                tarea.state = "DONE"
                tarea.last_error = "el hueco se cerro solo antes de despachar"
                continue
            hueco = _a_hueco(bloque, ahora, thresholds, policy.replication_factor)
            if hueco.missing <= 0:
                tarea.state = "DONE"
                continue
            huecos.append(hueco)
            tarea_de[hueco.block_id] = tarea.id

        candidatos = {
            n.id: n.fault_domain
            for n in uow.data_nodes.list_all()
            if state_for(n.last_heartbeat_at, ahora, thresholds, n.registered_at)
            is DataNodeState.ALIVE
        }

        asignaciones = assign_targets(
            huecos,
            candidatos,
            uow.rereplication.in_flight_by_target(),
            max_per_target=policy.max_per_node,
            max_total=policy.max_per_pass,
        )

        for asignacion in asignaciones:
            task_id = tarea_de[asignacion.block_id]
            # Se REGISTRA la eleccion en block_replicas, igual que en una colocacion
            # normal. Es la decision 3 de CLAUDE.md, y aqui no es una formalidad: sin
            # esta fila, el nodo destino copia el bloque y al avisar recibe un 404, la
            # copia queda huerfana en su disco y la tarea no se cierra nunca.
            uow.blocks.add_replica(
                asignacion.block_id,
                asignacion.target_node_id,
                ReplicaState.PENDING,
                ahora,
            )
            uow.rereplication.dispatch(
                task_id,
                asignacion.source_node_id,
                asignacion.target_node_id,
                ahora,
                ahora + policy.task_ttl,
            )
            log.info(
                "rereplication.dispatched",
                task_id=task_id,
                block_id=asignacion.block_id,
                source=asignacion.source_node_id,
                target=asignacion.target_node_id,
                remaining_replicas=asignacion.gap.current_replicas,
                critical=asignacion.gap.critical,
                epoch=fencing.epoch,
            )

        uow.commit()
        return asignaciones


# --- 3. Cierre --------------------------------------------------------------


def complete_for_block(uow: SqlUnitOfWork, block_id: str, data_node_id: str) -> bool:
    """Cierra la tarea de re-replicacion de este bloque en este nodo, si la habia.

    **No exige liderazgo**, y es deliberado: lo llama el camino por el que un DataNode
    avisa de que almaceno un bloque, que atiende cualquier instancia. Exigir liderazgo
    aqui haria que una copia terminada por un nodo cuyo heartbeat atiende una instancia
    que no es la lider se quedara sin cerrar, y la tarea volveria a la cola al vencer
    para repetir un trabajo que ya estaba hecho.

    Cerrar una tarea no es una decision: es registrar un hecho que ya ocurrio.
    """
    cerradas = uow.rereplication.complete(block_id, data_node_id)
    if cerradas:
        get_logger("control_node").info(
            "rereplication.completed",
            block_id=block_id,
            data_node_id=data_node_id,
            duration_ms=0.0,
        )
    return cerradas > 0


def pending_orders(
    uow: SqlUnitOfWork,
    data_node_id: str,
    resend_after: timedelta,
    now: datetime | None = None,
    signer=None,
) -> PendingOrders:
    """Ordenes que hay que empujarle a este nodo, y las marca como enviadas.

    **No exige liderazgo, a proposito.** El stream de heartbeat de un nodo lo atiende
    cualquiera de los tres ControlNodes, no necesariamente el lider: por eso la cola vive
    en la base y no en la memoria de quien la programo. Quien DECIDE que copiar si exige
    liderazgo (`scan_and_enqueue` y `dispatch`); esto solo entrega una decision ya
    tomada, y negarse a entregarla porque la atiende otra instancia dejaria la copia
    esperando hasta que el lease cambiara de manos.

    **El token se emite AQUI y no al programar la copia**, aunque programar sea el momento
    en que se toma la decision. El motivo es el reloj: una tarea puede quedarse en la cola
    minutos —la gracia son cinco— y reintentarse despues, asi que un token firmado al
    programarla llegaria caducado justo en el caso que importa, el de un cluster que va
    lento porque se esta recuperando. Emitirlo al entregar la orden le da su vida entera
    para hacer el trabajo. La decision sigue siendo la de antes; esto solo es cuando se
    escribe el permiso.
    """
    ahora = now or utcnow()
    tareas = uow.rereplication.orders_for(data_node_id, ahora, resend_after)
    if not tareas:
        return PendingOrders(replicate=[], delete=[])

    copias: list[ReplicationOrder] = []
    borrados: list[DeleteOrder] = []

    origenes = {n.id: n for n in uow.data_nodes.list_all()}
    bloques = {
        b.block_id: b
        for b in uow.blocks.get_many([t.block_id for t in tareas])
    }

    for tarea in tareas:
        if tarea.kind == "DELETE":
            borrados.append(
                DeleteOrder(
                    task_id=tarea.id,
                    block_id=tarea.block_id,
                    reason="huerfano confirmado por el recolector",
                )
            )
            continue

        origen = origenes.get(tarea.source_node_id or "")
        bloque = bloques.get(tarea.block_id)
        if origen is None or bloque is None:
            # El origen desaparecio entre el despacho y ahora. No se manda una orden
            # rota: la tarea vence sola y se vuelve a despachar con otro origen.
            continue

        copias.append(
            ReplicationOrder(
                task_id=tarea.id,
                block_id=tarea.block_id,
                # peer_base_url y NO advertise_url: esta URL se la manda el ControlNode
                # al nodo DESTINO para que descargue el bloque del ORIGEN, o sea que es
                # trafico entre DataNodes. Con la direccion de cliente, en contenedores
                # el destino se descargaria de si mismo.
                source_base_url=origen.peer_base_url,
                source_node_id=origen.id,
                size=bloque.size,
                checksum_sha256=bloque.checksum_sha256 or "",
                block_token=(
                    signer.issue(
                        tarea.block_id, "read", now=ahora, subject="rereplication"
                    )
                    if signer is not None
                    else ""
                ),
            )
        )

    uow.rereplication.mark_sent([t.id for t in tareas], ahora)
    return PendingOrders(replicate=copias, delete=borrados)
