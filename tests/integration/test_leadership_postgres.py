"""Exclusion mutua del lease contra PostgreSQL de verdad.

**Esta es la mitad del mecanismo que SQLite no puede probar.** Las pruebas unitarias de
`test_leadership.py` cubren la comparacion de epoca, que es lo que atrapa al lider
congelado; lo que no pueden cubrir es que dos instancias compitiendo a la vez no ganen
las dos, porque eso depende de `SELECT ... FOR UPDATE`, que SQLite no implementa y
SQLAlchemy omite en ese dialecto.

Se salta si no hay PostgreSQL a mano, para que `pytest` siga corriendo en un portatil sin
Docker. En el CI y en el compose si corre:

    DFSHA_TEST_PG_URL="postgresql+psycopg://dfsha:...@localhost:5432/dfsha" pytest

Ojo: usa su propio esquema temporal, no las tablas del despliegue, para no pisar datos de
nadie al correrlo contra una base que este en uso.
"""

from __future__ import annotations

import collections
import os
import threading

import pytest

from dfsha.control_node.commands.leadership import (
    acquire_or_renew,
    read_lease,
    require_leadership,
)
from dfsha.control_node.domain.leadership import Fencing, LeaseTimings
from dfsha.common.errors import StaleEpochError
from dfsha.control_node.repositories.database import build_engine, build_session_factory
from dfsha.control_node.repositories.models import LEADERSHIP_ROW_ID, LeadershipRow
from dfsha.control_node.repositories.sql import SqlUnitOfWork

PG_URL = os.environ.get("DFSHA_TEST_PG_URL", "")

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="sin DFSHA_TEST_PG_URL: la exclusion mutua solo se puede probar con PostgreSQL",
)

TIMINGS = LeaseTimings.from_millis(ttl_ms=6000, renew_ms=2000)
INSTANCIAS = 8


@pytest.fixture()
def uow_factory():
    """Un esquema propio y desechable, para no tocar las tablas de nadie."""
    from sqlalchemy import text

    engine = build_engine(PG_URL)
    with engine.begin() as conexion:
        conexion.execute(text("DROP SCHEMA IF EXISTS dfsha_test_lease CASCADE"))
        conexion.execute(text("CREATE SCHEMA dfsha_test_lease"))

    engine.dispose()
    engine = build_engine(PG_URL)
    engine.update_execution_options(
        schema_translate_map={None: "dfsha_test_lease"}
    )
    LeadershipRow.__table__.create(engine)

    factory = build_session_factory(engine)
    with factory() as sesion:
        sesion.add(LeadershipRow(id=LEADERSHIP_ROW_ID, leader_id=None, epoch=0))
        sesion.commit()

    try:
        yield lambda: SqlUnitOfWork(factory)
    finally:
        with engine.begin() as conexion:
            conexion.execute(text("DROP SCHEMA IF EXISTS dfsha_test_lease CASCADE"))
        engine.dispose()


def _competir(uow_factory, instancias: int) -> list[tuple[str, int | None]]:
    """`instancias` hilos intentando tomar el lease **en el mismo instante**.

    La barrera importa: sin ella los hilos se turnan y la prueba pasa aunque el cerrojo
    no exista, que es justo el falso verde que hay que evitar.
    """
    barrera = threading.Barrier(instancias)
    resultados: list[tuple[str, int | None]] = []
    candado = threading.Lock()

    def intentar(nombre: str) -> None:
        barrera.wait()
        lease = acquire_or_renew(uow_factory(), nombre, TIMINGS)
        with candado:
            resultados.append((nombre, lease.epoch if lease else None))

    hilos = [
        threading.Thread(target=intentar, args=(f"inst-{i}",)) for i in range(instancias)
    ]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(timeout=30)

    return resultados


def test_ocho_instancias_a_la_vez_y_solo_gana_una(uow_factory) -> None:
    resultados = _competir(uow_factory, INSTANCIAS)
    ganadores = [(n, e) for n, e in resultados if e is not None]

    assert len(resultados) == INSTANCIAS, "algun hilo no termino"
    assert len(ganadores) == 1, f"mas de un lider a la vez: {ganadores}"

    final = read_lease(uow_factory())
    assert final.leader_id == ganadores[0][0]
    assert final.epoch == 1


def test_ninguna_epoca_se_repite_entre_competidores(uow_factory) -> None:
    """Dos instancias con la misma epoca serian dos tokens de aislamiento identicos, o
    sea ninguno."""
    epocas = [e for _, e in _competir(uow_factory, INSTANCIAS) if e is not None]
    repetidas = [e for e, veces in collections.Counter(epocas).items() if veces > 1]

    assert repetidas == []


def test_con_el_lease_vivo_solo_el_lider_renueva(uow_factory) -> None:
    primera = _competir(uow_factory, INSTANCIAS)
    lider = next(n for n, e in primera if e is not None)

    segunda = _competir(uow_factory, INSTANCIAS)
    ganadores = [(n, e) for n, e in segunda if e is not None]

    assert len(ganadores) == 1
    assert ganadores[0][0] == lider, "otra instancia quito un lease que seguia vivo"
    assert ganadores[0][1] == 1, "una renovacion no puede cambiar la epoca"


def test_la_epoca_vieja_es_rechazada_tambien_en_postgres(uow_factory) -> None:
    """La comprobacion de epoca ya se prueba en SQLite; esto verifica que el dialecto
    no cambia el comportamiento, que es lo unico que podria diferir."""
    primera = read_lease(uow_factory())
    assert primera.leader_id is None

    lease = acquire_or_renew(uow_factory(), "A", TIMINGS)
    assert lease is not None

    with uow_factory() as uow:
        require_leadership(uow, Fencing("A", lease.epoch))  # vigente: pasa

    with uow_factory() as uow:
        with pytest.raises(StaleEpochError):
            require_leadership(uow, Fencing("A", lease.epoch - 1))
