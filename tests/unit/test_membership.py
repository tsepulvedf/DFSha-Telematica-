"""Maquina de estados del DataNode y reincorporacion tras un reinicio.

Reloj inyectado: comprobar que un nodo pasa a DEAD a los 30 s no puede costar 30 s.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from dfsha.control_node.domain.entities import DataNode, DataNodeState, NodeStats
from dfsha.control_node.domain.membership import (
    NEW_NODE,
    SAME_BOOT,
    WIPED_DISK,
    MembershipThresholds,
    classify_rejoin,
    evaluate,
    state_for,
)

AHORA = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)
UMBRALES = MembershipThresholds.from_millis(suspect_after_ms=10_000, dead_after_ms=30_000)


def nodo(
    *,
    state: DataNodeState = DataNodeState.ALIVE,
    hace_segundos: float | None = 0.0,
    boot_id: str = "boot-1",
    registrado_hace: float = 60.0,
) -> DataNode:
    return DataNode(
        id="dn-1",
        advertise_url="http://localhost:8001",
        capacity_bytes=1000,
        used_bytes=100,
        state=state,
        registered_at=AHORA - timedelta(seconds=registrado_hace),
        fault_domain="local-1",
        boot_id=boot_id,
        last_heartbeat_at=(
            None if hace_segundos is None else AHORA - timedelta(seconds=hace_segundos)
        ),
        stats=NodeStats(used_bytes=100, capacity_bytes=1000, disk_free_bytes=5000),
    )


class TestUmbrales:
    @pytest.mark.parametrize(
        ("silencio", "esperado"),
        [
            (0.0, DataNodeState.ALIVE),
            (3.0, DataNodeState.ALIVE),
            (9.9, DataNodeState.ALIVE),
            (10.0, DataNodeState.SUSPECT),   # el umbral es inclusivo
            (15.0, DataNodeState.SUSPECT),
            (29.9, DataNodeState.SUSPECT),
            (30.0, DataNodeState.DEAD),
            (300.0, DataNodeState.DEAD),
        ],
    )
    def test_estado_segun_el_silencio(self, silencio: float, esperado: DataNodeState) -> None:
        estado = state_for(AHORA - timedelta(seconds=silencio), AHORA, UMBRALES)
        assert estado is esperado

    def test_un_nodo_recien_registrado_no_nace_sospechoso(self) -> None:
        # Todavia no ha mandado ningun heartbeat, pero acaba de registrarse: se le da el
        # mismo margen que a cualquiera en vez de castigarle por no haber latido aun.
        assert (
            state_for(None, AHORA, UMBRALES, registered_at=AHORA - timedelta(seconds=2))
            is DataNodeState.ALIVE
        )

    def test_un_nodo_registrado_hace_mucho_que_nunca_latio_esta_muerto(self) -> None:
        assert (
            state_for(None, AHORA, UMBRALES, registered_at=AHORA - timedelta(seconds=120))
            is DataNodeState.DEAD
        )

    def test_sin_ninguna_referencia_temporal(self) -> None:
        assert state_for(None, AHORA, UMBRALES) is DataNodeState.DEAD

    def test_el_umbral_de_suspect_debe_ser_menor_que_el_de_dead(self) -> None:
        with pytest.raises(ValueError):
            MembershipThresholds.from_millis(suspect_after_ms=30_000, dead_after_ms=10_000)


class TestTransiciones:
    def test_sin_cambio_no_hay_transicion(self) -> None:
        assert evaluate(nodo(hace_segundos=1.0), AHORA, UMBRALES) is None

    def test_alive_a_suspect(self) -> None:
        cambio = evaluate(nodo(hace_segundos=12.0), AHORA, UMBRALES)
        assert cambio is not None
        assert (cambio.previous, cambio.current) == (
            DataNodeState.ALIVE,
            DataNodeState.SUSPECT,
        )
        assert cambio.silence_seconds == 12.0
        assert cambio.fault_domain == "local-1"
        # SUSPECT lo saca de la colocacion, pero sus replicas se siguen sirviendo.
        assert cambio.lost_replicas is False

    def test_suspect_a_dead_pierde_las_replicas(self) -> None:
        cambio = evaluate(
            nodo(state=DataNodeState.SUSPECT, hace_segundos=45.0), AHORA, UMBRALES
        )
        assert cambio is not None
        assert cambio.current is DataNodeState.DEAD
        assert cambio.lost_replicas is True

    def test_alive_a_dead_de_golpe_tambien_las_pierde(self) -> None:
        # Pasa cuando nadie evaluo durante la ventana intermedia.
        cambio = evaluate(nodo(hace_segundos=60.0), AHORA, UMBRALES)
        assert cambio is not None
        assert (cambio.previous, cambio.current) == (
            DataNodeState.ALIVE,
            DataNodeState.DEAD,
        )
        assert cambio.lost_replicas is True

    def test_dead_a_dead_no_vuelve_a_perder_nada(self) -> None:
        # Sin esto, cada evaluacion de un nodo muerto repetiria el evento y volveria a
        # marcar replicas que ya estaban marcadas.
        assert evaluate(nodo(state=DataNodeState.DEAD, hace_segundos=60.0), AHORA, UMBRALES) is None

    @pytest.mark.parametrize("desde", [DataNodeState.SUSPECT, DataNodeState.DEAD])
    def test_recuperacion(self, desde: DataNodeState) -> None:
        cambio = evaluate(nodo(state=desde, hace_segundos=0.5), AHORA, UMBRALES)
        assert cambio is not None
        assert cambio.current is DataNodeState.ALIVE
        assert cambio.recovered is True
        assert cambio.lost_replicas is False


class TestReincorporacion:
    def test_nodo_nuevo(self) -> None:
        assert classify_rejoin(None, "boot-1") == NEW_NODE

    def test_mismo_boot_id_conserva_el_disco(self) -> None:
        assert classify_rejoin(nodo(boot_id="boot-1"), "boot-1") == SAME_BOOT

    def test_otro_boot_id_es_disco_perdido(self) -> None:
        # Reincorporarlo sin mas seria anunciar bloques que ya no existen, y el cliente
        # se los encontraria al descargarlos.
        assert classify_rejoin(nodo(boot_id="boot-1"), "boot-2") == WIPED_DISK

    def test_un_nodo_sin_boot_id_registrado_se_trata_como_disco_perdido(self) -> None:
        assert classify_rejoin(nodo(boot_id=""), "boot-1") == WIPED_DISK
