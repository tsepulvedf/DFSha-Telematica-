"""La re-replicacion tambien esta detrás de la epoca.

El Bloque A puso la regla: si escribe y exige liderazgo, recibe `Fencing` y lo verifica
dentro de su transaccion. Esto comprueba que el Bloque B la cumple, y no de pasada: dos
lideres programando copias a la vez no duplicarian un evento de log, duplicarian el
trafico de copia de un cluster que ya se esta recuperando de una caida.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from dfsha.common.errors import StaleEpochError
from dfsha.control_node.commands.leadership import acquire_or_renew
from dfsha.control_node.commands.rereplication import (
    RereplicationPolicy,
    dispatch,
    scan_and_enqueue,
)
from dfsha.control_node.domain.entities import (
    Block,
    BlockReplica,
    Directory,
    File,
    FileState,
    NodeStats,
    ReplicaState,
    User,
)
from dfsha.control_node.domain.leadership import Fencing, LeaseTimings
from dfsha.control_node.domain.membership import MembershipThresholds
from dfsha.control_node.repositories.database import (
    build_engine,
    build_session_factory,
    create_schema,
)
from dfsha.control_node.repositories.sql import SqlUnitOfWork, new_id

T0 = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
TIMINGS = LeaseTimings.from_millis(ttl_ms=6000, renew_ms=2000)
UMBRALES = MembershipThresholds.from_millis(suspect_after_ms=10_000, dead_after_ms=30_000)
POLITICA = RereplicationPolicy(
    replication_factor=3,
    grace=timedelta(minutes=5),
    max_per_node=2,
)
MB = 1024 * 1024


@pytest.fixture()
def uow_factory():
    engine = build_engine("sqlite://")
    create_schema(engine)
    factory = build_session_factory(engine)
    conexion = engine.connect()
    try:
        yield lambda: SqlUnitOfWork(factory)
    finally:
        conexion.close()
        engine.dispose()


def _cluster_con_hueco(uow_factory, ahora: datetime) -> str:
    """Un archivo COMMITTED de un bloque con UNA sola copia viva y un nodo muerto.

    El nodo que tenia la segunda copia lleva callado mucho mas que `dead_after + gracia`,
    asi que el hueco esta asentado y toca programar la copia.
    """
    with uow_factory() as uow:
        usuario = User(id=new_id(), username="ana", password_hash="x", created_at=ahora)
        uow.users.add(usuario)
        raiz = Directory(
            id=new_id(), parent_id=None, name="", owner_id=usuario.id, created_at=ahora
        )
        uow.directories.add(raiz)

        archivo = File(
            id=new_id(),
            directory_id=raiz.id,
            name="datos.bin",
            owner_id=usuario.id,
            size=MB,
            block_size=MB,
            state=FileState.COMMITTED,
            created_at=ahora,
            committed_at=ahora,
        )
        uow.files.add(archivo)

        nodos = []
        for indice in range(4):
            nodo = uow.data_nodes.register(
                advertise_url=f"http://127.0.0.1:800{indice + 1}",
                capacity_bytes=10 * 1024**3,
                now=ahora - timedelta(hours=1),
                fault_domain=f"zona-{indice}",
                boot_id=f"boot-{indice}",
            )
            nodos.append(nodo)

        stats = NodeStats(
            used_bytes=0,
            capacity_bytes=10 * 1024**3,
            disk_free_bytes=10 * 1024**3,
            block_count=1,
        )
        # Tres siguen latiendo; el cuarto lleva una hora callado.
        for nodo in nodos[:3]:
            uow.data_nodes.record_heartbeat(nodo.id, 1, stats, ahora)
        uow.data_nodes.record_heartbeat(nodos[3].id, 1, stats, ahora - timedelta(hours=1))

        block_id = new_id()
        uow.blocks.add_plan(
            [Block(block_id=block_id, file_id=archivo.id, index=0, size=MB)],
            [
                BlockReplica(
                    block_id=block_id,
                    data_node_id=nodos[0].id,
                    state=ReplicaState.STORED,
                    created_at=ahora,
                ),
                BlockReplica(
                    block_id=block_id,
                    data_node_id=nodos[3].id,  # el muerto
                    state=ReplicaState.STORED,
                    created_at=ahora,
                ),
            ],
        )
        uow.commit()
        return block_id


def test_una_epoca_vieja_no_puede_programar_una_copia(uow_factory) -> None:
    """El lider congelado, aplicado a la re-replicacion."""
    _cluster_con_hueco(uow_factory, T0)

    lease_a = acquire_or_renew(uow_factory(), "A", TIMINGS, now=T0)
    assert lease_a is not None
    fencing_de_a = Fencing("A", lease_a.epoch)

    # A se congela. B toma el lease.
    despues = T0 + timedelta(seconds=40)
    lease_b = acquire_or_renew(uow_factory(), "B", TIMINGS, now=despues)
    assert lease_b is not None and lease_b.epoch == 2

    with pytest.raises(StaleEpochError):
        scan_and_enqueue(uow_factory(), fencing_de_a, POLITICA, UMBRALES, now=despues)

    # Y no dejo nada en la cola.
    with uow_factory() as uow:
        assert uow.rereplication.counts_by_state() == {}


def test_una_epoca_vieja_no_puede_despachar(uow_factory) -> None:
    """El despacho es el paso que de verdad manda trabajo a los nodos: si la epoca no se
    comprobara aqui, un lider congelado podria duplicar el trafico de copia."""
    _cluster_con_hueco(uow_factory, T0)

    lease_a = acquire_or_renew(uow_factory(), "A", TIMINGS, now=T0)
    assert lease_a is not None
    despues = T0 + timedelta(seconds=40)
    acquire_or_renew(uow_factory(), "B", TIMINGS, now=despues)

    with pytest.raises(StaleEpochError):
        dispatch(uow_factory(), Fencing("A", lease_a.epoch), POLITICA, UMBRALES, now=despues)


def test_el_lider_vigente_si_programa_y_despacha(uow_factory) -> None:
    """La otra mitad: la operacion no estaba mal, lo estaba quien la pedia."""
    block_id = _cluster_con_hueco(uow_factory, T0)

    lease = acquire_or_renew(uow_factory(), "A", TIMINGS, now=T0)
    assert lease is not None
    fencing = Fencing("A", lease.epoch)

    encolados = scan_and_enqueue(uow_factory(), fencing, POLITICA, UMBRALES, now=T0)
    assert encolados == 1

    asignaciones = dispatch(uow_factory(), fencing, POLITICA, UMBRALES, now=T0)

    assert len(asignaciones) == 2, "faltaban dos copias para llegar a R=3"
    for asignacion in asignaciones:
        assert asignacion.block_id == block_id

    # Y la eleccion quedo REGISTRADA en block_replicas, que es lo que permite que el
    # destino pueda confirmar su copia cuando la termine.
    with uow_factory() as uow:
        replicas = uow.blocks.list_replicas([block_id])[block_id]
        pendientes = [r for r in replicas if r.state is ReplicaState.PENDING]
        assert len(pendientes) == 2
        destinos = {a.target_node_id for a in asignaciones}
        assert {r.data_node_id for r in pendientes} == destinos


def test_el_hueco_no_asentado_no_se_programa(uow_factory) -> None:
    """La espera de gracia, en el nivel del caso de uso.

    El nodo lleva callado menos de `dead_after + gracia`, asi que todavia puede volver.
    """
    _cluster_con_hueco(uow_factory, T0)
    lease = acquire_or_renew(uow_factory(), "A", TIMINGS, now=T0)
    assert lease is not None

    # T0 - 1h fue el ultimo latido del nodo muerto; se mira un instante en el que solo
    # han pasado 40 s desde entonces.
    apenas_muerto = T0 - timedelta(hours=1) + timedelta(seconds=40)
    encolados = scan_and_enqueue(
        uow_factory(), Fencing("A", lease.epoch), POLITICA, UMBRALES, now=apenas_muerto
    )

    assert encolados == 0
