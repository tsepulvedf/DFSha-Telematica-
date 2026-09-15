"""Casos de uso del plano de control: registro, heartbeat, block report y pertenencia.

Lado escritura, como el resto de `commands/`. Estas funciones no saben que existe gRPC:
reciben datos planos y devuelven resultados planos. La traduccion desde Protobuf vive en
`api/grpc.py`, igual que la traduccion desde HTTP vive en `api/routers.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from dfsha.common.errors import NotFoundError
from dfsha.common.logging import get_logger
from dfsha.control_node.domain.divergence import DivergenceReport, compare
from dfsha.control_node.domain.entities import (
    DataNode,
    DataNodeState,
    NodeStats,
    ReplicaState,
    utcnow,
)
from dfsha.control_node.domain.membership import (
    SAME_BOOT,
    WIPED_DISK,
    MembershipThresholds,
    StateChange,
    classify_rejoin,
    evaluate,
)
from dfsha.control_node.repositories.sql import SqlUnitOfWork
from dfsha.control_node.tracing import command

__all__ = [
    "RegistrationResult",
    "HeartbeatResult",
    "register_node",
    "record_heartbeat",
    "apply_block_report",
    "evaluate_membership",
]

#: Estados de replica que el metadato espera encontrar en un nodo. MISSING entra a
#: proposito: si el nodo vuelve y la reporta, tiene que poder volver a STORED.
_ESPERADAS = (ReplicaState.STORED, ReplicaState.MISSING)


@dataclass(frozen=True, slots=True)
class RegistrationResult:
    node: DataNode
    rejoin_kind: str
    replicas_lost: int = 0


@dataclass(frozen=True, slots=True)
class HeartbeatResult:
    #: El ControlNode pide un report completo cuando sospecha que su vision no cuadra.
    request_full_report: bool = False
    reason: str = ""
    state_change: StateChange | None = None
    unknown_blocks: tuple[str, ...] = field(default_factory=tuple)


@command("control_plane.register")
def register_node(
    uow: SqlUnitOfWork,
    advertise_url: str,
    fault_domain: str,
    boot_id: str,
    capacity_bytes: int,
    data_node_id: str = "",
) -> RegistrationResult:
    """Da de alta un nodo, o lo readmite.

    El caso interesante es el tercero: un nodo que vuelve con otro `boot_id` perdio su
    disco. Conserva su identidad, pero sus replicas pasan a MISSING de forma definitiva,
    porque los bytes ya no estan. Readmitirlo sin mas seria seguir anunciando bloques
    inexistentes, y el cliente se los encontraria al descargarlos.
    """
    log = get_logger("control_node")

    with uow:
        existente = None
        if data_node_id:
            existente = uow.data_nodes.get(data_node_id)
        if existente is None:
            existente = uow.data_nodes.get_by_advertise_url(advertise_url)

        clase = classify_rejoin(existente, boot_id)
        ahora = utcnow()

        nodo = uow.data_nodes.register(
            advertise_url=advertise_url,
            capacity_bytes=capacity_bytes,
            now=ahora,
            fault_domain=fault_domain,
            boot_id=boot_id,
            data_node_id=(existente.id if existente else data_node_id) or None,
        )

        perdidas = 0
        if clase == WIPED_DISK and existente is not None:
            perdidas = uow.blocks.mark_node_replicas(
                nodo.id, ReplicaState.MISSING, only_from=(ReplicaState.STORED,)
            )

        uow.commit()

    log.info(
        "node.registered",
        data_node_id=nodo.id,
        advertise_url=advertise_url,
        fault_domain=fault_domain,
        boot_id=boot_id,
        capacity_bytes=capacity_bytes,
        rejoin_kind=clase,
        replicas_lost=perdidas,
    )
    if perdidas:
        log.warning(
            "node.disk_wiped",
            data_node_id=nodo.id,
            replicas_lost=perdidas,
            detail="volvio con otro boot_id; sus replicas pasan a MISSING",
        )

    return RegistrationResult(node=nodo, rejoin_kind=clase, replicas_lost=perdidas)


@command("control_plane.heartbeat")
def record_heartbeat(
    uow: SqlUnitOfWork,
    data_node_id: str,
    boot_id: str,
    sequence: int,
    stats: NodeStats,
    added_block_ids: list[str],
    removed_block_ids: list[str],
    thresholds: MembershipThresholds,
    now: datetime | None = None,
) -> HeartbeatResult:
    """Anota el latido y aplica el report incremental que lo acompana."""
    log = get_logger("control_node")
    ahora = now or utcnow()

    with uow:
        nodo = uow.data_nodes.get(data_node_id)
        if nodo is None:
            raise NotFoundError("DataNode desconocido", data_node_id=data_node_id)

        if nodo.boot_id and boot_id and nodo.boot_id != boot_id:
            # El nodo rearranco sin pasar por Register. No se toca nada del metadato
            # con datos de un arranque que el ControlNode no reconoce.
            return HeartbeatResult(
                request_full_report=True,
                reason="boot_id distinto del registrado; vuelve a registrarte",
            )

        anterior_seq = nodo.last_sequence
        uow.data_nodes.record_heartbeat(data_node_id, sequence, stats, ahora)

        # Un latido recien recibido significa ALIVE por definicion, sin necesidad de
        # volver a derivarlo de los umbrales: acaba de hablar. Si la fila decia SUSPECT
        # o DEAD, esta es su reincorporacion.
        cambio: StateChange | None = None
        if nodo.state is not DataNodeState.ALIVE:
            cambio = StateChange(
                data_node_id=nodo.id,
                fault_domain=nodo.fault_domain,
                previous=nodo.state,
                current=DataNodeState.ALIVE,
                silence_seconds=_silencio(nodo.last_heartbeat_at, ahora),
            )
            uow.data_nodes.set_state(data_node_id, DataNodeState.ALIVE)

        desconocidos: tuple[str, ...] = ()
        if added_block_ids:
            informe = compare(
                expected=uow.blocks.list_block_ids_on_node(data_node_id, _ESPERADAS),
                reported=added_block_ids,
                is_full=False,
            )
            desconocidos = informe.unknown_block_ids
            if informe.confirmed_block_ids:
                uow.blocks.set_replicas_state(
                    informe.confirmed_block_ids, data_node_id, ReplicaState.STORED
                )

        if removed_block_ids:
            uow.blocks.set_replicas_state(
                removed_block_ids, data_node_id, ReplicaState.MISSING
            )

        uow.commit()

    log.info(
        "heartbeat.received",
        data_node_id=data_node_id,
        sequence=sequence,
        used_bytes=stats.used_bytes,
        disk_free_bytes=stats.disk_free_bytes,
        block_count=stats.block_count,
        lag_ms=_lag_ms(nodo.last_heartbeat_at, ahora),
    )
    if cambio is not None:
        _log_state_change(log, cambio)
    for block_id in desconocidos:
        log.warning(
            "divergence.unknown_block",
            data_node_id=data_node_id,
            block_id=block_id,
            source="heartbeat",
            detail="el nodo anuncia un bloque que el metadato no conoce; queda para el GC",
        )
    if removed_block_ids:
        for block_id in removed_block_ids:
            log.warning(
                "divergence.missing_block",
                data_node_id=data_node_id,
                block_id=block_id,
                source="heartbeat",
            )

    hueco = sequence > anterior_seq + 1 and anterior_seq > 0
    return HeartbeatResult(
        request_full_report=bool(desconocidos) or hueco,
        reason=(
            "bloques desconocidos en el report incremental"
            if desconocidos
            else (f"hueco en la secuencia: {anterior_seq} -> {sequence}" if hueco else "")
        ),
        state_change=cambio,
        unknown_blocks=desconocidos,
    )


@command("control_plane.block_report")
def apply_block_report(
    uow: SqlUnitOfWork,
    data_node_id: str,
    boot_id: str,
    is_full: bool,
    reported_block_ids: list[str],
) -> DivergenceReport:
    """Compara lo reportado con lo esperado y anota la divergencia. No borra nada."""
    log = get_logger("control_node")

    with uow:
        nodo = uow.data_nodes.get(data_node_id)
        if nodo is None:
            raise NotFoundError("DataNode desconocido", data_node_id=data_node_id)

        informe = compare(
            expected=uow.blocks.list_block_ids_on_node(data_node_id, _ESPERADAS),
            reported=reported_block_ids,
            is_full=is_full,
        )

        if informe.confirmed_block_ids:
            # Un nodo que vuelve y reporta una replica que estaba MISSING la recupera.
            uow.blocks.set_replicas_state(
                informe.confirmed_block_ids, data_node_id, ReplicaState.STORED
            )
        if informe.missing_block_ids:
            uow.blocks.set_replicas_state(
                informe.missing_block_ids, data_node_id, ReplicaState.MISSING
            )

        uow.commit()

    for block_id in informe.unknown_block_ids:
        log.warning(
            "divergence.unknown_block",
            data_node_id=data_node_id,
            block_id=block_id,
            source="block_report",
            detail="huerfano: el GC lo recogera, el ControlNode no borra nada",
        )
    for block_id in informe.missing_block_ids:
        log.warning(
            "divergence.missing_block",
            data_node_id=data_node_id,
            block_id=block_id,
            source="block_report",
            detail="la replica pasa a MISSING; con R=1 su archivo queda ilegible",
        )

    log.info(
        "block_report.applied",
        data_node_id=data_node_id,
        is_full=is_full,
        reported=len(reported_block_ids),
        confirmed=len(informe.confirmed_block_ids),
        unknown=informe.unknown_count,
        missing=informe.missing_count,
    )
    return informe


@command("control_plane.evaluate_membership")
def evaluate_membership(
    uow: SqlUnitOfWork,
    thresholds: MembershipThresholds,
    now: datetime | None = None,
) -> list[StateChange]:
    """Persiste las transiciones de estado y emite sus eventos.

    El estado real se deriva del ultimo heartbeat en cada lectura, asi que esto no es lo
    que mantiene el sistema correcto: es lo que hace que la transicion quede registrada
    y que las replicas de un nodo muerto dejen de anunciarse.
    """
    log = get_logger("control_node")
    ahora = now or utcnow()
    cambios: list[StateChange] = []

    with uow:
        for nodo in uow.data_nodes.list_all():
            cambio = evaluate(nodo, ahora, thresholds)
            if cambio is None:
                continue

            uow.data_nodes.set_state(nodo.id, cambio.current)
            if cambio.lost_replicas:
                # Sus bytes pueden seguir ahi, pero nadie puede confirmarlo. Se marcan
                # MISSING, no se borran: en la Etapa 3 esto dispara la re-replicacion.
                uow.blocks.mark_node_replicas(
                    nodo.id, ReplicaState.MISSING, only_from=(ReplicaState.STORED,)
                )
            cambios.append(cambio)

        if cambios:
            uow.commit()

    for cambio in cambios:
        _log_state_change(log, cambio)
    return cambios


def _log_state_change(log, cambio: StateChange) -> None:
    log.warning(
        "node.state_changed",
        data_node_id=cambio.data_node_id,
        fault_domain=cambio.fault_domain,
        **{"from": cambio.previous.value, "to": cambio.current.value},
        silence_seconds=cambio.silence_seconds,
        lost_replicas=cambio.lost_replicas,
    )


def _silencio(anterior: datetime | None, ahora: datetime) -> float:
    """Segundos que el nodo estuvo callado antes de este latido."""
    if anterior is None:
        return 0.0
    return round((ahora - anterior).total_seconds(), 3)


def _lag_ms(anterior: datetime | None, ahora: datetime) -> float:
    """Cuanto tardo en llegar este latido desde el anterior. Mide si el nodo va con
    retraso antes de que el retraso llegue a costarle el estado ALIVE."""
    if anterior is None:
        return 0.0
    return round((ahora - anterior).total_seconds() * 1000, 1)
