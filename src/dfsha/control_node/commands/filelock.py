"""Adquirir, renovar, soltar y EXIGIR un lock de archivo. El RF3.

Cuatro funciones y la misma regla del Bloque A, palabra por palabra: **`require_lock` no
abre transaccion propia**. Se llama con la unidad de trabajo ya abierta de quien escribe,
para que la comprobacion de epoca y la escritura ocurran bajo el mismo cerrojo de fila.
Comprobar en una transaccion y escribir en otra deja una ventana, y por esa ventana se
cuela el cliente congelado.

Si esa incomodidad —tener que pasar el `LockFencing` hasta el fondo— desaparece en un
refactor, la proteccion desaparece con ella. Ver `domain/filelock.py`.
"""

from __future__ import annotations

from datetime import datetime

from dfsha.common.errors import (
    FileLockedError,
    NotFoundError,
    StaleLockError,
)
from dfsha.common.logging import get_logger
from dfsha.control_node.domain.entities import utcnow
from dfsha.control_node.domain.filelock import (
    CONFLICT,
    GRANT,
    RENEW_EXISTING,
    FileLock,
    LockFencing,
    LockMode,
    LockTimings,
    can_acquire,
)
from dfsha.control_node.repositories.sql import SqlUnitOfWork

__all__ = [
    "acquire_lock",
    "release_lock",
    "require_lock",
    "locks_of",
]


def acquire_lock(
    uow: SqlUnitOfWork,
    file_id: str,
    *,
    holder_id: str,
    holder_name: str,
    mode: LockMode,
    timings: LockTimings,
    now: datetime | None = None,
) -> FileLock:
    """Toma el lock, o lo renueva si ya era de este titular.

    **Adquirir y renovar son la misma llamada**, y no es pereza: un cliente que reintenta
    tras un timeout de red no puede saber si el primer intento llego. Si «adquirir» fallara
    cuando ya lo tienes, ese reintento —que es correcto— seria un error.

    Llamar con la unidad de trabajo YA ABIERTA. Toma cerrojo sobre las filas de locks del
    archivo, asi que entre la lectura y la escritura no se puede colar otro peticionario.
    """
    ahora = now or utcnow()

    existentes = uow.file_locks.lock_rows(file_id)
    decision, conflicto = can_acquire(
        existentes, holder_id=holder_id, mode=mode, now=ahora
    )

    if decision == CONFLICT:
        raise FileLockedError(
            "otro cliente tiene bloqueado el archivo",
            file_id=file_id,
            holder=conflicto.holder_id if conflicto else "",
            mode=conflicto.mode.value if conflicto else "",
            retry_after_seconds=round(conflicto.remaining_seconds, 1) if conflicto else 0,
        )

    # LA EPOCA SOLO SUBE, tambien al renovar. Es lo mismo que hace el liderazgo cuando
    # recupera su propio lease vencido, y por el mismo motivo: si al renovar se
    # conservara, un cliente que estuvo congelado y vuelve dentro de su ventana no se
    # distinguiria de uno que nunca se fue. Subirla siempre hace que cada concesion sea
    # un episodio nuevo y verificable.
    epoca = uow.file_locks.max_epoch(file_id) + 1

    lock = FileLock(
        file_id=file_id,
        holder_id=holder_id,
        mode=mode,
        epoch=epoca,
        acquired_at=ahora,
        expires_at=ahora + timings.ttl,
    )
    uow.file_locks.upsert(lock, holder_name=holder_name)

    get_logger("control_node").info(
        "lock.acquired" if decision == GRANT else "lock.renewed",
        file_id=file_id,
        holder=holder_name or holder_id,
        mode=mode.value,
        epoch=epoca,
        ttl_seconds=timings.ttl.total_seconds(),
    )
    return lock


def release_lock(
    uow: SqlUnitOfWork, file_id: str, *, holder_id: str, now: datetime | None = None
) -> bool:
    """Suelta el lock. Idempotente: soltar lo que ya no se tiene tambien es exito.

    **Soltar borra la fila, y con ella la epoca.** Si todos los locks de un archivo se
    sueltan, `max_epoch` vuelve a 0 y la siguiente concesion empieza otra vez por 1. Eso
    parece romper el «la epoca solo sube» del Bloque A, y conviene explicar por que no:

    **el fencing es un PAR `(holder_id, epoch)`, no una epoca suelta.** Para que un
    cliente congelado colara una escritura no bastaria con que la epoca coincidiera:
    tendria que coincidir ademas el titular, y el titular vivo con esa epoca es otro.
    `require_lock` busca el lock **por `holder_id`**, asi que Ana con epoca 1 no valida
    contra el lock de Beto con epoca 1.

    Es la misma razon por la que `Fencing` del Bloque A tampoco es solo un numero, dicha
    alli con otras palabras: el identificador no basta y la epoca sola tampoco.
    """
    soltado = uow.file_locks.release(file_id, holder_id)
    if soltado:
        get_logger("control_node").info(
            "lock.released", file_id=file_id, holder=holder_id
        )
    return soltado


