"""Enrutado del lado de lectura y read-your-writes.

Lo que se prueba aqui no es "las consultas van a la replica": es que **nunca** se le
niega a un cliente una escritura que acaba de hacer. Esa es la unica propiedad que hace
seguro replicar el lado de lectura, y la que se rompe en silencio si alguien simplifica
este modulo mas adelante.
"""

from __future__ import annotations

import pytest

from dfsha.control_node.services.read_routing import ReadRouter, lsn_to_int


class _SesionFalsa:
    """Sesion que responde a `SELECT pg_last_wal_replay_lsn()` con lo que se le diga."""

    def __init__(self, replay_lsn: str | None, falla: bool = False) -> None:
        self._lsn = replay_lsn
        self._falla = falla
        self.bind = None

    def execute(self, _sentencia):
        if self._falla:
            raise RuntimeError("la replica no responde")
        return _Resultado(self._lsn)


class _Resultado:
    def __init__(self, valor):
        self._valor = valor

    def scalar(self):
        return self._valor


class _UowFalsa:
    def __init__(self, etiqueta: str, replay_lsn: str | None = None, falla: bool = False):
        self.etiqueta = etiqueta
        self.session = _SesionFalsa(replay_lsn, falla)
        self.cerrada = False

    def __exit__(self, *_):
        self.cerrada = True


def _router(replay_lsn: str | None = None, falla: bool = False, con_replica: bool = True):
    primaria = _UowFalsa("primario")
    replica = _UowFalsa("replica", replay_lsn, falla)
    return (
        ReadRouter(lambda: primaria, (lambda: replica) if con_replica else None),
        primaria,
        replica,
    )


# --- Conversion del LSN ----------------------------------------------------


@pytest.mark.parametrize(
    "texto, esperado",
    [
        ("0/0", 0),
        ("0/10", 16),
        ("1/0", 4294967296),
        ("16/B374D848", (0x16 << 32) + 0xB374D848),
    ],
)
def test_lsn_se_convierte_a_entero_comparable(texto: str, esperado: int) -> None:
    assert lsn_to_int(texto) == esperado


@pytest.mark.parametrize("basura", [None, "", "no-es-un-lsn", "16", "zz/11", "16/"])
def test_un_lsn_invalido_no_revienta(basura) -> None:
    """El LSN llega por una cabecera HTTP, o sea entrada no confiable. Tiene que
    degradar a `None`, que manda la consulta al primario."""
    assert lsn_to_int(basura) is None


# --- Decision de enrutado --------------------------------------------------


def test_sin_replica_todo_va_al_primario() -> None:
    """DFSHA_DB_REPLICA_URL vacia es un modo soportado, no una degradacion."""
    router, primaria, _ = _router(con_replica=False)
    assert not router.has_replica

    decision = router.for_read("16/B374D848")
    assert decision.uow is primaria
    assert decision.target == "primary"


def test_un_cliente_que_no_ha_escrito_va_a_la_replica() -> None:
    """Sin LSN no hay ninguna escritura propia que la replica pueda no tener, asi que ni
    siquiera se le pregunta cuanto lleva reproducido."""
    router, _, replica = _router(replay_lsn="0/10")

    decision = router.for_read(None)
    assert decision.uow is replica
    assert decision.target == "replica"


def test_replica_al_dia_atiende_la_consulta() -> None:
    router, _, replica = _router(replay_lsn="0/200")

    decision = router.for_read("0/100")
    assert decision.uow is replica
    assert decision.target == "replica"


def test_replica_exactamente_en_el_lsn_del_cliente_sirve() -> None:
    """Igualdad cuenta como al dia: el LSN del cliente ya esta reproducido."""
    router, _, replica = _router(replay_lsn="0/100")
    assert router.for_read("0/100").uow is replica


def test_replica_retrasada_manda_la_consulta_al_primario() -> None:
    """El caso que da sentido a todo el modulo: el cliente escribio, la replica aun no lo
    tiene, y servirle desde ahi seria negarle su propia escritura."""
    router, primaria, replica = _router(replay_lsn="0/50")

    decision = router.for_read("0/100")

    assert decision.uow is primaria
    assert decision.target == "primary"
    # Y la unidad de trabajo de la replica se cierra: abrirla y abandonarla dejaria una
    # conexion del pool retenida en cada consulta que llegue con retraso.
    assert replica.cerrada


def test_replica_inalcanzable_cae_al_primario() -> None:
    """Preferible equivocarse de lado que servir datos de los que no se sabe nada."""
    router, primaria, _ = _router(falla=True)

    decision = router.for_read("0/100")
    assert decision.uow is primaria


def test_un_primario_disfrazado_de_replica_cae_al_primario() -> None:
    """`pg_last_wal_replay_lsn()` devuelve NULL fuera de recuperacion, que es lo que pasa
    si DFSHA_DB_REPLICA_URL apunta por error a un primario."""
    router, primaria, _ = _router(replay_lsn=None)

    assert router.for_read("0/100").uow is primaria
