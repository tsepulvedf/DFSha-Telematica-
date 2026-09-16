"""Cola de re-replicacion: prioridad y topes.

Funciones puras, sin base de datos ni red. Lo que se comprueba es lo que decide si la
recuperacion de una caida ayuda o empeora las cosas: en que orden se rinde la cola cuando
no da abasto, y que ningun destino reciba mas copias de las que puede tragar.
"""

from __future__ import annotations

from dfsha.control_node.domain.rereplication import (
    ReplicationGap,
    assign_targets,
    prioritise,
)

# Cuatro nodos en cuatro dominios, como el compose local.
NODOS = {"n1": "zona-a", "n2": "zona-b", "n3": "zona-c", "n4": "zona-d"}


def hueco(
    block_id: str,
    copias: int,
    holders: tuple[str, ...] = ("n1",),
    dominios: tuple[str, ...] = ("zona-a",),
    objetivo: int = 3,
) -> ReplicationGap:
    return ReplicationGap(
        block_id=block_id,
        size=1024,
        checksum_sha256="a" * 64,
        current_replicas=copias,
        target_replicas=objetivo,
        holders=holders,
        holder_domains=dominios,
    )


# --- Prioridad -------------------------------------------------------------


def test_menos_copias_va_primero() -> None:
    """No es una optimizacion: un bloque con una copia esta a un fallo de desaparecer y
    uno con dos todavia tolera una caida. Si el cluster no da abasto, el orden en que se
    rinde es lo que decide si se pierden datos."""
    cola = [hueco("b-dos", 2), hueco("b-una", 1), hueco("b-cero", 0)]

    assert [g.block_id for g in prioritise(cola)] == ["b-cero", "b-una", "b-dos"]


def test_el_orden_es_estable_y_no_depende_del_reloj() -> None:
    """Desempate por block_id y no por antiguedad: asi dos ControlNodes que miren el
    mismo estado deciden lo mismo, y la cola no cambia segun cuando paso el evaluador."""
    cola = [hueco("b-c", 2), hueco("b-a", 2), hueco("b-b", 2)]

    assert [g.block_id for g in prioritise(cola)] == ["b-a", "b-b", "b-c"]
    assert prioritise(cola) == prioritise(list(reversed(cola)))


def test_un_bloque_critico_se_reconoce() -> None:
    assert hueco("b", 1).critical
    assert not hueco("b", 2).critical
    assert hueco("b", 2).missing == 1


# --- Asignacion de destinos ------------------------------------------------


def test_por_defecto_se_asigna_UNA_copia_por_bloque_y_pasada() -> None:
    """La cola guarda una tarea viva por bloque, y el dominio respeta ese invariante.

    Devolver dos asignaciones para el mismo bloque produciria dos despachos sobre la
    MISMA fila y el segundo pisaria al primero: quedaria una copia programada en vez de
    dos, y una fila PENDING apuntando a un destino que nunca recibe la orden. Lo destapo
    `test_la_orden_lleva_la_direccion_de_PAR_del_origen`, que pedia la orden del primer
    destino y no encontraba ninguna.

    Un bloque al que le faltan dos copias recupera una por pasada.
    """
    asignaciones = assign_targets(
        [hueco("b1", 1)], NODOS, in_flight_by_target={}, max_per_target=2
    )

    assert len(asignaciones) == 1
    assert asignaciones[0].target_node_id not in hueco("b1", 1).holders


def test_con_max_per_block_mayor_se_asignan_todas_las_que_faltan() -> None:
    """La funcion sabe repartir N; quien la llama pide 1 por el invariante de la cola."""
    asignaciones = assign_targets(
        [hueco("b1", 1)], NODOS, {}, max_per_target=2, max_per_block=3
    )

    assert len(asignaciones) == 2  # de 1 copia a 3
    assert len({a.target_node_id for a in asignaciones}) == 2


def test_nunca_se_elige_un_nodo_que_ya_tiene_el_bloque() -> None:
    """Mandar la copia a quien ya la tiene no es una replica mas, es un 409."""
    g = hueco("b1", 2, holders=("n1", "n2"), dominios=("zona-a", "zona-b"))

    asignaciones = assign_targets([g], NODOS, {}, max_per_target=2)

    assert len(asignaciones) == 1
    assert asignaciones[0].target_node_id not in ("n1", "n2")


