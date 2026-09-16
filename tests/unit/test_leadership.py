"""Lease de liderazgo y epoca como token de aislamiento.

La prueba que importa de todo el fichero es `test_el_lider_congelado_es_rechazado_y_no
_escribe_nada`. Las demas comprueban piezas; esa comprueba la propiedad que justifica
que la epoca exista.

Lo que NO se prueba aqui, y conviene tenerlo dicho: la **exclusion mutua real** entre dos
instancias compitiendo a la vez. Depende de `SELECT ... FOR UPDATE`, que SQLite no
implementa y SQLAlchemy omite en ese dialecto. Lo que si se prueba en SQLite es la
comparacion de epoca, que es la mitad del mecanismo que atrapa al lider congelado; la
otra mitad, el cerrojo, solo puede probarse contra PostgreSQL de verdad.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from dfsha.common.errors import NotLeaderError, StaleEpochError
from dfsha.control_node.commands.control_plane import evaluate_membership
from dfsha.control_node.commands.leadership import (
    acquire_or_renew,
    read_lease,
    release_leadership,
    require_leadership,
)
from dfsha.control_node.domain.entities import DataNodeState, NodeStats
from dfsha.control_node.domain.leadership import (
    ACQUIRE,
    RENEW,
    STAND_BY,
    Fencing,
    Lease,
    LeaseTimings,
    decide,
)
from dfsha.control_node.domain.membership import MembershipThresholds
from dfsha.control_node.repositories.database import (
    build_engine,
    build_session_factory,
    create_schema,
)
from dfsha.control_node.repositories.sql import SqlUnitOfWork
from dfsha.control_node.services.leadership import LeadershipService

T0 = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)
TIMINGS = LeaseTimings.from_millis(ttl_ms=6000, renew_ms=2000)
UMBRALES = MembershipThresholds.from_millis(suspect_after_ms=10_000, dead_after_ms=30_000)


@pytest.fixture()
def uow_factory():
    engine = build_engine("sqlite://")  # en memoria
    create_schema(engine)
    factory = build_session_factory(engine)
    conexion = engine.connect()  # mantiene viva la base en memoria
    try:
        yield lambda: SqlUnitOfWork(factory)
    finally:
        conexion.close()
        engine.dispose()


def _lease(leader: str | None, epoch: int, expira: datetime | None) -> Lease:
    return Lease(
        leader_id=leader,
        epoch=epoch,
        acquired_at=T0,
        renewed_at=T0,
        expires_at=expira,
    )


# --- Dominio: la decision, sin base de datos -------------------------------


def test_lease_propio_y_vivo_se_renueva() -> None:
    lease = _lease("A", 7, T0 + timedelta(seconds=6))
    assert decide(lease, "A", T0) == RENEW


def test_lease_de_otro_y_vivo_se_espera() -> None:
    """Con tres instancias, dos estan siempre aqui. No es un fallo."""
    lease = _lease("B", 7, T0 + timedelta(seconds=6))
    assert decide(lease, "A", T0) == STAND_BY


def test_lease_vencido_de_otro_se_toma() -> None:
    lease = _lease("B", 7, T0 - timedelta(seconds=1))
    assert decide(lease, "A", T0) == ACQUIRE


def test_lease_vencido_PROPIO_tambien_se_toma_de_cero() -> None:
    """El caso sutil, y el que sostiene toda la proteccion.

    Si un lease vencido propio se tratara como renovacion, un lider que estuvo congelado
    volveria con su epoca antigua intacta y podria validar operaciones que habia
    empezado antes de la pausa. Tratarlo como adquisicion le sube la epoca, y con ello
    invalida su propio trabajo a medio camino, que es lo correcto: entre una epoca y la
    otra pudo pasar cualquier cosa.
    """
    lease = _lease("A", 7, T0 - timedelta(seconds=1))
    assert decide(lease, "A", T0) == ACQUIRE


def test_la_fila_recien_sembrada_esta_libre() -> None:
    assert decide(_lease(None, 0, None), "A", T0) == ACQUIRE


def test_renovar_mas_lento_que_el_ttl_no_se_acepta() -> None:
    with pytest.raises(ValueError, match="menor que"):
        LeaseTimings.from_millis(ttl_ms=2000, renew_ms=2000)


# --- Adquisicion y renovacion contra la base -------------------------------


def test_el_primero_en_llegar_toma_el_lease_con_epoca_1(uow_factory) -> None:
    lease = acquire_or_renew(uow_factory(), "A", TIMINGS, now=T0)

    assert lease is not None
    assert lease.leader_id == "A"
    assert lease.epoch == 1  # la fila se siembra con 0


def test_renovar_no_cambia_la_epoca(uow_factory) -> None:
    acquire_or_renew(uow_factory(), "A", TIMINGS, now=T0)
    renovado = acquire_or_renew(uow_factory(), "A", TIMINGS, now=T0 + timedelta(seconds=2))

    assert renovado is not None
    assert renovado.epoch == 1
    assert renovado.expires_at == T0 + timedelta(seconds=8)


def test_otra_instancia_no_puede_quitar_un_lease_vivo(uow_factory) -> None:
    acquire_or_renew(uow_factory(), "A", TIMINGS, now=T0)

    assert acquire_or_renew(uow_factory(), "B", TIMINGS, now=T0 + timedelta(seconds=1)) is None


def test_un_lease_vencido_pasa_a_otra_instancia_subiendo_la_epoca(uow_factory) -> None:
    acquire_or_renew(uow_factory(), "A", TIMINGS, now=T0)

    tomado = acquire_or_renew(uow_factory(), "B", TIMINGS, now=T0 + timedelta(seconds=7))

    assert tomado is not None
    assert tomado.leader_id == "B"
    assert tomado.epoch == 2


def test_la_epoca_solo_sube_aunque_el_lease_vaya_y_venga(uow_factory) -> None:
    """Nunca se reutiliza una epoca: es lo unico que hace que sirva como token."""
    epocas = []
    ahora = T0
    for instancia in ("A", "B", "A", "C"):
        lease = acquire_or_renew(uow_factory(), instancia, TIMINGS, now=ahora)
        assert lease is not None
        epocas.append(lease.epoch)
        ahora += timedelta(seconds=7)  # cada vez, el anterior ya vencio

    assert epocas == [1, 2, 3, 4]


def test_soltar_el_lease_lo_deja_libre_sin_bajar_la_epoca(uow_factory) -> None:
    lease = acquire_or_renew(uow_factory(), "A", TIMINGS, now=T0)
    assert lease is not None

    release_leadership(uow_factory(), Fencing("A", lease.epoch))

    libre = read_lease(uow_factory())
    assert libre.leader_id is None
    assert libre.epoch == 1  # la epoca no retrocede

    siguiente = acquire_or_renew(uow_factory(), "B", TIMINGS, now=T0 + timedelta(seconds=1))
    assert siguiente is not None and siguiente.epoch == 2


# --- La comprobacion de epoca ----------------------------------------------


def test_la_epoca_vigente_pasa_la_comprobacion(uow_factory) -> None:
    acquire_or_renew(uow_factory(), "A", TIMINGS, now=T0)

    with uow_factory() as uow:
        lease = require_leadership(uow, Fencing("A", 1), now=T0 + timedelta(seconds=1))
    assert lease.leader_id == "A"


def test_una_epoca_vieja_es_rechazada(uow_factory) -> None:
    acquire_or_renew(uow_factory(), "A", TIMINGS, now=T0)
    acquire_or_renew(uow_factory(), "B", TIMINGS, now=T0 + timedelta(seconds=7))

    with uow_factory() as uow:
        with pytest.raises(StaleEpochError) as exc:
            require_leadership(uow, Fencing("A", 1), now=T0 + timedelta(seconds=8))

    assert exc.value.details["requested_epoch"] == 1
    assert exc.value.details["current_epoch"] == 2


def test_un_lease_propio_pero_vencido_es_rechazado(uow_factory) -> None:
    """Aunque nadie mas lo haya tomado todavia.

    Que el lease siga siendo mio no significa que siga siendo valido: si vencio, esta
    instancia estuvo parada mas tiempo del que cree, y no puede saber que paso mientras.
    """
    acquire_or_renew(uow_factory(), "A", TIMINGS, now=T0)

    with uow_factory() as uow:
        with pytest.raises(StaleEpochError):
            require_leadership(uow, Fencing("A", 1), now=T0 + timedelta(seconds=7))


def test_sin_lider_registrado_no_hay_liderazgo(uow_factory) -> None:
    with uow_factory() as uow:
        with pytest.raises(NotLeaderError):
            require_leadership(uow, Fencing("A", 1), now=T0)


# --- La prueba del punto de control ----------------------------------------


def _sembrar_nodo_callado(uow_factory, desde: datetime) -> str:
    """Un DataNode cuyo ultimo latido es tan viejo que la proxima evaluacion lo mataria."""
    with uow_factory() as uow:
        nodo = uow.data_nodes.register(
            advertise_url="http://localhost:8001",
            capacity_bytes=10 * 1024**3,
            now=desde,
            fault_domain="local-1",
            boot_id="boot-1",
        )
        uow.data_nodes.record_heartbeat(
            nodo.id,
            sequence=1,
            stats=NodeStats(
                used_bytes=0,
                capacity_bytes=10 * 1024**3,
                disk_free_bytes=10 * 1024**3,
                block_count=0,
            ),
            now=desde,
        )
        uow.commit()
        return nodo.id


def _estado(uow_factory, data_node_id: str) -> str:
    with uow_factory() as uow:
        return uow.data_nodes.get(data_node_id).state.value


def test_el_lider_congelado_es_rechazado_y_no_escribe_nada(uow_factory) -> None:
    """El escenario completo, que es la razon de ser de la epoca.

        t=0    A toma el lease con epoca 1
        t=0    un DataNode deja de latir
        t=+1   A se congela (pausa del recolector, particion, contenedor sin CPU)
        t=+40  el lease de A vencio hace rato; B lo toma con epoca 2
        t=+40  A DESPIERTA en medio de su operacion, convencido de que manda

    Sin epoca, en ese ultimo instante A escribe: marca el nodo muerto por segunda vez y,
    en el Bloque B, programaria la re-replicacion de los mismos bloques que B ya
    programo. Con epoca, la transaccion de A se aborta entera.

    Se comprueban las dos mitades, y la segunda es la que suele olvidarse: que fue
    rechazado, **y** que no dejo ni una escritura detras.
    """
    nodo_id = _sembrar_nodo_callado(uow_factory, T0)

    # t=0: A manda.
    lease_a = acquire_or_renew(uow_factory(), "A", TIMINGS, now=T0)
    assert lease_a is not None and lease_a.epoch == 1
    fencing_de_a = Fencing("A", lease_a.epoch)

    # A se congela aqui. Mientras tanto, el mundo sigue.
    despues = T0 + timedelta(seconds=40)

    lease_b = acquire_or_renew(uow_factory(), "B", TIMINGS, now=despues)
    assert lease_b is not None
    assert lease_b.epoch == 2, "tomar un lease vencido tiene que subir la epoca"

    assert _estado(uow_factory, nodo_id) == DataNodeState.ALIVE.value

    # A despierta y sigue por donde iba, con su epoca de hace 40 segundos.
    with pytest.raises(StaleEpochError):
        evaluate_membership(uow_factory(), UMBRALES, now=despues, fencing=fencing_de_a)

    # Y lo que de verdad hay que comprobar: la transaccion no dejo nada escrito. El nodo
    # sigue ALIVE porque quien tenia que matarlo es B, no A.
    assert _estado(uow_factory, nodo_id) == DataNodeState.ALIVE.value

    # B, con su epoca vigente, si puede: la operacion no estaba mal, lo estaba quien la
    # pedia.
    cambios = evaluate_membership(
        uow_factory(), UMBRALES, now=despues, fencing=Fencing("B", 2)
    )
    assert [c.current for c in cambios] == [DataNodeState.DEAD]
    assert _estado(uow_factory, nodo_id) == DataNodeState.DEAD.value


def test_el_lider_congelado_que_recupera_el_lease_sigue_sin_validar_lo_viejo(
    uow_factory,
) -> None:
    """Variante mas incomoda: nadie tomo el lease mientras A estuvo congelado.

    A despierta, ve el lease libre y lo recupera. La tentacion seria dejarle seguir con
    lo que estaba haciendo, porque "nunca dejo de ser el lider". Pero entre su epoca
    vieja y ahora hubo un hueco sin lider en el que cualquier otra instancia pudo haber
    entrado, y A no tiene forma de saber si paso. Su epoca vieja queda invalidada igual.
    """
    lease_a = acquire_or_renew(uow_factory(), "A", TIMINGS, now=T0)
    assert lease_a is not None
    fencing_viejo = Fencing("A", lease_a.epoch)

    despues = T0 + timedelta(seconds=40)
    recuperado = acquire_or_renew(uow_factory(), "A", TIMINGS, now=despues)

    assert recuperado is not None
    assert recuperado.epoch == 2, "recuperar el propio lease no devuelve la epoca vieja"

    with uow_factory() as uow:
        with pytest.raises(StaleEpochError):
            require_leadership(uow, fencing_viejo, now=despues)

    # Con la epoca nueva, A vuelve a trabajar con normalidad.
    with uow_factory() as uow:
        require_leadership(uow, Fencing("A", 2), now=despues)


# --- El servicio -----------------------------------------------------------


def test_el_servicio_expone_la_epoca_que_cree_tener(uow_factory) -> None:
    servicio = LeadershipService(uow_factory, TIMINGS, instance_id="A")

    assert servicio.current_fencing() is None  # todavia no ha corrido
    servicio.run_once()

    fencing = servicio.current_fencing()
    assert fencing is not None
    assert fencing == Fencing("A", 1)


def test_el_servicio_que_pierde_el_lease_deja_de_proponer_epoca(uow_factory) -> None:
    """Y esto es lo unico que la vista local puede hacer bien: dejar de afirmar.

    Que devuelva `None` no es la proteccion (el lider congelado ni siquiera llega a
    ejecutar esto). La proteccion es la comprobacion dentro de la transaccion.
    """
    servicio = LeadershipService(uow_factory, TIMINGS, instance_id="A")
    servicio.run_once()

    # Otra instancia se lleva el lease tras el vencimiento.
    acquire_or_renew(uow_factory(), "B", TIMINGS, now=_ahora_mas(7))

    servicio.run_once()
    assert servicio.current_fencing() is None
    assert not servicio.is_leader


def _ahora_mas(segundos: int) -> datetime:
    """El servicio usa el reloj real, asi que las pruebas que lo mezclan con
    adquisiciones manuales tienen que partir de ahora, no de T0."""
    from dfsha.control_node.domain.entities import utcnow

    return utcnow() + timedelta(seconds=segundos)


def test_un_fallo_de_la_base_no_mata_el_hilo_del_lease(uow_factory) -> None:
    """Misma disciplina que el evaluador de pertenencia: perder una renovacion cuesta
    que el lease pase a otra instancia, que es lo que deberia pasar; perder el hilo
    dejaria a esta instancia sin poder volver a ser lider nunca."""

    def factory_rota() -> SqlUnitOfWork:
        raise RuntimeError("la base no responde")

    servicio = LeadershipService(factory_rota, TIMINGS, instance_id="A")

    assert servicio.tick() is None  # no propaga
    assert servicio.failures == 1
    assert servicio.current_fencing() is None
