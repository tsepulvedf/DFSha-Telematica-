"""Politica de colocacion de bloques.

El ControlNode ELIGE el DataNode de cada bloque y registra la eleccion en
`block_replicas`. No se calcula por hash del `block_id`: con hash, anadir un nodo
reubicaria bloques existentes y no habria forma de colocar dos replicas en dominios de
falla distintos.

La fuente de verdad de carga y espacio libre es el **ultimo heartbeat**, no el contador
`used_bytes` del ControlNode, que es solo una cache y se desvia en cuanto se pierde una
notificacion.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from dfsha.common.errors import NoDataNodeAvailableError
from dfsha.common.logging import get_logger
from dfsha.control_node.domain.entities import DataNode, DataNodeState, utcnow
from dfsha.control_node.domain.membership import MembershipThresholds, state_for
from dfsha.control_node.repositories.base import DataNodeRepository

__all__ = ["BlockPlacementPolicy", "LeastLoadedPlacement", "SingleNodePlacement", "Candidate"]


@dataclass(frozen=True, slots=True)
class Candidate:
    """Un nodo evaluado, con el motivo si quedo descartado."""

    node: DataNode
    state: DataNodeState
    free_bytes: int
    reason: str = ""

    @property
    def eligible(self) -> bool:
        return not self.reason

    def describe(self) -> str:
        etiqueta = self.node.advertise_url or self.node.id[:8]
        return f"{etiqueta} ({self.node.fault_domain or 'sin-dominio'}): {self.reason}"


class BlockPlacementPolicy(Protocol):
    """Un unico metodo, a proposito: es la costura por la que entro la Etapa 2 y por la
    que entrara la Etapa 3 al subir el factor de replicacion."""

    def select(self, block_size: int, replication_factor: int) -> list[DataNode]:
        """Devuelve los nodos donde colocar un bloque de `block_size` bytes.

        Lanza `NoDataNodeAvailableError` si no puede satisfacer el factor pedido.
        """
        ...


class LeastLoadedPlacement:
    """*Power of d choices* con restriccion de dominios de falla.

    El algoritmo, en orden:

    1. **Filtrar**: solo nodos ALIVE con `disk_free_bytes >= block_size + min_free_bytes`.
       SUSPECT y DEAD quedan fuera: un nodo que lleva 10 s sin latir puede estar vivo,
       pero mandarle bloques nuevos es apostar a que vuelve.
    2. **Ordenar** por ocupacion (`used/capacity`) ascendente, desempatando por
       escrituras en vuelo.
    3. **Elegir al azar entre los `d` primeros**, no el primero. Coger siempre el mas
       vacio provoca efecto manada: todas las escrituras concurrentes van al mismo nodo
       hasta el siguiente heartbeat, que es cuando el ControlNode se entera de que ya no
       esta vacio. Con d=3 el reparto se suaviza sin dejar de favorecer a los menos
       cargados.
    4. **Dominios de falla**: cada replica en un dominio distinto. Si no quedan dominios
       libres, se relaja y se avisa, pero nunca se repite `data_node_id`: dos replicas en
       el mismo nodo no son dos replicas.
    """

    def __init__(
        self,
        data_nodes: DataNodeRepository,
        thresholds: MembershipThresholds,
        d: int = 3,
        min_free_bytes: int = 128 * 1024 * 1024,
        rng: random.Random | None = None,
        clock=utcnow,
    ) -> None:
        self._data_nodes = data_nodes
        self._thresholds = thresholds
        self._d = max(1, d)
        self._min_free_bytes = max(0, min_free_bytes)
        self._rng = rng or random.Random()
        self._clock = clock
        self._log = get_logger("control_node")
        #: Bytes que esta politica ya ha colocado en cada nodo y que el heartbeat
        #: todavia no refleja. Ver `_carga`.
        self._asignado: dict[str, int] = {}

    # --- Evaluacion --------------------------------------------------------

    def evaluate(self, block_size: int, now: datetime | None = None) -> list[Candidate]:
        """Todos los nodos con su veredicto. Publico porque es lo que hace diagnosticable
        un fallo de colocacion: el error dice por que se descarto cada uno."""
        ahora = now or self._clock()
        necesario = block_size + self._min_free_bytes
        evaluados: list[Candidate] = []

        for nodo in self._data_nodes.list_all():
            estado = state_for(
                nodo.last_heartbeat_at, ahora, self._thresholds, nodo.registered_at
            )
            libre = nodo.stats.disk_free_bytes

            if estado is not DataNodeState.ALIVE:
                motivo = f"estado {estado.value}"
            elif nodo.last_heartbeat_at is None:
                # Registrado pero sin latir: no hay dato de espacio en el que confiar.
                motivo = "todavia no ha mandado ningun heartbeat"
            elif libre < necesario:
                motivo = (
                    f"espacio insuficiente: libre {libre} B, necesarios {necesario} B "
                    f"(bloque {block_size} B + margen {self._min_free_bytes} B)"
                )
            else:
                motivo = ""

            evaluados.append(
                Candidate(node=nodo, state=estado, free_bytes=libre, reason=motivo)
            )

        return evaluados

    # --- Seleccion ---------------------------------------------------------

    def select(self, block_size: int, replication_factor: int = 1) -> list[DataNode]:
        if replication_factor < 1:
            raise NoDataNodeAvailableError(
                "el factor de replicacion debe ser al menos 1",
                requested=replication_factor,
            )

        evaluados = self.evaluate(block_size)
        elegibles = [c for c in evaluados if c.eligible]

        if len(elegibles) < replication_factor:
            self._fallar(block_size, replication_factor, evaluados)

        ordenados = sorted(elegibles, key=self._carga)
        elegidos: list[Candidate] = []
        dominios_usados: set[str] = set()
        relajado = False

        for indice in range(replication_factor):
            restantes = [c for c in ordenados if c.node.id not in {e.node.id for e in elegidos}]

            # Primero, solo dominios sin usar.
            preferentes = [c for c in restantes if c.node.fault_domain not in dominios_usados]
            if preferentes:
                candidatos = preferentes
            else:
                # Sin dominios libres se relaja, pero nunca se repite nodo: dos replicas
                # en el mismo disco se pierden juntas, que es lo contrario de replicar.
                candidatos = restantes
                relajado = True
                self._log.warning(
                    "placement.domain_relaxed",
                    block_size=block_size,
                    replication_factor=replication_factor,
                    replica_index=indice,
                    fault_domains_used=sorted(dominios_usados),
                    detail=(
                        "no quedan dominios de falla libres; esta replica comparte "
                        "dominio con otra"
                    ),
                )

            elegido = self._power_of_d(candidatos)
            elegidos.append(elegido)
            dominios_usados.add(elegido.node.fault_domain)
            # Se contabiliza al momento: el proximo bloque de este mismo archivo ya ve
            # a este nodo un poco mas cargado.
            self._asignado[elegido.node.id] = (
                self._asignado.get(elegido.node.id, 0) + block_size
            )

        self._log.info(
            "placement.selected",
            block_size=block_size,
            replication_factor=replication_factor,
            candidates=len(elegibles),
            evaluated=len(evaluados),
            chosen=[c.node.id for c in elegidos],
            chosen_urls=[c.node.advertise_url for c in elegidos],
            fault_domains=[c.node.fault_domain for c in elegidos],
            domain_relaxed=relajado,
        )
        return [c.node for c in elegidos]

    def _power_of_d(self, candidatos: list[Candidate]) -> Candidate:
        """Uno al azar entre los `d` menos cargados de la lista, que ya viene ordenada."""
        ventana = candidatos[: self._d]
        return self._rng.choice(ventana)

    def _carga(self, candidato: Candidate) -> tuple[float, int, str]:
        """Ocupacion del nodo contando lo que esta politica ya le ha asignado.

        Sin esto hay un sesgo importante y facil de pasar por alto: los bloques de un
        `put` se colocan TODOS en la misma llamada a `/files/create`, con una unica foto
        de carga, la del ultimo heartbeat. Cuatro nodos igual de vacios y d=3 significan
        que el cuarto no entra en la ventana ni una sola vez en todo el archivo, por muy
        aleatoria que sea la eleccion dentro de la ventana. Es el mismo efecto manada que
        el power of d evita entre peticiones, reaparecido dentro de una.

        Contar lo ya asignado hace que el orden se reordene bloque a bloque, y entonces
        el reparto alcanza a todos los nodos validos.
        """
        stats = candidato.node.stats
        pendiente = self._asignado.get(candidato.node.id, 0)
        capacidad = stats.capacity_bytes or candidato.node.capacity_bytes
        ocupacion = (
            min(1.0, (stats.used_bytes + pendiente) / capacidad) if capacidad > 0 else 1.0
        )
        # El id entra en la clave solo para que el orden sea estable entre ejecuciones
        # cuando dos nodos empatan en todo: sin el, `sorted` es estable respecto a la
        # consulta, que no tiene por que serlo.
        return (ocupacion, stats.writes_in_flight, candidato.node.id)

    def _fallar(
        self, block_size: int, replication_factor: int, evaluados: list[Candidate]
    ) -> None:
        """Nunca colocar en silencio menos replicas de las pedidas."""
        descartes = [c.describe() for c in evaluados if not c.eligible]
        elegibles = sum(1 for c in evaluados if c.eligible)

        self._log.error(
            "placement.insufficient_candidates",
            block_size=block_size,
            replication_factor=replication_factor,
            eligible=elegibles,
            evaluated=len(evaluados),
            rejected=descartes,
        )
        detalle = "; ".join(descartes) if descartes else "no hay ningun DataNode registrado"
        raise NoDataNodeAvailableError(
            f"se pidieron {replication_factor} replicas y solo hay {elegibles} nodos "
            f"validos de {len(evaluados)} registrados. Descartados: {detalle}",
            requested=replication_factor,
            eligible=elegibles,
            evaluated=len(evaluados),
            rejected=descartes,
        )


class SingleNodePlacement(LeastLoadedPlacement):
    """Alias de la Etapa 1, conservado para no romper llamadas existentes.

    `LeastLoadedPlacement` con un solo nodo registrado se comporta igual que aquella,
    asi que no hay dos implementaciones que mantener.
    """