def test_se_prefiere_un_dominio_de_falla_sin_cubrir() -> None:
    g = hueco("b1", 1, holders=("n1",), dominios=("zona-a",))

    asignaciones = assign_targets([g], NODOS, {}, max_per_target=2, max_per_block=3)

    dominios = {NODOS[a.target_node_id] for a in asignaciones}
    assert "zona-a" not in dominios
    assert len(dominios) == 2, "las dos copias nuevas cayeron en el mismo dominio"


def test_sin_dominios_libres_se_relaja_pero_no_se_repite_nodo() -> None:
    """Misma regla que la colocacion de la Etapa 2: el dominio se puede relajar, el nodo
    nunca. Dos copias en el mismo disco se pierden juntas."""
    dos_dominios = {"n1": "zona-a", "n2": "zona-a", "n3": "zona-b", "n4": "zona-b"}
    g = hueco("b1", 2, holders=("n1", "n3"), dominios=("zona-a", "zona-b"))

    asignaciones = assign_targets([g], dos_dominios, {}, max_per_target=2)

    assert len(asignaciones) == 1
    assert asignaciones[0].target_node_id in ("n2", "n4")


def test_el_tope_por_destino_se_respeta() -> None:
    """Sin esto, la recuperacion de un nodo caido se concentra en el que este mas vacio
    y lo tumba por saturacion."""
    huecos = [hueco(f"b{i}", 2, holders=("n1",), dominios=("zona-a",)) for i in range(10)]

    asignaciones = assign_targets(huecos, NODOS, {}, max_per_target=2)

    por_destino: dict[str, int] = {}
    for a in asignaciones:
        por_destino[a.target_node_id] = por_destino.get(a.target_node_id, 0) + 1

    assert por_destino, "no se asigno ninguna copia"
    assert max(por_destino.values()) <= 2


def test_lo_que_ya_esta_en_vuelo_cuenta_para_el_tope() -> None:
    """El tope es de copias SIMULTANEAS, no de copias por pasada. Ignorar lo que ya esta
    en marcha convertiria el limite en uno nuevo cada vez que corre el planificador."""
    huecos = [hueco(f"b{i}", 2, holders=("n1",), dominios=("zona-a",)) for i in range(6)]
    en_vuelo = {"n2": 2, "n3": 2, "n4": 1}

    asignaciones = assign_targets(huecos, NODOS, en_vuelo, max_per_target=2)

    assert [a.target_node_id for a in asignaciones] == ["n4"]


def test_un_bloque_sin_ninguna_copia_no_se_programa() -> None:
    """No hay de donde copiar. Se deja en la cola en vez de inventarse un origen: si el
    nodo vuelve se resuelve solo, y si no, es una perdida que ninguna re-replicacion
    puede arreglar."""
    asignaciones = assign_targets([hueco("b1", 0, holders=())], NODOS, {}, max_per_target=2)

    assert asignaciones == []


def test_los_criticos_se_llevan_el_cupo_antes_que_los_demas() -> None:
    """El escenario que junta las dos reglas: cupo escaso y huecos de distinta urgencia.
    Los de una sola copia tienen que salir primero."""
    huecos = [
        hueco("b-dos-a", 2, holders=("n1", "n2"), dominios=("zona-a", "zona-b")),
        hueco("b-una", 1, holders=("n1",), dominios=("zona-a",)),
        hueco("b-dos-b", 2, holders=("n1", "n2"), dominios=("zona-a", "zona-b")),
    ]
    # Solo queda sitio para dos copias en todo el cluster.
    asignaciones = assign_targets(
        huecos, NODOS, {"n2": 2, "n3": 1, "n4": 2}, max_per_target=2
    )

    assert [a.block_id for a in asignaciones] == ["b-una"]


def test_max_total_corta_la_pasada() -> None:
    """El planificador no vacia la cola de golpe: reparte por pasadas para que el cluster
    respire entre tandas."""
    huecos = [hueco(f"b{i}", 2, holders=("n1",), dominios=("zona-a",)) for i in range(10)]

    asignaciones = assign_targets(huecos, NODOS, {}, max_per_target=5, max_total=3)

    assert len(asignaciones) == 3


def test_sin_candidatos_no_se_fuerza_nada() -> None:
    """Un hueco que no se puede cubrir ahora se queda en la cola. Mandar la copia a un
    sitio malo es peor que esperar a la siguiente pasada."""
    solo_el_que_lo_tiene = {"n1": "zona-a"}

    assert assign_targets([hueco("b1", 1)], solo_el_que_lo_tiene, {}, 2) == []
