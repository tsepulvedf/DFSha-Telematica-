"""Bloqueo de archivos por lease. El RF3, y la tercera vez que aparece la misma idea.

## Esto ya lo hemos resuelto dos veces

El escenario que obliga a la epoca en el Bloque A, palabra por palabra pero cambiando
«lider» por «cliente»:

    t=0   Ana toma el lock de /informe.txt (epoca 3) y empieza a escribir
    t=1   Ana se congela: su portatil suspende, se le va la red, el proceso se para
    t=7   el lease vence sin que Ana se entere
    t=8   Beto lo toma con epoca 4 y escribe su version
    t=9   Ana despierta EN MEDIO de su escritura, convencida de que sigue teniendo el lock

Si lo unico que Ana comprobo fue «tengo el lock» antes de empezar, en t=9 escribe encima
de lo de Beto. **El lock no sirvio para nada**, y ademas de forma silenciosa: los dos
creen haber escrito con exclusion.

Es literalmente el mismo problema que el del ControlNode congelado, y tiene literalmente
la misma solucion: **la epoca viaja con la operacion y se verifica dentro de la misma
transaccion que la escritura**. En t=9 la fila dice 4, Ana trae 3, y el `append` se
rechaza entero.

Y por tercera vez la misma regla de diseno: **no existe ningun `tengo_el_lock()`**
consultable por separado. `require_lock(uow, token, now)` se llama DENTRO del `with uow:`
de quien escribe. Una funcion que responde «¿lo tengo?» se puede ignorar; una que hace
«falla si no lo tienes» no.

| Donde | Que viaja | Quien lo verifica |
|---|---|---|
| Bloque A | `Fencing(leader_id, epoch)` | `require_leadership`, dentro del `uow` |
| Bloque C, ACLs | el permiso **minimo** | `directory_for`, al resolver |
| Bloque C, token de bloque | `block_id` + operacion, firmados | `verify_token`, en el DataNode |
| **Bloque C, RF3** | `LockFencing(holder_id, epoch)` | `require_lock`, dentro del `uow` |

## Por que hay lease y no un lock que dure hasta que se suelte

Decision 7 de la seccion 1: todo estado transitorio vence por tiempo. Un lock sin
vencimiento lo sostiene para siempre el primer cliente que se caiga sin soltarlo, y la
unica salida seria que un administrador lo quitara a mano. Con lease, un cliente que
desaparece libera el archivo solo.

El precio, y hay que decirlo: **un cliente lento puede perder el lock sin haberse caido**.
Por eso se renueva —igual que el lease de liderazgo— y por eso el TTL por defecto es
holgado. Quien tarda mas que el TTL entre renovaciones esta, a efectos del sistema,
indistinguible de uno que murio; esa indistinguibilidad no es un defecto de la
implementacion, es lo que un sistema distribuido puede saber.

## Compartido y exclusivo

Dos modos, con la semantica clasica:

- **SHARED**: varios a la vez. Es el que pide `open` en modo lectura.
- **EXCLUSIVE**: uno solo, y ademas incompatible con cualquier compartido vivo.

No hay ascenso de compartido a exclusivo, y es deliberado: dos clientes con lock
compartido pidiendo ascender a la vez se quedan esperando el uno al otro para siempre.
Detectarlo obliga a un grafo de espera y a elegir victima, que es un proyecto aparte.
Quien quiera escribir pide EXCLUSIVE desde el principio.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

__all__ = [
    "LockMode",
    "FileLock",
    "LockFencing",
    "LockTimings",
    "LockConflict",
    "can_acquire",
    "GRANT",
    "RENEW_EXISTING",
    "CONFLICT",
]


class LockMode(str, Enum):
    SHARED = "shared"
    EXCLUSIVE = "exclusive"


@dataclass(frozen=True, slots=True)
class LockFencing:
    """Lo que una escritura sobre un archivo bloqueado tiene que llevar encima.

    Igual que `Fencing` del Bloque A, es un par y por el mismo motivo: el identificador
    solo no basta —el mismo cliente puede haber perdido y recuperado el lock— y la epoca
    sola tampoco, porque dice *cuando* pero no *quien*.
    """

    holder_id: str
    epoch: int


@dataclass(frozen=True, slots=True)
class FileLock:
    file_id: str
    holder_id: str
    mode: LockMode
    epoch: int
    acquired_at: datetime
    expires_at: datetime

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at

    def held_by(self, holder_id: str, now: datetime) -> bool:
        return self.holder_id == holder_id and not self.is_expired(now)

    def remaining_seconds(self, now: datetime) -> float:
        return max(0.0, (self.expires_at - now).total_seconds())

    @property
    def fencing(self) -> LockFencing:
        return LockFencing(holder_id=self.holder_id, epoch=self.epoch)


@dataclass(frozen=True, slots=True)
class LockTimings:
    ttl: timedelta

    @classmethod
    def from_millis(cls, ttl_ms: int) -> "LockTimings":
        if ttl_ms <= 0:
            raise ValueError("el TTL de un lock tiene que ser positivo")
        return cls(ttl=timedelta(milliseconds=ttl_ms))


@dataclass(frozen=True, slots=True)
class LockConflict:
    """Quien impide tomar el lock, para poder decirlo en el error.

    Se devuelve el motivo y no solo un `False` porque «el archivo esta bloqueado» sin
    decir por quien ni hasta cuando es un mensaje con el que no se puede hacer nada. Con
    esto el cliente puede decidir si esperar: `remaining_seconds` es exactamente lo que
    tardara en quedar libre si el otro no renueva.
    """

    holder_id: str
    mode: LockMode
    remaining_seconds: float


#: Que hacer con lo que hay ahora mismo.
GRANT = "grant"
RENEW_EXISTING = "renew_existing"
CONFLICT = "conflict"


def can_acquire(
    existing: list[FileLock],
    *,
    holder_id: str,
    mode: LockMode,
    now: datetime,
) -> tuple[str, LockConflict | None]:
    """Funcion pura: dados los locks vivos de un archivo, que le toca a este peticionario.

    **Los vencidos no cuentan y no hace falta borrarlos antes.** Es la comprobacion
    perezosa de la seccion 1: el vencimiento se evalua cuando alguien pregunta, nunca con
    un barrido en background. Un lock vencido que sigue en la tabla es exactamente igual
    de inofensivo que uno borrado.

    Los cuatro casos, en este orden:

    1. **Ya es mio y sigue vivo** -> renovar. Pedir dos veces el mismo lock no es un
       error: un cliente que reintenta tras un timeout de red no puede saber si el
       primero llego.
    2. **Nadie vivo** -> conceder.
    3. **Quiero EXCLUSIVE y hay alguien** -> conflicto, sea cual sea su modo.
    4. **Quiero SHARED y hay un EXCLUSIVE** -> conflicto. Varios SHARED conviven.
    """
    vivos = [lock for lock in existing if not lock.is_expired(now)]

    mio = next((lock for lock in vivos if lock.holder_id == holder_id), None)
    if mio is not None:
        # Renovar no cambia de modo. Cambiarlo seria el ascenso que este modulo no
        # implementa a proposito; ver el docstring.
        if mio.mode is mode:
            return RENEW_EXISTING, None
        return CONFLICT, LockConflict(
            holder_id=mio.holder_id,
            mode=mio.mode,
            remaining_seconds=mio.remaining_seconds(now),
        )

    ajenos = [lock for lock in vivos if lock.holder_id != holder_id]
    if not ajenos:
        return GRANT, None

    if mode is LockMode.EXCLUSIVE:
        estorbo = ajenos[0]
    else:
        exclusivo = next(
            (lock for lock in ajenos if lock.mode is LockMode.EXCLUSIVE), None
        )
        if exclusivo is None:
            return GRANT, None
        estorbo = exclusivo

    return CONFLICT, LockConflict(
        holder_id=estorbo.holder_id,
        mode=estorbo.mode,
        remaining_seconds=estorbo.remaining_seconds(now),
    )
