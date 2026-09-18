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
    "CommitRequest",
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
    "GcDispatchResponse",
    "ShareRequest",
    "UnshareRequest",
    "GroupRequest",
    "GroupMemberRequest",
    "GroupInfo",
    "GroupsResponse",
    "AclGrant",
    "AclResponse",
    "SharedEntryInfo",
    "SharedWithMeResponse",
    "LeadershipResponse",
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
    """Un bloque a subir, con las dos direcciones que hacen falta.

    `replicas` lleva las direcciones alcanzables por el CLIENTE; `pipeline`, las
    alcanzables entre DATANODES para el resto de la cadena. Son listas distintas porque
    son dos redes distintas: en compose el cliente esta fuera y los nodos dentro.

    El cliente manda los bytes a `replicas[0].base_url` y copia `pipeline` **tal cual** a
    la cabecera `X-DFSha-Pipeline`. No construye ni deduce direcciones de par, asi que no
    puede equivocarse al hacerlo; el ControlNode tampoco elige cual mandar, manda las dos
    y cada una va en su sitio del mensaje.
    """

    block_id: str
    index: int
    size: int
    replicas: list[ReplicaRef]
    #: Vacia con R=1, y tambien cuando no se configura direccion de par: entonces la
    #: cadena usa las mismas direcciones y el despliegue es el simple, no uno degradado.
    pipeline: list[str] = []
    #: Autorizacion de ESCRITURA para este bloque, firmada por el ControlNode. El cliente
    #: la copia en `X-DFSha-Block-Token` y el DataNode la verifica contra la CA. Viaja en
    #: el plan y no en una llamada aparte porque es la misma decision: el ControlNode ya
    #: comprobo el permiso al construirlo. Ver common/blocktoken.py.
    token: str = ""


class BlockReadPlan(_Dto):
    """Un bloque a descargar, con el checksum que el cliente debe verificar."""

    block_id: str
    index: int
    size: int
    checksum_sha256: str
    replicas: list[ReplicaRef]
    #: Autorizacion de LECTURA para este bloque. Ver `BlockWritePlan.token`.
    token: str = ""


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
    #: Sal del KDF con la que el CLIENTE deriva su clave maestra. No es un secreto: su
    #: trabajo es que dos usuarios con la misma contrasena no compartan clave. Sin la
    #: contrasena no sirve de nada, y el cliente la necesita para poder reconstruir su
    #: clave en una sesion nueva.
    kdf_salt: str = ""


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
    #: Estado de replicacion, derivado en el momento de la consulta:
    #: FULLY_REPLICATED / UNDER_REPLICATED / UNAVAILABLE. `None` en directorios.
    replication_state: str | None = None
    #: Copias del bloque PEOR replicado. El minimo y no la media: la durabilidad de un
    #: archivo la marca su bloque mas debil.
    min_replicas: int | None = None
    max_replicas: int | None = None
    replication_factor: int | None = None


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
    #: Bytes que el cifrado anade a CADA bloque (16 con AES-GCM: su etiqueta de
    #: autenticacion). Lo declara el cliente porque es el quien cifra.
    #:
    #: Hace falta porque `blocks.size` es el tamano ALMACENADO, no el claro: es lo que el
    #: DataNode tiene en disco y lo que cuentan la cuota, el GC y la re-replicacion.
    #: `files.size` sigue siendo el tamano claro, que es el que el usuario ve. Sin esta
    #: distincion, el cliente descargaba 16 bytes mas de los que el metadato decia y la
    #: comprobacion de tamano fallaba.
    cipher_overhead: int = Field(default=0, ge=0)


class CreateFileResponse(_Dto):
    file_id: str
    block_size: int
    expires_at: datetime
    blocks: list[BlockWritePlan]


class CommitRequest(_Dto):
    """Lo que acompana al commit. Hoy solo la clave envuelta.

    Viaja aqui y no en un endpoint propio porque es metadato que tiene que quedar durable
    justo cuando el archivo se hace visible, que es la definicion del commit. Y no puede
    ir en `create` porque se envuelve con el `file_id`, que ahi todavia no existe.
    """

    wrapped_key: str = ""
    key_algo: str = ""


class CommitResponse(_Dto):
    path: str
    size: int
    block_count: int


