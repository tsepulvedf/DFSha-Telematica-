"""Enrutado del lado de lectura de CQRS.

La separacion comandos/consultas existe desde la Etapa 1 en paquetes distintos
(`commands/` y `queries/`) pero hasta ahora las dos golpeaban la misma base. Aqui es
donde esa separacion se cobra: las consultas pueden ir a una replica de PostgreSQL.

El problema de hacerlo, y toda la razon de ser de este modulo, es que la replicacion es
**asincrona**. Un cliente que hace `mkdir /a` y acto seguido `ls /` puede preguntarle a
una replica que todavia no ha reproducido su `mkdir`, y ver que su directorio no existe.
Eso no es "un poco de retraso": es que el sistema le miente al cliente sobre su propia
escritura.

La solucion es *read-your-writes* con el LSN de PostgreSQL, que es el numero de secuencia
del WAL:

1. Tras cada comando, el ControlNode devuelve el LSN del primario en `X-DFSha-Write-LSN`.
2. El cliente lo guarda y lo reenvia en sus consultas como `X-DFSha-Read-LSN`.
3. Antes de servir una consulta con ese encabezado, se mira hasta donde ha reproducido la
   replica. Si va por detras, la consulta se atiende desde el primario.

El coste cae **solo sobre los clientes que acaban de escribir**: quien no manda LSN va
derecho a la replica sin consulta adicional.

Sin replica configurada (`DFSHA_DB_REPLICA_URL` vacia) todo esto queda inerte y las
consultas van al primario, igual que en las etapas anteriores.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from sqlalchemy import text

from dfsha.common.logging import get_logger
from dfsha.control_node.repositories.sql import SqlUnitOfWork

__all__ = [
    "READ_LSN_HEADER",
    "WRITE_LSN_HEADER",
    "lsn_to_int",
    "current_write_lsn",
    "ReadRouter",
    "RouteDecision",
]

#: El cliente manda en esta cabecera el LSN de su ultima escritura.
READ_LSN_HEADER = "X-DFSha-Read-LSN"
#: El ControlNode devuelve en esta el LSN del primario tras un comando.
WRITE_LSN_HEADER = "X-DFSha-Write-LSN"


def lsn_to_int(lsn: str | None) -> int | None:
    """Convierte un LSN de PostgreSQL ('16/B374D848') al entero que representa.

    Se compara en Python y no con `::pg_lsn` en SQL para no tener que meter el valor que
    manda el cliente dentro de una consulta: un LSN llega por una cabecera HTTP, o sea
    que es entrada no confiable, y aqui una cadena mal formada se convierte en `None` y
    la consulta acaba en el primario, que es el lado seguro.
    """
    if not lsn:
        return None
    alto, _, bajo = lsn.strip().partition("/")
    if not bajo:
        return None
    try:
        return (int(alto, 16) << 32) + int(bajo, 16)
    except ValueError:
        return None


def current_write_lsn(uow: SqlUnitOfWork) -> str | None:
    """LSN actual del primario, o `None` si el motor no es PostgreSQL.

    En SQLite devuelve `None` y todo el mecanismo queda desactivado, que es exactamente
    lo que hace falta en las pruebas.
    """
    if uow.session.bind is None or uow.session.bind.dialect.name != "postgresql":
        return None
    try:
        return uow.session.execute(text("SELECT pg_current_wal_lsn()")).scalar()
    except Exception:  # pragma: no cover - solo si el motor no lo soporta
        return None


@dataclass(frozen=True, slots=True)
class RouteDecision:
    uow: SqlUnitOfWork
    target: str  # "primary" | "replica"
    reason: str


class ReadRouter:
    """Decide a que base va cada consulta.

    Se le pasan las dos fabricas de unidad de trabajo. Si la de lectura es la misma que
    la de escritura, no hay replica y la decision es trivial.
    """

    def __init__(
        self,
        write_factory: Callable[[], SqlUnitOfWork],
        read_factory: Callable[[], SqlUnitOfWork] | None,
    ) -> None:
        self._write = write_factory
        self._read = read_factory
        self._log = get_logger("control_node")

    @property
    def has_replica(self) -> bool:
        return self._read is not None

    def for_read(self, client_lsn: str | None = None) -> RouteDecision:
        if self._read is None:
            return RouteDecision(self._write(), "primary", "sin replica configurada")

        objetivo = lsn_to_int(client_lsn)
        if objetivo is None:
            # El cliente no ha escrito nada en esta sesion (o mando basura): no hay nada
            # que pueda no ver todavia.
            return RouteDecision(self._read(), "replica", "sin lsn del cliente")

        lectura = self._read()
        alcanzado = self._replay_lsn(lectura)

        if alcanzado is not None and alcanzado >= objetivo:
            return RouteDecision(lectura, "replica", "replica al dia")

        # La replica va por detras de lo que este cliente ya escribio. Servirle desde
        # aqui seria negarle su propia escritura.
        lectura.__exit__(None, None, None)
        self._log.info(
            "query.routed_to_primary",
            reason="replica_lag",
            client_lsn=client_lsn,
            replica_lsn=alcanzado,
        )
        return RouteDecision(self._write(), "primary", "replica retrasada")

    def _replay_lsn(self, uow: SqlUnitOfWork) -> int | None:
        """Hasta donde ha reproducido la replica.

        Devuelve `None` si el nodo no esta en recuperacion, que es lo que pasa si
        `DFSHA_DB_REPLICA_URL` apunta por error a un primario. El efecto de ese `None` es
        mandar la consulta al primario: equivocarse de lado es preferible a servir datos
        de los que no se sabe nada.
        """
        try:
            valor = uow.session.execute(text("SELECT pg_last_wal_replay_lsn()")).scalar()
        except Exception as exc:  # pragma: no cover - fallo de red con la replica
            self._log.warning("query.replica_unreachable", error=type(exc).__name__)
            return None
        return lsn_to_int(valor)
