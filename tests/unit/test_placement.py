"""Politica de colocacion: filtrado, power of d choices y dominios de falla."""

from __future__ import annotations

import random
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

from dfsha.common.errors import NoDataNodeAvailableError
from dfsha.control_node.domain.entities import DataNode, DataNodeState, NodeStats
from dfsha.control_node.domain.membership import MembershipThresholds
from dfsha.control_node.services.placement import LeastLoadedPlacement

MB = 1024 * 1024
GB = 1024 * MB
AHORA = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)
UMBRALES = MembershipThresholds.from_millis(10_000, 30_000)


def nodo(
    id_: str,
    *,
    dominio: str = "local-1",
    usado: int = 0,
    capacidad: int = 100 * GB,
    libre: int = 100 * GB,
    escrituras: int = 0,
    hace_segundos: float | None = 1.0,
) -> DataNode:
    return DataNode(
        id=id_,
        advertise_url=f"http://{id_}:8001",
        capacity_bytes=capacidad,
        used_bytes=usado,
        state=DataNodeState.ALIVE,
        registered_at=AHORA - timedelta(seconds=300),
        fault_domain=dominio,
        boot_id=f"boot-{id_}",
        last_heartbeat_at=(
            None if hace_segundos is None else AHORA - timedelta(seconds=hace_segundos)
        ),
        stats=NodeStats(
            used_bytes=usado,
            capacity_bytes=capacidad,
            disk_free_bytes=libre,
            writes_in_flight=escrituras,
        ),
    )


class RepoFalso:
    def __init__(self, nodos: list[DataNode]) -> None:
        self._nodos = nodos

    def list_all(self) -> list[DataNode]:
        return list(self._nodos)


def politica(nodos: list[DataNode], *, d: int = 3, min_free: int = 0, semilla: int = 1):
    return LeastLoadedPlacement(
        RepoFalso(nodos),
        thresholds=UMBRALES,
        d=d,
        min_free_bytes=min_free,
        rng=random.Random(semilla),
        clock=lambda: AHORA,
    )


class TestFiltrado:
    def test_solo_entran_los_alive(self) -> None:
        nodos = [
            nodo("vivo", hace_segundos=1),
            nodo("sospechoso", hace_segundos=15),   # SUSPECT
            nodo("muerto", hace_segundos=120),      # DEAD
        ]
        elegidos = politica(nodos).select(MB, 1)
        assert [n.id for n in elegidos] == ["vivo"]

    def test_un_suspect_queda_fuera_aunque_sea_el_mas_vacio(self) -> None:
        # Puede estar vivo, pero mandarle bloques nuevos es apostar a que vuelve.
        nodos = [
            nodo("vacio-sospechoso", usado=0, hace_segundos=15),
            nodo("lleno-vivo", usado=90 * GB, hace_segundos=1),
        ]
        assert [n.id for n in politica(nodos).select(MB, 1)] == ["lleno-vivo"]

    def test_el_espacio_insuficiente_descarta_aunque_sea_el_menos_cargado(self) -> None:
        nodos = [
            nodo("vacio-sin-disco", usado=0, libre=10 * MB),
            nodo("cargado-con-disco", usado=80 * GB, libre=50 * GB),
        ]
        # Pide 20 MB con un margen de 100 MB: el primero no llega.
        elegidos = politica(nodos, min_free=100 * MB).select(20 * MB, 1)
        assert [n.id for n in elegidos] == ["cargado-con-disco"]

    def test_un_nodo_registrado_que_nunca_latio_no_es_candidato(self) -> None:
        # No hay dato de espacio libre en el que confiar.
        nodos = [nodo("recien-llegado", hace_segundos=None), nodo("con-latido")]
        assert [n.id for n in politica(nodos).select(MB, 1)] == ["con-latido"]

    def test_el_margen_se_suma_al_tamano_del_bloque(self) -> None:
        justo = nodo("justo", libre=64 * MB + 128 * MB)
        corto = nodo("corto", libre=64 * MB + 128 * MB - 1)
        assert politica([justo], min_free=128 * MB).select(64 * MB, 1)[0].id == "justo"
        with pytest.raises(NoDataNodeAvailableError):
            politica([corto], min_free=128 * MB).select(64 * MB, 1)