class OpenFileResponse(_Dto):
    file_id: str
    size: int
    block_size: int
    blocks: list[BlockReadPlan]
    #: La misma envoltura que se guardo al crear. El cliente la abre con su clave
    #: maestra; el servidor se limita a devolverla. Vacia = archivo sin cifrar, que es
    #: como se reconocen los de las Etapas 1 y 2.
    wrapped_key: str = ""
    key_algo: str = ""


# --- RF3: open / read / write / lock ---------------------------------------


class LockRequest(_Dto):
    path: str
    #: "shared" o "exclusive".
    mode: str = "exclusive"
    #: Identificador de SESION. Vacio = el id del usuario. Va explicito porque el titular
    #: de un lock es una sesion y no una persona: Ana desde dos maquinas tiene que poder
    #: excluirse a si misma, o el lock no excluiria nada entre sus propios procesos.
    holder: str = ""


class OpenRequest(_Dto):
    path: str
    #: "read" o "write". Decide el permiso exigido y el modo del lock.
    mode: str = "read"
    #: Si ademas hay que bloquear. Abrir y bloquear ocurren en la MISMA transaccion: con
    #: dos llamadas, entre una y otra otro cliente puede tomar el lock.
    lock: bool = False
    holder: str = ""


class LocksResponse(_Dto):
    path: str
    holders: list["LockHolder"] = []


class LockResponse(_Dto):
    """El lock concedido. `holder` y `epoch` juntos son el token de aislamiento.

    El cliente los reenvia en cada escritura y el ControlNode los verifica **dentro de la
    transaccion** que escribe. No es un identificador opaco a proposito: que se vea la
    epoca hace legible en los logs y en `dfsha locks` que cada concesion es un episodio
    nuevo.
    """

    file_id: str
    path: str
    holder: str
    epoch: int
    mode: str
    expires_at: datetime
    lease_seconds: float


class LockHolder(_Dto):
    holder: str
    mode: str
    epoch: int
    expires_at: datetime


class OpenResponse(_Dto):
    """El «handle» del RF3, que es **solo datos**.

    El ControlNode no guarda handles abiertos: es stateless desde la Etapa 1, y guardarlos
    obligaria a sesiones pegajosas en el balanceador. Lo que el cliente recibe es todo lo
    que necesita para operar, y si se pierde, basta volver a abrir.
    """

    file_id: str
    path: str
    size: int
    block_size: int
    mode: str
    wrapped_key: str = ""
    key_algo: str = ""
    #: Presente solo si se pidio bloqueo al abrir.
    lock: LockResponse | None = None


class ReadRangeResponse(_Dto):
    """Los bloques que intersectan un rango, con que trozo de cada uno hace falta.

    `skip` y `take` van en bytes CLAROS sobre el bloque ya descifrado: **el bloque se
    descarga entero**. Un bloque cifrado con AES-GCM no se puede descifrar por partes,
    porque la etiqueta cubre el bloque completo. Lo que el rango ahorra —y es casi todo el
    ahorro real— es no bajar los bloques que no intersectan.
    """

    file_id: str
    offset: int
    length: int
    #: Tamano claro del archivo, para que el cliente sepa si pidio mas alla del final.
    size: int
    blocks: list["RangeBlock"] = []


class RangeBlock(_Dto):
    block_id: str
    index: int
    size: int
    checksum_sha256: str
    replicas: list[ReplicaRef]
    token: str = ""
    #: Bytes a descartar del principio del bloque ya descifrado.
    skip: int = 0
    #: Bytes a quedarse a partir de ahi.
    take: int = 0


class AppendRequest(_Dto):
    size: int = Field(gt=0)
    cipher_overhead: int = Field(default=0, ge=0)
    #: El par de aislamiento del lock. Vacio = el archivo no esta bloqueado.
    lock_holder: str = ""
    lock_epoch: int = 0


class AppendCommitRequest(_Dto):
    """Lo que el cliente devuelve tras subir los bloques del append.

    Manda los `block_ids` y el tamano que reclama en vez de que el servidor los deduzca,
    porque el servidor no sabe cuantos de los bloques planificados llego a subir de verdad:
    lo unico que sabe es cuales tienen quorum, y eso es lo que comprueba.
    """

    block_ids: list[str] = []
    new_size: int = Field(ge=0)
    #: `block_id` del bloque de cola que se sustituye. Vacio si no habia cola que
    #: reescribir. Se desliga en la misma transaccion en que se enganchan los nuevos.
    replaces: str = ""
    lock_holder: str = ""
    lock_epoch: int = 0


