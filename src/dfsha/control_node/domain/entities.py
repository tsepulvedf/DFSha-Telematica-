"""Entidades del dominio.

Son dataclasses puras: ni SQLAlchemy ni Pydantic. Los repositorios traducen entre estas y
las filas de la base de datos, y la capa `api` traduce entre estas y los DTOs. Esa doble
traduccion es justamente lo que permite cambiar SQLite por PostgreSQL en la Etapa 3 sin
tocar una linea de logica.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum

__all__ = [
    "FileState",
    "NodeStats",
    "ReplicaState",
    "DataNodeState",
    "User",
    "Directory",
    "File",
    "Block",
    "BlockReplica",
    "DataNode",
    "utcnow",
]


def utcnow() -> datetime:
    """Ahora, siempre con tzinfo. Un datetime naive comparado con uno aware revienta, y
    ese bug aparece justo en la comprobacion de vencimiento de reservas."""
    return datetime.now(timezone.utc)


class FileState(StrEnum):
    WRITING = "WRITING"
    COMMITTED = "COMMITTED"
    DELETED = "DELETED"


class ReplicaState(StrEnum):
    PENDING = "PENDING"
    STORED = "STORED"
    #: El nodo que la tenia dejo de reportarla, o perdio su disco. El ControlNode NUNCA
    #: borra datos por esto: solo lo anota. En la Etapa 3, MISSING es lo que dispara la
    #: re-replicacion.
    MISSING = "MISSING"


class DataNodeState(StrEnum):
    ALIVE = "ALIVE"
    #: Sin heartbeat el tiempo suficiente para dejar de darle bloques nuevos, pero no
    #: tanto como para dar sus replicas por perdidas. Es el estado que evita que un
    #: hipo de red cueste una re-replicacion entera.
    SUSPECT = "SUSPECT"
    DEAD = "DEAD"


@dataclass(frozen=True, slots=True)
class User:
    id: str
    username: str
    password_hash: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Directory:
    """Un directorio. `parent_id is None` marca la raiz de un usuario."""

    id: str
    parent_id: str | None
    name: str
    owner_id: str
    created_at: datetime
    deleted_at: datetime | None = None

    @property
    def is_root(self) -> bool:
        return self.parent_id is None

    @property
    def is_live(self) -> bool:
        """Un directorio borrado es invisible y su nombre queda libre, pero su fila
        sigue ahi mientras los archivos que contenia esperen al GC."""
        return self.deleted_at is None


@dataclass(frozen=True, slots=True)
class File:
    id: str
    directory_id: str
    name: str
    owner_id: str
    size: int
    block_size: int
    state: FileState
    created_at: datetime
    committed_at: datetime | None = None
    expires_at: datetime | None = None
    deleted_at: datetime | None = None

    def is_expired(self, now: datetime | None = None) -> bool:
        """Solo un archivo en WRITING puede vencer: al hacer commit, `expires_at` se
        pone a NULL."""
        if self.state is not FileState.WRITING or self.expires_at is None:
            return False
        return self.expires_at <= (now or utcnow())

    def is_visible(self, now: datetime | None = None) -> bool:
        """Lo que `ls`, `stat` y `open` pueden ver. Un archivo a medio escribir no existe
        para el resto del mundo."""
        return self.state is FileState.COMMITTED

    def holds_name(self, now: datetime | None = None) -> bool:
        """True si este archivo ocupa el nombre frente a un `create` nuevo.

        Una reserva vencida no lo ocupa: el cliente que la pidio ya no esta.
        """
        if self.state is FileState.DELETED:
            return False
        if self.state is FileState.COMMITTED:
            return True
        return not self.is_expired(now)


@dataclass(frozen=True, slots=True)
class Block:
    block_id: str
    file_id: str
    index: int
    size: int
    checksum_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class BlockReplica:
    block_id: str
    data_node_id: str
    state: ReplicaState
    created_at: datetime

    @property
    def is_stored(self) -> bool:
        return self.state is ReplicaState.STORED


@dataclass(frozen=True, slots=True)
class NodeStats:
    """Lo que el nodo reporto en su ultimo heartbeat.

    `disk_free_bytes` sale de `shutil.disk_usage` en el DataNode, nunca de
    `capacity - used`: si el disco se llena por logs o por otro contenedor, la resta
    miente y la colocacion mandaria bloques a un nodo que no puede recibirlos.
    """

    used_bytes: int = 0
    capacity_bytes: int = 0
    disk_free_bytes: int = 0
    block_count: int = 0
    writes_in_flight: int = 0
    reads_in_flight: int = 0
    bytes_written_60s: int = 0

    @property
    def load_ratio(self) -> float:
        """Fraccion de capacidad ocupada. Un nodo sin capacidad conocida se considera
        lleno, para que no gane el desempate por accidente."""
        if self.capacity_bytes <= 0:
            return 1.0
        return min(1.0, self.used_bytes / self.capacity_bytes)


@dataclass(frozen=True, slots=True)
class DataNode:
    id: str
    #: Direccion alcanzable por el CLIENTE. El DTO que viaja al cliente la llama
    #: `base_url` por compatibilidad con el contrato de la Etapa 1.
    advertise_url: str
    capacity_bytes: int
    used_bytes: int
    state: DataNodeState
    registered_at: datetime
    #: Cadena opaca: el ControlNode solo compara igualdad. Eso hace que el mismo codigo
    #: sirva para simular dominios en local y para ser real en AWS.
    fault_domain: str = ""
    #: UUID nuevo en cada arranque con disco vacio. Distinguir "volvio el mismo nodo" de
    #: "volvio con el disco perdido" es lo que decide si sus replicas se recuperan.
    boot_id: str = ""
    last_heartbeat_at: datetime | None = None
    last_sequence: int = 0
    stats: NodeStats = field(default_factory=NodeStats)

    @property
    def is_alive(self) -> bool:
        return self.state is DataNodeState.ALIVE

    @property
    def free_bytes(self) -> int:
        """Espacio libre segun lo que el ControlNode tiene registrado.

        Es una estimacion, no la verdad: el numero honesto es el `disk_free_bytes` que el
        DataNode calcula con `shutil.disk_usage`, y que llega al ControlNode con el
        heartbeat de la Etapa 2. Hasta entonces esto solo sirve para descartar nodos
        obviamente llenos.
        """
        return max(0, self.capacity_bytes - self.used_bytes)

    def has_room_for(self, size: int) -> bool:
        return self.free_bytes >= size