class TestPowerOfDChoices:
    def test_elige_entre_los_d_menos_cargados(self) -> None:
        nodos = [
            nodo("n1", usado=10 * GB),
            nodo("n2", usado=20 * GB),
            nodo("n3", usado=30 * GB),
            nodo("n4", usado=90 * GB),   # fuera de la ventana d=3
        ]
        vistos = {
            politica(nodos, d=3, semilla=s).select(MB, 1)[0].id for s in range(50)
        }
        assert vistos <= {"n1", "n2", "n3"}
        assert "n4" not in vistos

    def test_no_se_concentra_en_uno_solo(self) -> None:
        """Prueba anti-manada.

        Coger siempre el nodo mas vacio manda todas las escrituras concurrentes al mismo
        sitio hasta el siguiente heartbeat, que es cuando el ControlNode se entera de que
        ya no esta vacio. Con d=3 el reparto se suaviza.
        """
        nodos = [nodo(f"n{i}", usado=i * GB) for i in range(1, 5)]
        rng = random.Random(7)
        politica_compartida = LeastLoadedPlacement(
            RepoFalso(nodos),
            thresholds=UMBRALES,
            d=3,
            min_free_bytes=0,
            rng=rng,
            clock=lambda: AHORA,
        )

        conteo = Counter(
            politica_compartida.select(MB, 1)[0].id for _ in range(1000)
        )

        assert len(conteo) == 3, "deberia repartir entre los 3 menos cargados"
        for nodo_id, veces in conteo.items():
            assert 250 < veces < 420, f"{nodo_id} se lleva {veces}/1000, reparto sesgado"

    def test_un_archivo_entero_alcanza_a_todos_los_nodos(self) -> None:
        """Los bloques de un `put` se colocan TODOS en la misma llamada a
        `/files/create`, con una unica foto de carga: la del ultimo heartbeat.

        Sin contar lo ya asignado dentro de la propia llamada, cuatro nodos igual de
        vacios y d=3 significan que el cuarto no entra en la ventana ni una sola vez en
        todo el archivo, por muy aleatoria que sea la eleccion. Es el mismo efecto manada
        que el power of d evita entre peticiones, reaparecido dentro de una.
        """
        nodos = [nodo(f"n{i}", usado=0, capacidad=10 * GB, libre=10 * GB) for i in range(1, 5)]
        p = politica(nodos, d=3, semilla=3)

        # 50 bloques de 1 MB: el archivo de la prueba de aceptacion.
        reparto = Counter(p.select(MB, 1)[0].id for _ in range(50))

        assert len(reparto) == 4, f"algun nodo se quedo sin bloques: {dict(reparto)}"
        assert max(reparto.values()) <= 30, f"reparto concentrado: {dict(reparto)}"

    def test_sin_contar_lo_asignado_el_cuarto_nodo_nunca_entraria(self) -> None:
        # Documenta el sesgo que la prueba de arriba evita: una politica nueva por
        # bloque (sin memoria) deja fuera al cuarto nodo de forma sistematica.
        nodos = [nodo(f"n{i}", usado=0, capacidad=10 * GB, libre=10 * GB) for i in range(1, 5)]
        reparto = Counter(
            politica(nodos, d=3, semilla=s).select(MB, 1)[0].id for s in range(50)
        )
        assert "n4" not in reparto

    def test_con_d_igual_a_1_siempre_gana_el_menos_cargado(self) -> None:
        nodos = [nodo("lleno", usado=90 * GB), nodo("vacio", usado=1 * GB)]
        assert {politica(nodos, d=1, semilla=s).select(MB, 1)[0].id for s in range(20)} == {
            "vacio"
        }

    def test_desempata_por_escrituras_en_vuelo(self) -> None:
        # Misma ocupacion: gana el que tiene menos trabajo en curso.
        nodos = [
            nodo("ocupado", usado=10 * GB, escrituras=9),
            nodo("libre", usado=10 * GB, escrituras=0),
        ]
        assert politica(nodos, d=1).select(MB, 1)[0].id == "libre"


