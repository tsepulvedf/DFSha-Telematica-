"""Bloqueo de archivos: la decision pura, sin base de datos.

El caso que da sentido a todo el fichero es `test_el_cliente_congelado_...`: es el mismo
escenario del lider congelado del Bloque A, con un cliente en vez de un ControlNode. Que
la respuesta sea tambien la misma —la epoca viaja con la operacion— es lo que hace que el
diseno sea coherente y no una coleccion de soluciones sueltas.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from dfsha.control_node.domain.filelock import (
    CONFLICT,
    GRANT,
    RENEW_EXISTING,
    FileLock,
    LockMode,
    LockTimings,
    can_acquire,
)

T0 = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
ARCHIVO = "f-1"


def lock(
    holder: str,
    mode: LockMode = LockMode.EXCLUSIVE,
    *,
    epoch: int = 1,
    vence_en: int = 60,
) -> FileLock:
    return FileLock(
        file_id=ARCHIVO,
        holder_id=holder,
        mode=mode,
        epoch=epoch,
        acquired_at=T0,
        expires_at=T0 + timedelta(seconds=vence_en),
    )


# --- Conceder ---------------------------------------------------------------


def test_sobre_un_archivo_libre_se_concede() -> None:
    decision, conflicto = can_acquire(
        [], holder_id="ana", mode=LockMode.EXCLUSIVE, now=T0
    )

    assert decision == GRANT
    assert conflicto is None


def test_varios_COMPARTIDOS_conviven() -> None:
    """Para lo que existe el modo compartido: muchos lectores, ningun escritor."""
    existentes = [lock("ana", LockMode.SHARED), lock("beto", LockMode.SHARED)]

    decision, _ = can_acquire(
        existentes, holder_id="carla", mode=LockMode.SHARED, now=T0
    )

    assert decision == GRANT


def test_pedir_el_mismo_lock_otra_vez_es_RENOVAR_y_no_un_error() -> None:
    """Un cliente que reintenta tras un timeout de red no puede saber si el primer
    intento llego. Si adquirir fallara cuando ya lo tienes, ese reintento correcto seria
    un error."""
    decision, _ = can_acquire(
        [lock("ana")], holder_id="ana", mode=LockMode.EXCLUSIVE, now=T0
    )

    assert decision == RENEW_EXISTING


# --- Excluir ----------------------------------------------------------------


def test_un_EXCLUSIVO_ajeno_impide_cualquier_cosa() -> None:
    for modo in (LockMode.SHARED, LockMode.EXCLUSIVE):
        decision, conflicto = can_acquire(
            [lock("ana", LockMode.EXCLUSIVE)], holder_id="beto", mode=modo, now=T0
        )
        assert decision == CONFLICT, modo
        assert conflicto.holder_id == "ana"


def test_un_COMPARTIDO_ajeno_impide_el_EXCLUSIVO() -> None:
    decision, conflicto = can_acquire(
        [lock("ana", LockMode.SHARED)], holder_id="beto", mode=LockMode.EXCLUSIVE, now=T0
    )

    assert decision == CONFLICT
    assert conflicto.mode is LockMode.SHARED


def test_el_conflicto_dice_QUIEN_y_CUANTO_falta() -> None:
    """«El archivo esta bloqueado» sin decir por quien ni hasta cuando es un mensaje con
    el que no se puede hacer nada. Con esto el cliente puede decidir si esperar."""
    existentes = [lock("ana", vence_en=60)]

    _, conflicto = can_acquire(
        existentes, holder_id="beto", mode=LockMode.EXCLUSIVE, now=T0 + timedelta(seconds=20)
    )

    assert conflicto.holder_id == "ana"
    assert conflicto.remaining_seconds == pytest.approx(40.0)


def test_no_se_asciende_de_COMPARTIDO_a_EXCLUSIVO() -> None:
    """Deliberado, y el motivo esta en el modulo: dos clientes con compartido pidiendo
    ascender a la vez se esperarian el uno al otro para siempre. Detectarlo obliga a un
    grafo de espera y a elegir victima, que es otro proyecto."""
    decision, _ = can_acquire(
        [lock("ana", LockMode.SHARED)], holder_id="ana", mode=LockMode.EXCLUSIVE, now=T0
    )

    assert decision == CONFLICT


# --- El vencimiento, que es lo que hace util al lease -----------------------


def test_un_lock_VENCIDO_no_estorba() -> None:
    """Sin esto, el primer cliente que se caiga sin soltar deja el archivo bloqueado para
    siempre y hace falta un administrador."""
    existentes = [lock("ana", vence_en=60)]

    decision, _ = can_acquire(
        existentes,
        holder_id="beto",
        mode=LockMode.EXCLUSIVE,
        now=T0 + timedelta(seconds=61),
    )

    assert decision == GRANT


def test_los_vencidos_NO_hace_falta_borrarlos_antes() -> None:
    """Comprobacion perezosa, la regla transversal de la seccion 1: el vencimiento se
    evalua cuando alguien pregunta, nunca con un barrido en background. Un lock vencido
    que sigue en la tabla es igual de inofensivo que uno borrado."""
    muchos_muertos = [lock(f"cliente-{i}", vence_en=1) for i in range(50)]

    decision, _ = can_acquire(
        muchos_muertos, holder_id="ana", mode=LockMode.EXCLUSIVE, now=T0 + timedelta(hours=1)
    )

    assert decision == GRANT


def test_justo_en_el_instante_de_vencer_ya_no_vale() -> None:
    """El limite es cerrado por abajo: `now >= expires_at` vence. Lo contrario dejaria un
    instante en el que dos clientes pueden creer que lo tienen."""
    existentes = [lock("ana", vence_en=60)]

    decision, _ = can_acquire(
        existentes, holder_id="beto", mode=LockMode.EXCLUSIVE, now=T0 + timedelta(seconds=60)
    )

    assert decision == GRANT


# --- El fencing: por que la epoca es un PAR --------------------------------


def test_el_fencing_lleva_titular_Y_epoca() -> None:
    """El identificador solo no basta —el mismo cliente puede haber perdido y recuperado
    el lock— y la epoca sola tampoco, porque dice *cuando* pero no *quien*."""
    l = lock("ana", epoch=7)

    assert l.fencing.holder_id == "ana"
    assert l.fencing.epoch == 7


def test_held_by_exige_las_dos_cosas() -> None:
    l = lock("ana", vence_en=60)

    assert l.held_by("ana", T0)
    assert not l.held_by("beto", T0)
    assert not l.held_by("ana", T0 + timedelta(seconds=61))


# --- Configuracion ----------------------------------------------------------


def test_un_TTL_no_positivo_se_rechaza_al_configurar() -> None:
    """Mismo criterio que `LeaseTimings`: fallar al arrancar y no al primer lock."""
    with pytest.raises(ValueError):
        LockTimings.from_millis(0)


def test_el_TTL_se_configura_en_milisegundos() -> None:
    assert LockTimings.from_millis(30_000).ttl == timedelta(seconds=30)
