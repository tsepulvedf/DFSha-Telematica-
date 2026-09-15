"""Entidades del dominio.

Son dataclasses puras: ni SQLAlchemy ni Pydantic. Los repositorios traducen entre estas y
las filas de la base de datos, y la capa `api` traduce entre estas y los DTOs. Esa doble
traduccion es justamente lo que permite cambiar SQLite por PostgreSQL en la Etapa 3 sin
tocar una linea de logica.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

__all__ = [
    "FileState",
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


class DataNodeState(StrEnum):
    ALIVE = "ALIVE"
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

    @property
    def is_root(self) -> bool:
        return self.parent_id is None


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
class DataNode:
    id: str
    base_url: str
    capacity_bytes: int
    used_bytes: int
    state: DataNodeState
    registered_at: datetime

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
