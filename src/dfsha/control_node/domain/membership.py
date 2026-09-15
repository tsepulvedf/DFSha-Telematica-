"""Pertenencia al cluster: estado de cada DataNode y reincorporacion tras un reinicio.

Funciones puras con el reloj y los umbrales inyectados. Nada de `utcnow()` escondido
dentro: probar que un nodo pasa a DEAD a los 30 s no puede requerir esperar 30 s.

El estado es **derivado** del ultimo heartbeat, no un valor que alguien recuerde
actualizar. Asi no puede quedar desincronizado: si el ultimo heartbeat tiene 12 s, el
nodo esta SUSPECT, lo haya escrito alguien en la base de datos o no. La columna `state`
se persiste solo para poder detectar la transicion y emitir el evento.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .entities import DataNode, DataNodeState

__all__ = [
    "MembershipThresholds",
    "state_for",
    "evaluate",
    "StateChange",
    "RejoinKind",
    "classify_rejoin",
]


@dataclass(frozen=True, slots=True)
class MembershipThresholds:
    """Umbrales de deteccion, configurables.

    Los valores por defecto son agresivos a proposito: con un heartbeat cada 3 s, 10 s
    son tres latidos perdidos y 30 s son diez. En un sistema de produccion serian mucho
    mas largos, porque el coste de declarar muerto a un nodo vivo es alto. Aqui se
    eligieron para que la transicion quepa en la demostracion del hito.
    """

    suspect_after: timedelta = timedelta(seconds=10)
    dead_after: timedelta = timedelta(seconds=30)

    @classmethod
    def from_millis(cls, suspect_after_ms: int, dead_after_ms: int) -> "MembershipThresholds":
        if suspect_after_ms >= dead_after_ms:
            raise ValueError(
                "el umbral de SUSPECT debe ser menor que el de DEAD; si no, el estado "
                "SUSPECT no existiria y un hipo de red costaria una re-replicacion"
            )
        return cls(
            suspect_after=timedelta(milliseconds=suspect_after_ms),
            dead_after=timedelta(milliseconds=dead_after_ms),
        )


def state_for(
    last_heartbeat_at: datetime | None,
    now: datetime,
    thresholds: MembershipThresholds,
    registered_at: datetime | None = None,
) -> DataNodeState:
    """Estado que le corresponde a un nodo segun cuanto hace que no se le oye.

    Un nodo recien registrado que aun no ha mandado su primer heartbeat cuenta desde su
    alta: se le da el mismo margen que a cualquiera, en vez de nacer SUSPECT.
    """
    referencia = last_heartbeat_at or registered_at
    if referencia is None:
        return DataNodeState.DEAD

    silencio = now - referencia
    if silencio >= thresholds.dead_after:
        return DataNodeState.DEAD
    if silencio >= thresholds.suspect_after:
        return DataNodeState.SUSPECT
    return DataNodeState.ALIVE


@dataclass(frozen=True, slots=True)
class StateChange:
    data_node_id: str
    fault_domain: str
    previous: DataNodeState
    current: DataNodeState
    silence_seconds: float

    @property
    def lost_replicas(self) -> bool:
        """True cuando la transicion hace que sus replicas dejen de ser legibles.

        SUSPECT saca al nodo de la colocacion pero sus replicas se siguen sirviendo: el
        nodo probablemente esta vivo y solo se le perdieron unos latidos. Solo DEAD las
        da por no disponibles.
        """
        return self.current is DataNodeState.DEAD and self.previous is not DataNodeState.DEAD

    @property
    def recovered(self) -> bool:
        return self.current is DataNodeState.ALIVE and self.previous is not DataNodeState.ALIVE


def evaluate(
    node: DataNode, now: datetime, thresholds: MembershipThresholds
) -> StateChange | None:
    """Devuelve la transicion si el estado derivado difiere del persistido, si no `None`."""
    actual = state_for(node.last_heartbeat_at, now, thresholds, node.registered_at)
    if actual is node.state:
        return None

    referencia = node.last_heartbeat_at or node.registered_at
    silencio = (now - referencia).total_seconds() if referencia else 0.0

    return StateChange(
        data_node_id=node.id,
        fault_domain=node.fault_domain,
        previous=node.state,
        current=actual,
        silence_seconds=round(silencio, 3),
    )


class RejoinKind(str):
    """Marcador de tipo; los valores son las constantes de abajo."""


NEW_NODE = "NEW_NODE"
SAME_BOOT = "SAME_BOOT"
WIPED_DISK = "WIPED_DISK"


def classify_rejoin(existing: DataNode | None, boot_id: str) -> str:
    """Decide que significa este registro.

    - `NEW_NODE`: no habia nada con ese id. Alta normal.
    - `SAME_BOOT`: vuelve con el mismo `boot_id`, asi que conserva su disco. Sus replicas
      siguen siendo validas y vuelven a estar disponibles tras el primer report completo.
    - `WIPED_DISK`: vuelve con otro `boot_id`. Perdio el disco: conserva su
      `data_node_id`, pero sus replicas antiguas pasan a MISSING de forma definitiva,
      porque los bytes ya no estan. Reincorporarlo sin mas seria anunciar bloques que no
      existen, y el cliente se los encontraria al descargarlos.
    """
    if existing is None:
        return NEW_NODE
    if existing.boot_id and existing.boot_id == boot_id:
        return SAME_BOOT
    return WIPED_DISK
