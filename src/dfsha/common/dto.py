"""DTOs compartidos entre ControlNode, DataNode y cliente.

Viven en `common/` a proposito: el cliente valida las respuestas con los mismos modelos
con los que el servidor las serializa, asi que cualquier divergencia del contrato falla en
la frontera y no tres capas mas adentro.

Son DTOs de transporte, no entidades de dominio. El dominio vive en
`control_node/domain/` y no importa nada de este modulo.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "EntryType",
    "ReplicaRef",
    "BlockWritePlan",
    "BlockReadPlan",
    "RegisterRequest",
    "LoginRequest",
    "TokenResponse",
    "LsEntry",
    "LsResponse",
    "StatResponse",
    "MkdirRequest",
    "MvRequest",
    "CreateFileRequest",
    "CreateFileResponse",
    "CommitResponse",
    "OpenFileResponse",
    "DataNodeRegisterRequest",
    "DataNodeRegisterResponse",
    "BlockStoredRequest",
    "OrphanBlock",
    "OrphanBlocksResponse",
    "GcConfirmRequest",
    "HealthResponse",
    "DataNodeStatus",
    "ClusterStatusResponse",
    "ErrorResponse",
]

EntryType = Literal["file", "directory"]


class _Dto(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- Plan de bloques -------------------------------------------------------


class ReplicaRef(_Dto):
    """Donde vive (o va a vivir) una copia de un bloque.

    El cliente habla con `base_url` directamente: los bytes nunca pasan por el
    ControlNode.
    """

    data_node_id: str
    base_url: str


class BlockWritePlan(_Dto):
    """Un bloque a subir. `replicas` es una lista desde ya, aunque en la Etapa 1 tenga un
    solo elemento; la Etapa 3 solo la hace mas larga."""

    block_id: str
    index: int
    size: int
    replicas: list[ReplicaRef]


class BlockReadPlan(_Dto):
    """Un bloque a descargar, con el checksum que el cliente debe verificar."""

    block_id: str
    index: int
    size: int
    checksum_sha256: str
    replicas: list[ReplicaRef]


# --- Autenticacion ---------------------------------------------------------


class RegisterRequest(_Dto):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=256)


class LoginRequest(_Dto):
    username: str
    password: str


class TokenResponse(_Dto):
    access_token: str
    expires_in: int
    token_type: str = "bearer"


# --- Namespace (RF1) -------------------------------------------------------


class LsEntry(_Dto):
    name: str
    type: EntryType
    size: int
    created_at: datetime


class LsResponse(_Dto):
    entries: list[LsEntry]


class StatResponse(_Dto):
    path: str
    type: EntryType
    size: int
    block_size: int | None = None
    block_count: int | None = None
    created_at: datetime


class MkdirRequest(_Dto):
    path: str
    parents: bool = False


class MvRequest(_Dto):
    src: str
    dst: str


# --- Transferencia (RF2) ---------------------------------------------------


class CreateFileRequest(_Dto):
    path: str
    size: int = Field(ge=0)
    block_size: int | None = Field(default=None, gt=0)


class CreateFileResponse(_Dto):
    file_id: str
    block_size: int
    expires_at: datetime
    blocks: list[BlockWritePlan]


class CommitResponse(_Dto):
    path: str
    size: int
    block_count: int


class OpenFileResponse(_Dto):
    file_id: str
    size: int
    block_size: int
    blocks: list[BlockReadPlan]


# --- Interno: DataNode y GC ------------------------------------------------


class DataNodeRegisterRequest(_Dto):
    base_url: str
    capacity_bytes: int = Field(ge=0)


class DataNodeRegisterResponse(_Dto):
    data_node_id: str


class BlockStoredRequest(_Dto):
    data_node_id: str
    size: int = Field(ge=0)
    checksum_sha256: str


class OrphanBlock(_Dto):
    """Un bloque a recoger.

    `size` no estaba en el contrato original, y se anadio al implementar el GC: el
    ControlNode ya conoce el tamano de cada bloque, y sin el, el script tendria que
    preguntarselo al DataNode con una peticion extra por bloque solo para poder informar
    cuantos bytes libero.
    """

    block_id: str
    size: int
    replicas: list[ReplicaRef]


class OrphanBlocksResponse(_Dto):
    blocks: list[OrphanBlock]


class GcConfirmRequest(_Dto):
    block_ids: list[str]


# --- DataNode --------------------------------------------------------------


class HealthResponse(_Dto):
    """En la Etapa 2 esto se convierte en el heartbeat, y la politica de colocacion
    depende de estos numeros: se calculan del estado real en disco, no de contadores en
    memoria.

    `disk_free_bytes` sale de `shutil.disk_usage`, nunca de `capacity_bytes - used_bytes`:
    si el disco se llena por logs o por otro contenedor, la resta miente.
    """

    status: Literal["ok", "degraded"]
    used_bytes: int
    capacity_bytes: int
    block_count: int
    disk_free_bytes: int
    data_node_id: str | None = None
    #: Etapa 2: para poder comprobar desde fuera en que dominio esta y con que disco
    #: arranco, sin tener que mirar los logs del ControlNode.
    fault_domain: str | None = None
    boot_id: str | None = None


class DataNodeStatus(_Dto):
    """Un DataNode visto desde el ControlNode.

    `state` se deriva del ultimo heartbeat en el momento de la consulta, no de una
    columna que alguien tenga que acordarse de actualizar.
    """

    data_node_id: str
    advertise_url: str
    fault_domain: str
    state: Literal["ALIVE", "SUSPECT", "DEAD"]
    used_bytes: int
    capacity_bytes: int
    disk_free_bytes: int
    block_count: int
    #: Replicas que el METADATO cree que tiene. Comparada con `block_count`, que sale del
    #: disco del nodo, la diferencia entre las dos es divergencia.
    replica_count: int
    seconds_since_heartbeat: float | None
    writes_in_flight: int
    reads_in_flight: int


class ClusterStatusResponse(_Dto):
    nodes: list[DataNodeStatus]
    replication_factor: int
    suspect_after_ms: int
    dead_after_ms: int


class ErrorResponse(_Dto):
    code: str
    message: str
    details: dict | None = None
