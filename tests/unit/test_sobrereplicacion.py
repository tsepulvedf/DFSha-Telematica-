"""Un bloque con MAS copias de las pedidas: que pasa y que NO pasa.

Se observo en la validacion en Docker del Bloque B. La secuencia:

1. `data-node-2` muere. El evaluador marca sus replicas MISSING — no las borra.
2. Pasada la gracia, la re-replicacion pone una copia nueva en otro nodo. El bloque
   vuelve a tener 3 copias vivas, mas una fila MISSING del nodo caido.
3. `data-node-2` **vuelve** con su disco intacto, reporta sus bloques, y esa fila MISSING
   vuelve a STORED (`apply_block_report`, que trata un report completo como la verdad
   sobre ese disco).
4. El bloque se queda con **4 copias** cuando R=3.

`dfsha stat` lo mostro como `FULLY_REPLICATED (3-4 de 3 copias por bloque)`.

## Las dos afirmaciones que estas pruebas fijan

**Es un estado normal, no una anomalia.** Nadie registra un error, nadie borra nada, y el
archivo se considera completamente replicado: el estado se deriva del bloque PEOR
replicado, y tener de mas no empeora a ninguno. Borrar la copia sobrante
automaticamente violaria la decision 8 de la Etapa 2 —el ControlNode no borra datos por
una divergencia—, y la violaria en el peor sitio: para deshacer una redundancia que el
propio sistema acaba de conseguir.

**Y NO se limpia sola.** Esta es la parte que hay que tener clara y la que corrige la
intuicion natural: el GC recoge bloques de archivos DELETED y de reservas vencidas
(`list_orphans`), y este bloque pertenece a un archivo COMMITTED y vivo. La cuarta copia
**se queda**. Cuesta disco hasta que alguien decida quitarla; no cuesta correccion.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from dfsha.control_node.commands.control_plane import apply_block_report
from dfsha.control_node.commands.leadership import acquire_or_renew
from dfsha.control_node.commands.rereplication import RereplicationPolicy, scan_and_enqueue
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
from dfsha.control_node.queries.cluster import replication_health
from dfsha.control_node.queries.gc import orphan_blocks
from dfsha.control_node.queries.namespace import stat
from dfsha.control_node.repositories.database import (
    build_engine,
    build_session_factory,
    create_schema,
)
from dfsha.control_node.repositories.sql import SqlUnitOfWork, new_id

T0 = datetime(2026, 9, 16, 12, 0, 0, tzinfo=timezone.utc)
UMBRALES = MembershipThresholds.from_millis(10_000, 30_000)
MB = 1024 * 1024
R = 3


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


@pytest.fixture()
def escenario(uow_factory):
    """El estado justo DESPUES de que la re-replicacion repusiera la copia.

    Dos bloques de un archivo COMMITTED. El bloque A tiene tres copias vivas mas una fila
    MISSING del nodo que se cayo; el bloque B tiene sus tres y nunca estuvo en ese nodo.
    Que sean dos es lo que hace que `stat` muestre un rango (3-4) en vez de un numero
    plano, igual que se vio en Docker.
    """
    with uow_factory() as uow:
        usuario = User(id=new_id(), username="ana", password_hash="x", created_at=T0)
        uow.users.add(usuario)
        raiz = Directory(
            id=new_id(), parent_id=None, name="", owner_id=usuario.id, created_at=T0
        )
        uow.directories.add(raiz)

        archivo = File(
            id=new_id(),
            directory_id=raiz.id,
            name="datos.bin",
            owner_id=usuario.id,
            size=2 * MB,
            block_size=MB,
            state=FileState.COMMITTED,
            created_at=T0,
            committed_at=T0,
        )
        uow.files.add(archivo)

        nodos = []
        for indice in range(4):
            nodos.append(
                uow.data_nodes.register(
                    advertise_url=f"http://localhost:800{indice + 1}",
                    peer_url=f"http://data-node-{indice + 1}:8001",
                    capacity_bytes=10 * 1024**3,
                    now=T0,
                    fault_domain=f"zona-{indice}",
                    boot_id=f"boot-{indice}",
                )
            )
        stats = NodeStats(
            used_bytes=0, capacity_bytes=10 * 1024**3, disk_free_bytes=10 * 1024**3
        )
        for nodo in nodos:
            uow.data_nodes.record_heartbeat(nodo.id, 1, stats, T0)

        bloque_a, bloque_b = new_id(), new_id()
        replicas = []
        # Bloque A: tres copias vivas (0, 2, 3) y la del nodo 1, que murio, en MISSING.
        for nodo in (nodos[0], nodos[2], nodos[3]):
            replicas.append(
                BlockReplica(
                    block_id=bloque_a,
                    data_node_id=nodo.id,
                    state=ReplicaState.STORED,
                    created_at=T0,
                )
            )
        replicas.append(
            BlockReplica(
                block_id=bloque_a,
                data_node_id=nodos[1].id,
                state=ReplicaState.MISSING,
                created_at=T0,
            )
        )
        # Bloque B: sus tres, sin pasar por el nodo que murio.
        for nodo in (nodos[0], nodos[2], nodos[3]):
            replicas.append(
                BlockReplica(
                    block_id=bloque_b,
                    data_node_id=nodo.id,
                    state=ReplicaState.STORED,
                    created_at=T0,
                )
            )

        uow.blocks.add_plan(
            [
                Block(block_id=bloque_a, file_id=archivo.id, index=0, size=MB),
                Block(block_id=bloque_b, file_id=archivo.id, index=1, size=MB),
            ],
            replicas,
        )
        uow.commit()

    return {
        "owner_id": usuario.id,
        "file_id": archivo.id,
        "bloque_a": bloque_a,
        "bloque_b": bloque_b,
        "vuelve": nodos[1].id,
    }


def test_el_nodo_que_vuelve_recupera_su_replica_y_el_bloque_queda_con_cuatro(
    uow_factory, escenario
) -> None:
    """El mecanismo: un report COMPLETO es la verdad sobre ese disco.

    La fila MISSING no se habia borrado —el ControlNode no borra— asi que sigue ahi para
    poder volver a STORED. Es la misma propiedad que permite que una reincorporacion no
    cueste una re-replicacion; aqui llega tarde y el resultado es una copia de mas.
    """
    with uow_factory() as uow:
        antes = uow.blocks.list_replicas([escenario["bloque_a"]])[escenario["bloque_a"]]
        assert sum(1 for r in antes if r.state is ReplicaState.STORED) == 3

    apply_block_report(
        uow_factory(),
        data_node_id=escenario["vuelve"],
        boot_id="boot-1",
        is_full=True,
        reported_block_ids=[escenario["bloque_a"]],
    )

    with uow_factory() as uow:
        despues = uow.blocks.list_replicas([escenario["bloque_a"]])[escenario["bloque_a"]]
        almacenadas = [r for r in despues if r.state is ReplicaState.STORED]

    assert len(almacenadas) == 4, "la replica del nodo que volvio no se recupero"


def test_stat_lo_muestra_como_completamente_replicado(uow_factory, escenario) -> None:
    """3-4 de 3, exactamente lo que se vio en Docker.

    El estado se deriva del bloque PEOR replicado: tener de mas en uno no empeora a
    ninguno, asi que el archivo esta completo. Que el rango se vea es a proposito — un
    numero plano escondería que los bloques no estan todos igual.
    """
    apply_block_report(
        uow_factory(),
        data_node_id=escenario["vuelve"],
        boot_id="boot-1",
        is_full=True,
        reported_block_ids=[escenario["bloque_a"]],
    )

    info = stat(uow_factory(), escenario["owner_id"], "/datos.bin", replication_factor=R)

    assert info.replication_state == "FULLY_REPLICATED"
    assert info.min_replicas == 3
    assert info.max_replicas == 4


def test_no_se_cuenta_como_sub_replicado_ni_como_critico(uow_factory, escenario) -> None:
    apply_block_report(
        uow_factory(),
        data_node_id=escenario["vuelve"],
        boot_id="boot-1",
        is_full=True,
        reported_block_ids=[escenario["bloque_a"]],
    )

    salud = replication_health(uow_factory(), replication_factor=R)

    assert salud.under_replicated == 0
    assert salud.critical == 0


def test_la_rereplicacion_no_programa_nada(uow_factory, escenario) -> None:
    """Sobrar copias no es un hueco. `missing` es max(0, R - actuales), asi que un bloque
    con cuatro da cero y no entra en la cola."""
    apply_block_report(
        uow_factory(),
        data_node_id=escenario["vuelve"],
        boot_id="boot-1",
        is_full=True,
        reported_block_ids=[escenario["bloque_a"]],
    )

    lease = acquire_or_renew(
        uow_factory(), "A", LeaseTimings.from_millis(6000, 2000), now=T0
    )
    assert lease is not None

    encolados = scan_and_enqueue(
        uow_factory(),
        Fencing("A", lease.epoch),
        RereplicationPolicy(replication_factor=R, grace=timedelta(0), max_per_node=2),
        UMBRALES,
        now=T0,
    )

    assert encolados == 0


def test_el_GC_NO_recoge_la_copia_sobrante(uow_factory, escenario) -> None:
    """La afirmacion que corrige la intuicion natural.

    Parece que el GC lo limpiara, y no: el GC recoge bloques de archivos DELETED y de
    reservas vencidas, y este bloque pertenece a un archivo COMMITTED y vivo. La cuarta
    copia **se queda**. Cuesta disco, no correccion.

    Si algun dia se decide recortar las copias sobrantes, hara falta un mecanismo nuevo y
    deliberado, y esta prueba tendra que cambiar a proposito. Lo que no puede pasar es que
    cambie sin que nadie lo note.
    """
    apply_block_report(
        uow_factory(),
        data_node_id=escenario["vuelve"],
        boot_id="boot-1",
        is_full=True,
        reported_block_ids=[escenario["bloque_a"]],
    )

    huerfanos = orphan_blocks(uow_factory())

    assert [b.block_id for b in huerfanos] == []


def test_el_bloque_de_mas_no_se_marca_como_divergencia(uow_factory, escenario) -> None:
    """Un bloque que el nodo tiene y el metadato TAMBIEN espera de el no es un huerfano.

    La diferencia con `divergence.unknown_block` es justo esa: alli el metadato no
    asociaba el bloque a ese nodo; aqui si, porque la fila MISSING nunca se borro.
    """
    informe = apply_block_report(
        uow_factory(),
        data_node_id=escenario["vuelve"],
        boot_id="boot-1",
        is_full=True,
        reported_block_ids=[escenario["bloque_a"]],
    )

    assert informe.unknown_block_ids == ()
    assert informe.missing_block_ids == ()
    assert informe.confirmed_block_ids == (escenario["bloque_a"],)
