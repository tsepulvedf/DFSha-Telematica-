"""Cola de re-replicacion: a quien se copia, en que orden y cuanto a la vez.

Funciones puras. Reciben el estado ya leido y devuelven decisiones; no tocan la base ni
la red. Eso es lo que permite probar la prioridad y los topes sin levantar un cluster.

## Tres frenos, y ninguno es opcional

La re-replicacion es el mecanismo del sistema con mas capacidad de hacerse dano a si
mismo: reacciona a una caida moviendo gigabytes, justo cuando el cluster ya va justo.

1. **Espera de gracia.** No se programa nada hasta que el nodo lleva
   `DFSHA_REREPLICATION_GRACE_MS` muerto. Reiniciar un contenedor tarda segundos; copiar
   su disco entero por un reinicio es el error clasico, y ademas suele encadenarse: la
   copia satura la red, otro nodo deja de latir a tiempo, y se dispara otra copia.
2. **Tope por destino.** Como mucho `DFSHA_REREPLICATION_MAX_PER_NODE` copias
   simultaneas hacia el mismo DataNode. Sin esto, la recuperacion de un nodo caido se
   concentra en el que quede mas vacio y lo tumba por saturacion.
3. **Prioridad por copias restantes.** Un bloque con UNA sola copia va antes que uno con
   dos. No es una optimizacion: uno esta a un fallo de desaparecer y el otro todavia
   tolera una caida. Si el cluster no da abasto, importa mucho en que orden se rinde.

## Por que el destino tira y no el origen empuja

La orden se le manda al DESTINO, que descarga el bloque del origen. Asi la manda quien
tiene que hacer el trabajo y puede negarse si no le cabe, y no se carga de escrituras a
un nodo que quiza esta sirviendo lecturas.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "ReplicationGap",
    "Assignment",
    "prioritise",
    "assign_targets",
]


@dataclass(frozen=True, slots=True)
class ReplicationGap:
    """Un bloque al que le faltan copias, con lo necesario para decidir."""

    block_id: str
    size: int
    checksum_sha256: str | None
    #: Copias STORED que quedan ahora mismo.
    current_replicas: int
    #: Cuantas deberia tener.
    target_replicas: int
    #: Nodos que YA lo tienen. Ni origen ni destino pueden salirse de aqui a la ligera:
    #: el origen tiene que ser uno de estos, y el destino no puede serlo.
    holders: tuple[str, ...] = ()
    #: Dominios de falla ya cubiertos, para no juntar dos copias donde ya hay una.
    holder_domains: tuple[str, ...] = ()

    @property
    def missing(self) -> int:
        return max(0, self.target_replicas - self.current_replicas)

    @property
    def critical(self) -> bool:
        """Una sola copia: a un fallo de perderse."""
        return self.current_replicas == 1


@dataclass(frozen=True, slots=True)
class Assignment:
    """Una copia concreta: de donde a donde."""

    block_id: str
    source_node_id: str
    target_node_id: str
    gap: ReplicationGap


def prioritise(gaps: list[ReplicationGap]) -> list[ReplicationGap]:
    """Ordena la cola: menos copias primero.

    El desempate es por `block_id` y no por antiguedad a proposito. Parece que lo justo
    seria atender antes al que lleva mas tiempo esperando, pero eso hace que la cola
    dependa de cuando se detecto cada hueco, que a su vez depende de cuando paso el
    evaluador: el mismo cluster en el mismo estado produciria ordenes distintas segun la
    suerte de los tiempos. Con un desempate estable, dos ControlNodes que miren lo mismo
    deciden lo mismo, y eso hace el sistema reproducible y las pruebas deterministas.

    Un bloque con CERO copias va el primero de todos, aunque no haya nada que copiar: asi
    aparece en los logs en vez de desaparecer en silencio.
    """
    return sorted(gaps, key=lambda g: (g.current_replicas, g.block_id))


def assign_targets(
    gaps: list[ReplicationGap],
    candidates_by_domain: dict[str, str],
    in_flight_by_target: dict[str, int],
    max_per_target: int,
    max_total: int | None = None,
    max_per_block: int = 1,
) -> list[Assignment]:
    """Reparte la cola entre los nodos destino, respetando los topes.

    `candidates_by_domain` son los nodos ALIVE con sitio, como `{node_id: fault_domain}`.
    `in_flight_by_target` es lo que cada uno ya tiene en marcha.

    ## `max_per_block=1`, y no es una limitacion arbitraria

    La cola guarda **una tarea viva por bloque**: lo impone el indice unico parcial sobre
    `(block_id)` en los estados PENDING e IN_FLIGHT, que es lo que impide que dos lideres
    solapados programen la misma copia dos veces. Devolver aqui dos asignaciones para el
    mismo bloque produce dos despachos sobre **la misma fila**, y el segundo pisa al
    primero: queda una copia programada en vez de dos, y una fila PENDING en
    `block_replicas` apuntando a un destino que nunca recibira la orden.

    Asi que el dominio respeta el mismo invariante que el esquema. Un bloque al que le
    faltan dos copias recupera una por pasada: la segunda se programa cuando la primera
    ha terminado. Ademas de correcto es preferible, y por el mismo motivo que los otros
    tres frenos: reparte la recuperacion en el tiempo en vez de concentrarla.

    `max_per_block` mayor que 1 solo tiene sentido para probar la funcion en aislamiento;
    quien la llama de verdad usa el default.

    Se prefiere un destino en un dominio de falla que el bloque no cubra todavia; si no
    queda ninguno, se relaja el dominio pero **nunca se elige un nodo que ya tenga el
    bloque**: eso no seria una copia mas, seria un 409.

    Un hueco que no se puede cubrir ahora no se fuerza: se queda en la cola y se vuelve a
    mirar en la siguiente pasada. Es preferible a mandar la copia a un sitio malo.
    """
    asignaciones: list[Assignment] = []
    en_vuelo = dict(in_flight_by_target)

    for hueco in prioritise(gaps):
        if max_total is not None and len(asignaciones) >= max_total:
            break
        if not hueco.holders:
            # Sin ninguna copia viva no hay de donde copiar. Se deja en la cola: si el
            # nodo vuelve, se resuelve solo; si no, es una perdida de datos que ninguna
            # re-replicacion puede arreglar, y fingir lo contrario seria peor.
            continue

        # El origen es el primero de los que lo tienen. Cualquiera vale: todos tienen los
        # mismos bytes, y el checksum se verifica en el destino de todas formas.
        origen = hueco.holders[0]

        for _ in range(min(hueco.missing, max_per_block)):
            destino = _elegir_destino(hueco, asignaciones, candidates_by_domain, en_vuelo, max_per_target)
            if destino is None:
                break
            asignaciones.append(
                Assignment(
                    block_id=hueco.block_id,
                    source_node_id=origen,
                    target_node_id=destino,
                    gap=hueco,
                )
            )
            en_vuelo[destino] = en_vuelo.get(destino, 0) + 1

    return asignaciones


def _elegir_destino(
    hueco: ReplicationGap,
    ya_asignado: list[Assignment],
    candidatos: dict[str, str],
    en_vuelo: dict[str, int],
    max_per_target: int,
) -> str | None:
    ocupados = {a.target_node_id for a in ya_asignado if a.block_id == hueco.block_id}
    prohibidos = set(hueco.holders) | ocupados

    disponibles = [
        (node_id, dominio)
        for node_id, dominio in candidatos.items()
        if node_id not in prohibidos and en_vuelo.get(node_id, 0) < max_per_target
    ]
    if not disponibles:
        return None

    dominios_cubiertos = set(hueco.holder_domains)
    # Los dominios de los destinos ya elegidos para ESTE bloque tambien cuentan.
    dominios_cubiertos |= {
        candidatos[a.target_node_id]
        for a in ya_asignado
        if a.block_id == hueco.block_id and a.target_node_id in candidatos
    }

    preferentes = [n for n, d in disponibles if d not in dominios_cubiertos]
    elegibles = preferentes or [n for n, _ in disponibles]

    # El menos ocupado, con desempate estable por id: dos ControlNodes que vean lo mismo
    # tienen que decidir lo mismo.
    return min(elegibles, key=lambda n: (en_vuelo.get(n, 0), n))
