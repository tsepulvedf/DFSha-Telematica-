"""Adquisicion, renovacion y comprobacion del lease de liderazgo.

Dos funciones y una regla. Las funciones son `acquire_or_renew`, que llama el bucle del
servicio, y `require_leadership`, que llaman las operaciones que exigen ser lider.

La regla, que es la que importa: **`require_leadership` no abre transaccion propia**.
Se llama con la unidad de trabajo ya abierta de la operacion, para que la comprobacion de
epoca y la escritura ocurran en la misma transaccion y bajo el mismo cerrojo de fila.
Comprobar en una transaccion y escribir en otra deja una ventana entre ambas; esa ventana
es justo por donde se cuela el lider congelado, y entonces todo este mecanismo no sirve
para nada.
"""

from __future__ import annotations

import time
from datetime import datetime

from dfsha.common.errors import NotLeaderError, StaleEpochError
from dfsha.common.logging import get_logger
from dfsha.control_node.domain.entities import utcnow
from dfsha.control_node.domain.leadership import (
    ACQUIRE,
    RENEW,
    Fencing,
    Lease,
    LeaseTimings,
    decide,
)
from dfsha.control_node.repositories.sql import SqlUnitOfWork

__all__ = ["acquire_or_renew", "require_leadership", "release_leadership", "read_lease"]


def acquire_or_renew(
    uow: SqlUnitOfWork,
    instance_id: str,
    timings: LeaseTimings,
    now: datetime | None = None,
) -> Lease | None:
    """Intenta sostener el lease. Devuelve el lease si esta instancia manda, o `None`.

    `None` no es un error: con tres ControlNodes, dos de ellos devuelven `None` todo el
    rato. Es el estado normal de la mayoria.
    """
    ahora = now or utcnow()
    log = get_logger("control_node")
    inicio = time.perf_counter()

    with uow:
        lease = uow.leadership.lock()
        if lease is None:
            # En PostgreSQL la fila la siembra la migracion 0002. Esto cubre el esquema
            # de SQLite de las pruebas, que se crea con create_all.
            lease = uow.leadership.seed()

        accion = decide(lease, instance_id, ahora)

        if accion == RENEW:
            renovado = uow.leadership.renew(
                instance_id, lease.epoch, ahora, ahora + timings.ttl
            )
            uow.commit()
            # A nivel debug a proposito: con el default de 2 s y tres instancias son
            # 1,5 lineas por segundo para siempre, y ahogarian los eventos que si
            # cuentan algo (node.state_changed, rereplication.scheduled). La adquisicion
            # y la perdida, que son los sucesos de verdad, si van a INFO.
            log.debug(
                "leadership.renewed",
                leader_id=instance_id,
                epoch=renovado.epoch,
                duration_ms=_ms(inicio),
                expires_in_s=round(renovado.remaining_seconds(ahora), 3),
            )
            return renovado

        if accion == ACQUIRE:
            anterior = lease.leader_id
            epoca = lease.epoch + 1
            tomado = uow.leadership.acquire(
                instance_id, epoca, ahora, ahora + timings.ttl
            )
            uow.commit()
            log.info(
                "leadership.acquired",
                leader_id=instance_id,
                epoch=epoca,
                previous_leader=anterior,
                # Un lider que recupera su propio lease vencido tambien sube de epoca:
                # entre la epoca vieja y esta pudo pasar cualquier cosa.
                self_recovery=anterior == instance_id,
                duration_ms=_ms(inicio),
            )
            return tomado

        return None


def require_leadership(
    uow: SqlUnitOfWork, fencing: Fencing, now: datetime | None = None
) -> Lease:
    """Exige que esta instancia siga siendo lider CON ESTA EPOCA.

    Llamar con la unidad de trabajo **ya abierta** por la operacion. Toma el cerrojo de
    la fila del lease, asi que mientras la operacion no confirme, nadie mas puede
    adquirirlo.

    Lanza `StaleEpochError` en los tres casos en que la instancia no debe escribir:
    el lease es de otro, la epoca ya no es la suya, o su propio lease vencio (lo que
    significa que estuvo parada mas tiempo del que creia).
    """
    ahora = now or utcnow()
    inicio = time.perf_counter()
    lease = uow.leadership.lock()

    if lease is None or lease.leader_id is None:
        raise NotLeaderError(
            "no hay ningun lider registrado", required_epoch=fencing.epoch
        )

    desajuste = (
        lease.leader_id != fencing.leader_id
        or lease.epoch != fencing.epoch
        or lease.is_expired(ahora)
    )
    if desajuste:
        get_logger("control_node").warning(
            "leadership.epoch_rejected",
            leader_id=fencing.leader_id,
            epoch=fencing.epoch,
            current_leader=lease.leader_id,
            current_epoch=lease.epoch,
            expired=lease.is_expired(ahora),
            duration_ms=_ms(inicio),
            detail=(
                "una operacion que exige liderazgo se pidio con una epoca que ya no es "
                "la vigente; se aborta entera"
            ),
        )
        raise StaleEpochError(
            "la epoca del lider ya no es la vigente; la operacion se aborta",
            requested_epoch=fencing.epoch,
            current_epoch=lease.epoch,
            requested_leader=fencing.leader_id,
            current_leader=lease.leader_id,
        )

    return lease


def _ms(inicio: float) -> float:
    return round((time.perf_counter() - inicio) * 1000, 3)


def release_leadership(uow: SqlUnitOfWork, fencing: Fencing) -> None:
    """Suelta el lease en un apagado ordenado, para no dejar al cluster sin lider
    durante un TTL entero por nada."""
    with uow:
        uow.leadership.release(fencing.leader_id, fencing.epoch)
        uow.commit()


def read_lease(uow: SqlUnitOfWork, now: datetime | None = None) -> Lease:
    """Lectura del lease para exponerlo por la API. No toma cerrojo: es informativa."""
    with uow:
        lease = uow.leadership.peek()
        if lease is None:
            return Lease(
                leader_id=None,
                epoch=0,
                acquired_at=None,
                renewed_at=None,
                expires_at=None,
            )
        return lease