class TestDominiosDeFalla:
    def test_r3_usa_tres_nodos_y_tres_dominios(self) -> None:
        nodos = [
            nodo("a1", dominio="zona-a"),
            nodo("b1", dominio="zona-b"),
            nodo("c1", dominio="zona-c"),
            nodo("a2", dominio="zona-a"),
        ]
        elegidos = politica(nodos).select(MB, 3)

        assert len({n.id for n in elegidos}) == 3
        assert len({n.fault_domain for n in elegidos}) == 3

    def test_con_dos_dominios_relaja_pero_no_repite_nodo(self) -> None:
        nodos = [
            nodo("a1", dominio="zona-a"),
            nodo("a2", dominio="zona-a"),
            nodo("b1", dominio="zona-b"),
        ]
        elegidos = politica(nodos).select(MB, 3)

        assert len({n.id for n in elegidos}) == 3, "dos replicas en el mismo nodo no son dos"
        assert len({n.fault_domain for n in elegidos}) == 2  # relajado

    def test_nunca_dos_replicas_en_el_mismo_nodo(self) -> None:
        # Un solo dominio: la relajacion se aplica a todas las replicas.
        nodos = [nodo(f"n{i}", dominio="unica") for i in range(1, 6)]
        for semilla in range(30):
            elegidos = politica(nodos, semilla=semilla).select(MB, 4)
            assert len({n.id for n in elegidos}) == 4

    def test_r1_no_relaja_nada(self) -> None:
        nodos = [nodo("a1", dominio="zona-a"), nodo("a2", dominio="zona-a")]
        assert len(politica(nodos).select(MB, 1)) == 1


class TestErrores:
    def test_sin_nodos_registrados(self) -> None:
        with pytest.raises(NoDataNodeAvailableError) as excinfo:
            politica([]).select(MB, 1)
        assert "no hay ningun DataNode registrado" in excinfo.value.message

    def test_el_error_dice_cuantos_se_pidieron_y_por_que_se_descarto_cada_uno(self) -> None:
        nodos = [
            nodo("muerto", hace_segundos=120),
            nodo("sin-disco", libre=1 * MB),
            nodo("bueno"),
        ]
        with pytest.raises(NoDataNodeAvailableError) as excinfo:
            politica(nodos, min_free=100 * MB).select(MB, 3)

        error = excinfo.value
        assert error.details["requested"] == 3
        assert error.details["eligible"] == 1
        assert error.details["evaluated"] == 3

        descartes = " | ".join(error.details["rejected"])
        assert "DEAD" in descartes
        assert "espacio insuficiente" in descartes
        # El mensaje nombra el nodo y su dominio, no solo un id opaco.
        assert "http://muerto:8001" in descartes

    def test_nunca_coloca_menos_replicas_de_las_pedidas(self) -> None:
        # Dos nodos validos y se piden tres: falla en vez de devolver dos en silencio.
        nodos = [nodo("n1", dominio="a"), nodo("n2", dominio="b")]
        with pytest.raises(NoDataNodeAvailableError):
            politica(nodos).select(MB, 3)

    def test_factor_de_replicacion_invalido(self) -> None:
        with pytest.raises(NoDataNodeAvailableError):
            politica([nodo("n1")]).select(MB, 0)


class TestEvaluacion:
    def test_evaluate_explica_cada_nodo(self) -> None:
        nodos = [nodo("bueno"), nodo("muerto", hace_segundos=120)]
        evaluados = {c.node.id: c for c in politica(nodos).evaluate(MB)}

        assert evaluados["bueno"].eligible is True
        assert evaluados["bueno"].reason == ""
        assert evaluados["muerto"].eligible is False
        assert evaluados["muerto"].state is DataNodeState.DEAD