def require_lock(
    uow: SqlUnitOfWork,
    file_id: str,
    fencing: LockFencing | None,
    *,
    now: datetime | None = None,
) -> None:
    """Exige que se pueda escribir en este archivo AHORA. O lanza.

    Llamar con la unidad de trabajo **ya abierta** por la operacion de escritura. Toma
    cerrojo sobre las filas del archivo, asi que mientras la operacion no confirme, nadie
    puede adquirir el lock por debajo.

    Los casos, y ninguno es redundante:

    - **Nadie tiene lock vivo** -> se puede escribir, se traiga `fencing` o no. Un archivo
      sin bloquear se escribe como siempre; el lock es opcional, no obligatorio. Esto es
      lo que mantiene funcionando todo lo de las etapas anteriores.
    - **Hay un lock vivo y no se trae `fencing`** -> `FileLockedError`. Es el caso normal
      de «otro lo tiene abierto».
    - **Se trae `fencing` y NO coincide con el lock vivo** -> `StaleLockError`. **Este es
      el cliente congelado**: tenia el lock, se quedo sin CPU, su lease vencio, otro lo
      tomo, y ahora despierta creyendo que sigue mandando. Su epoca es vieja y la
      escritura se rechaza entera.
    - **Se trae `fencing`, coincide, pero el lock VENCIO** -> `StaleLockError` tambien. Que
      nadie lo haya tomado todavia no lo hace valido: entre el vencimiento y ahora pudo
      pasar cualquier cosa, exactamente igual que con el lease de liderazgo.
    """
    ahora = now or utcnow()
    vivos = [
        lock for lock in uow.file_locks.lock_rows(file_id) if not lock.is_expired(ahora)
    ]

    if fencing is None:
        if not vivos:
            return
        estorbo = vivos[0]
        raise FileLockedError(
            "el archivo esta bloqueado por otro cliente",
            file_id=file_id,
            holder=estorbo.holder_id,
            mode=estorbo.mode.value,
            retry_after_seconds=round(estorbo.remaining_seconds(ahora), 1),
        )

    mio = next((lock for lock in vivos if lock.holder_id == fencing.holder_id), None)
    if mio is None or mio.epoch != fencing.epoch:
        # El mismo evento que `leadership.epoch_rejected`, y con los mismos campos: es el
        # mismo suceso en otro sitio del sistema.
        get_logger("control_node").warning(
            "lock.epoch_rejected",
            file_id=file_id,
            holder=fencing.holder_id,
            epoch=fencing.epoch,
            current_epoch=mio.epoch if mio else None,
            expired=mio is None,
        )
        raise StaleLockError(
            "el bloqueo con el que se pidio la escritura ya no es el vigente",
            file_id=file_id,
            epoch=fencing.epoch,
            current_epoch=mio.epoch if mio else None,
        )

    # Un SHARED no autoriza a escribir. Sin esto, abrir en modo lectura y luego escribir
    # se saltaria la exclusion entera: varios SHARED conviven, asi que dos clientes
    # escribirian a la vez creyendo tener permiso.
    if mio.mode is not LockMode.EXCLUSIVE:
        raise FileLockedError(
            "el bloqueo es compartido y no autoriza a escribir",
            file_id=file_id,
            holder=fencing.holder_id,
            mode=mio.mode.value,
            retry_after_seconds=0,
        )


def locks_of(uow: SqlUnitOfWork, file_id: str, now: datetime | None = None) -> list[FileLock]:
    """Los locks VIVOS de un archivo, sin cerrojo. Para mostrarlos, no para decidir."""
    ahora = now or utcnow()
    return [lock for lock in uow.file_locks.peek(file_id) if not lock.is_expired(ahora)]


def file_or_fail(uow: SqlUnitOfWork, file_id: str):
    fila = uow.files.get(file_id)
    if fila is None:
        raise NotFoundError("el archivo no existe", file_id=file_id)
    return fila
