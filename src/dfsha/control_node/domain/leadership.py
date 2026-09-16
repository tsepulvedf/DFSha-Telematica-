"""Liderazgo por lease con epoca.

Varios ControlNodes sin estado contra la misma base. Todos sirven lecturas, planes de
escritura y autenticacion; **uno solo** sostiene el lease y es el unico que puede evaluar
la pertenencia de los DataNodes, programar re-replicaciones y recolectar.

## La epoca es un token de aislamiento, y es lo importante de este modulo

Un lease por si solo no basta. Considera este orden de sucesos, que no es hipotetico:

    t=0   A toma el lease (epoca 7) y empieza a evaluar la pertenencia
    t=1   A se congela: una pausa larga del recolector de basura, una particion, un
          contenedor que el planificador dejo sin CPU
    t=7   el lease de A vence sin que A se entere
    t=8   B lo toma con epoca 8 y empieza a trabajar
    t=9   A despierta EN MEDIO de su operacion, convencido de que sigue siendo lider

Si lo unico que A comprobo fue "soy el lider" antes de empezar, en t=9 escribe. Y
entonces hay dos lideres marcando nodos muertos y programando la misma re-replicacion
dos veces.

Lo que lo corta es que A lleve su epoca **encima** y se compruebe contra la almacenada
**dentro de la misma transaccion que la escritura**. En t=9 la fila dice 8, A trae 7, y
la operacion se aborta entera. Por eso aqui no hay ninguna funcion `soy_el_lider()`
consultable por separado: ese es exactamente el patron que deja pasar a A.

## Recuperar el lease no devuelve la epoca vieja

Si A vuelve y el lease esta libre, lo toma con epoca 9, no con la 7. Sus operaciones a
medio camino siguen rechazadas, que es lo correcto: entre la 7 y la 9 pudo pasar
cualquier cosa. La epoca solo sube.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

__all__ = ["Lease", "Fencing", "LeaseTimings", "decide"]


@dataclass(frozen=True, slots=True)
class Fencing:
    """Lo que una operacion que exige liderazgo tiene que llevar encima.

    Es deliberadamente un par: el identificador no basta (una instancia que se reinicia
    conserva el suyo si se configura fijo) y la epoca sola tampoco (dice *cuando*, no
    *quien*).
    """

    leader_id: str
    epoch: int


@dataclass(frozen=True, slots=True)
class Lease:
    leader_id: str | None
    epoch: int
    acquired_at: datetime | None
    renewed_at: datetime | None
    expires_at: datetime | None

    def is_expired(self, now: datetime) -> bool:
        """Sin fecha de vencimiento no hay lease: la fila inicial esta libre."""
        if self.expires_at is None:
            return True
        return now >= self.expires_at

    def held_by(self, instance_id: str, now: datetime) -> bool:
        return self.leader_id == instance_id and not self.is_expired(now)

    def remaining_seconds(self, now: datetime) -> float:
        if self.expires_at is None:
            return 0.0
        return max(0.0, (self.expires_at - now).total_seconds())

    @property
    def fencing(self) -> Fencing | None:
        if self.leader_id is None:
            return None
        return Fencing(leader_id=self.leader_id, epoch=self.epoch)


@dataclass(frozen=True, slots=True)
class LeaseTimings:
    ttl: timedelta
    renew_every: timedelta

    @classmethod
    def from_millis(cls, ttl_ms: int, renew_ms: int) -> "LeaseTimings":
        if renew_ms >= ttl_ms:
            raise ValueError(
                "DFSHA_LEASE_RENEW_MS debe ser menor que DFSHA_LEASE_TTL_MS; si no, el "
                "lider pierde el lease antes de tener ocasion de renovarlo y el "
                "liderazgo cambia de manos sin que haya fallado nada"
            )
        return cls(
            ttl=timedelta(milliseconds=ttl_ms),
            renew_every=timedelta(milliseconds=renew_ms),
        )


#: Que hacer con el lease que hay ahora mismo.
RENEW = "renew"
ACQUIRE = "acquire"
STAND_BY = "stand_by"


def decide(lease: Lease, instance_id: str, now: datetime) -> str:
    """Funcion pura: dado el lease almacenado, que le toca hacer a esta instancia.

    Los tres casos, y por que el orden importa:

    1. **Es mio y sigue vivo** -> renovar, conservando la epoca. Es el caso normal.
    2. **Esta vencido (de quien sea, incluido yo)** -> tomarlo subiendo la epoca. Que un
       lease vencido MIO tambien suba la epoca es lo que impide que un lider congelado
       resucite con su epoca antigua y valide operaciones que empezo antes de la pausa.
    3. **Es de otro y sigue vivo** -> esperar. No es un fallo: con tres instancias, dos
       estan siempre aqui.
    """
    if lease.held_by(instance_id, now):
        return RENEW
    if lease.is_expired(now):
        return ACQUIRE
    return STAND_BY