class AppendResponse(_Dto):
    file_id: str
    block_size: int
    expires_at: datetime
    #: El bloque de cola a reescribir, o null si el archivo acaba en bloque lleno.
    #: El CLIENTE lo baja, lo descifra, le pega los datos nuevos y sube el resultado como
    #: el primero de `blocks`. Lo hace el cliente porque con cifrado extremo a extremo el
    #: servidor no podria: la clave no sale de ahi.
    tail: BlockReadPlan | None = None
    tail_plain_size: int = 0
    blocks: list[BlockWritePlan] = []


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
    #: Autorizacion de BORRADO. El GC corre fuera del cluster y presenta un certificado
    #: de cliente, no uno de ControlNode: sin token no podria borrar nada, que es lo
    #: correcto. Quien decide que un bloque es huerfano sigue siendo el ControlNode.
    token: str = ""


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
    write_quorum: int = 1
    #: Bloques de archivos vivos con menos de `replication_factor` copias almacenadas.
    #: Es el numero que dice si la re-replicacion va al dia o va perdiendo terreno.
    under_replicated_blocks: int = 0
    #: De esos, los que se quedaron con UNA sola copia. Van primero en la cola de
    #: re-replicacion y son los unicos que estan a un fallo de perderse.
    critical_blocks: int = 0


class ShareRequest(_Dto):
    path: str
    #: Nombre de usuario o de grupo. Se busca primero como grupo propio.
    principal: str
    #: read | write | admin
    permission: str


class UnshareRequest(_Dto):
    path: str
    principal: str


class GroupRequest(_Dto):
    name: str


class GroupMemberRequest(_Dto):
    name: str
    username: str


class GroupInfo(_Dto):
    name: str
    members: list[str]


class GroupsResponse(_Dto):
    groups: list[GroupInfo]


class AclGrant(_Dto):
    """Una concesion puesta EN un directorio, sin heredar."""

    principal: str
    principal_type: Literal["USER", "GROUP"]
    permission: Literal["READ", "WRITE", "ADMIN"]
    granted_by: str
    granted_at: datetime


class AclResponse(_Dto):
    path: str
    #: Permiso EFECTIVO de quien pregunta, ya resuelto con herencia y grupos.
    effective: Literal["READ", "WRITE", "ADMIN"] | None = None
    #: De donde sale: owner, user o group. Un permiso que no se puede explicar no se
    #: puede auditar.
    source: str | None = None
    #: Ruta del directorio del que se heredo, si no era el consultado.
    inherited_from: str | None = None
    #: Concesiones puestas en ESTE directorio. Vacia si solo se hereda.
    grants: list[AclGrant] = []


class SharedEntryInfo(_Dto):
    owner: str
    name: str
    permission: Literal["READ", "WRITE", "ADMIN"]
    via_group: str | None = None
    path: str


class SharedWithMeResponse(_Dto):
    entries: list[SharedEntryInfo]


class GcDispatchResponse(_Dto):
    """Lo que dejo encolado una pasada del GC por el canal de control."""

    #: Bloques huerfanos vistos.
    blocks: int
    #: Ordenes de borrado encoladas (un bloque puede estar en varios nodos).
    orders: int
    #: Bloques que no se encolaron porque ya tenian una tarea viva.
    skipped: int


class LeadershipResponse(_Dto):
    """Quien sostiene el lease del ControlNode ahora mismo.

    `epoch` es lo que hace util a este endpoint durante una demostracion: al matar al
    lider, la epoca del siguiente tiene que ser exactamente una mas. Si se repitiera, el
    token de aislamiento no serviria para nada.
    """

    leader_id: str | None
    epoch: int
    #: Si ESTA instancia (la que responde) es la que sostiene el lease. Con tres
    #: ControlNodes tras un balanceador, es la unica forma de saber a quien te atendio.
    is_self: bool
    #: Identidad de la instancia que responde, lider o no.
    instance_id: str
    expires_in_seconds: float
    acquired_at: datetime | None = None
    renewed_at: datetime | None = None


class ErrorResponse(_Dto):
    code: str
    message: str
    details: dict | None = None
