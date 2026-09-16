"""Modelos SQLAlchemy 2.x: la unica parte del ControlNode que sabe como se ven las tablas.

Nada fuera de `repositories/` importa este modulo. Los casos de uso hablan con las
entidades de `domain/`, que no tienen relacion con SQLAlchemy.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

__all__ = [
    "Base",
    "UserRow",
    "DirectoryRow",
    "FileRow",
    "BlockRow",
    "BlockReplicaRow",
    "DataNodeRow",
    "LeadershipRow",
    "LEADERSHIP_ROW_ID",
    "RereplicationTaskRow",
]

ID_LEN = 36
NAME_LEN = 255


class UtcDateTime(TypeDecorator):
    """Guarda siempre en UTC y devuelve siempre con tzinfo.

    SQLite no tiene tipo fecha: devuelve datetimes naive y comparar uno naive con uno
    aware lanza TypeError. Como toda la logica de vencimiento de reservas son
    comparaciones de fechas, ese fallo aparecerian justo en la ruta que decide si una
    reserva sigue viva. Se normaliza aqui, una vez, en lugar de en cada consulta.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("solo se guardan datetimes con zona horaria")
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    username: Mapped[str] = mapped_column(String(NAME_LEN), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class DirectoryRow(Base):
    """El root de cada usuario es la fila con `parent_id IS NULL`.

    Los directorios se borran de forma logica (`deleted_at`), no fisica, aunque el
    esquema de la especificacion no lo previera. La razon es una dependencia que solo
    aparece al implementar `rmdir -r`: los archivos que cuelgan de un directorio se
    marcan DELETED y sus filas tienen que sobrevivir hasta que el GC recoja sus bloques,
    pero `files.directory_id` apunta a esta tabla. Borrar la fila del directorio
    violaria esa clave foranea, y quitar la clave foranea dejaria el metadato sin quien
    lo sostenga. Un directorio con `deleted_at` es invisible y su nombre queda libre.
    """

    __tablename__ = "directories"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    parent_id: Mapped[str | None] = mapped_column(
        String(ID_LEN), ForeignKey("directories.id", ondelete="CASCADE"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(NAME_LEN), nullable=False)
    owner_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("users.id"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    # Borrado logico, igual que en files. Un directorio borrado no puede desaparecer de
    # la tabla mientras queden filas de `files` apuntando a el: esas filas son lo que le
    # dice al GC que bloques recoger, y borrarlas antes dejaria los .blk en disco sin
    # nadie que supiera de ellos. Ver la nota de la clase.
    deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    __table_args__ = (
        # Unicidad solo entre directorios vivos, para que `rmdir /a` seguido de
        # `mkdir /a` funcione mientras el /a viejo espera al GC.
        Index(
            "uq_directories_parent_name",
            "parent_id",
            "name",
            unique=True,
            sqlite_where=text("deleted_at IS NULL"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # El indice de arriba no protege las raices: en SQL dos NULL nunca colisionan, asi
        # que nada impediria dos filas con parent_id NULL para el mismo usuario, es decir
        # dos arboles para una sola cuenta. Este indice parcial es el que garantiza
        # una raiz por usuario.
        Index(
            "uq_directories_root_per_owner",
            "owner_id",
            unique=True,
            sqlite_where=text("parent_id IS NULL AND deleted_at IS NULL"),
            postgresql_where=text("parent_id IS NULL AND deleted_at IS NULL"),
        ),
        Index("ix_directories_parent", "parent_id"),
    )


class FileRow(Base):
    __tablename__ = "files"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    directory_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("directories.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(NAME_LEN), nullable=False)
    owner_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("users.id"), nullable=False, index=True
    )
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    block_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    committed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    __table_args__ = (
        # Unicidad solo entre los archivos vivos: la misma ruta puede acumular versiones
        # DELETED (copy-on-write) y reservas WRITING abandonadas sin colisionar.
        # Los dos dialectos se declaran juntos a proposito, para que la migracion a
        # PostgreSQL de la Etapa 3 no tenga que tocar el esquema.
        Index(
            "uq_files_directory_name_committed",
            "directory_id",
            "name",
            unique=True,
            sqlite_where=text("state = 'COMMITTED'"),
            postgresql_where=text("state = 'COMMITTED'"),
        ),
        Index("ix_files_directory_state", "directory_id", "state"),
        Index("ix_files_state_expires", "state", "expires_at"),
    )


class BlockRow(Base):
    __tablename__ = "blocks"

    block_id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    file_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("files.id"), nullable=False, index=True
    )
    index: Mapped[int] = mapped_column(Integer, nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (UniqueConstraint("file_id", "index", name="uq_blocks_file_index"),)


class BlockReplicaRow(Base):
    """Una fila por copia. Hoy hay una por bloque; la cardinalidad N ya esta soportada,
    asi que la Etapa 3 solo inserta mas filas."""

    __tablename__ = "block_replicas"

    block_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("blocks.block_id", ondelete="CASCADE"), primary_key=True
    )
    data_node_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("data_nodes.id"), primary_key=True
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)


class DataNodeRow(Base):
    """Un DataNode registrado.

    Las columnas de estadisticas son el ultimo heartbeat recibido, no un acumulado del
    ControlNode. Es la fuente de verdad para la politica de colocacion: `used_bytes` del
    ControlNode (mas abajo) es solo una cache que se desvia en cuanto se pierde una
    notificacion. Ver la nota de `add_used_bytes`.
    """

    __tablename__ = "data_nodes"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    #: Direccion alcanzable por el CLIENTE. El ControlNode se limita a repetirsela en
    #: el plan, porque los bytes van directos.
    advertise_url: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    #: Direccion alcanzable por OTROS DATANODES. Vacia = usar `advertise_url`.
    #:
    #: NO lleva `unique`, al contrario que `advertise_url`: dos nodos con la misma
    #: direccion de par serian un error de despliegue, pero detectarlo con una
    #: restriccion impediria el caso legitimo de dejarla vacia en varios nodos a la vez.
    peer_url: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    #: Cadena opaca: solo se compara igualdad. En local son etiquetas logicas
    #: (local-1..local-4); en AWS, zonas de disponibilidad reales.
    fault_domain: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    #: UUID nuevo en cada arranque con disco vacio. Distingue "volvio el mismo nodo" de
    #: "volvio con el disco perdido", que decide si sus replicas se recuperan.
    boot_id: Mapped[str] = mapped_column(String(ID_LEN), nullable=False, default="")

    capacity_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: Cache del ControlNode, NO la fuente de verdad. Ver stat_used_bytes.
    used_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    registered_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    # --- Ultimo heartbeat: lo que la colocacion debe mirar ---------------------
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    last_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    stat_used_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    stat_capacity_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    #: De shutil.disk_usage en el nodo, nunca de capacity - used.
    stat_disk_free_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    stat_block_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stat_writes_in_flight: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stat_reads_in_flight: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stat_bytes_written_60s: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    __table_args__ = (Index("ix_data_nodes_state", "state"),)


#: El liderazgo es una fila unica. La clave primaria fija es lo que lo garantiza: no hay
#: forma de insertar una segunda, ni por una carrera ni por un error de codigo.
LEADERSHIP_ROW_ID = 1


class LeadershipRow(Base):
    """El lease de liderazgo del ControlNode. Una sola fila, id = 1.

    Vive en la base y no en la memoria de ningun proceso a proposito: es lo unico que
    tres instancias sin estado comparten, y por tanto el unico sitio donde pueden
    ponerse de acuerdo. La exclusion la da `SELECT ... FOR UPDATE` sobre esta fila.
    """

    __tablename__ = "leadership"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    #: Quien lo sostiene. NULL solo en la fila recien sembrada, antes del primer lider.
    leader_id: Mapped[str | None] = mapped_column(String(ID_LEN), nullable=True)
    #: Token de aislamiento. SOLO SUBE: nunca baja ni se reinicia, ni siquiera cuando el
    #: mismo proceso recupera el lease que acababa de perder. Ver domain/leadership.py.
    epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    acquired_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    renewed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class RereplicationTaskRow(Base):
    """Una copia pendiente de hacer.

    La cola vive en la base y no en la memoria del lider, y eso no es casualidad: si
    viviera en memoria se perderia justo cuando mas falta hace, que es cuando el lider
    cambia de manos. Ademas el stream de heartbeat del nodo destino lo puede estar
    atendiendo OTRA instancia, que tiene que poder leer la orden para empujarsela.
    """

    __tablename__ = "rereplication_tasks"

    id: Mapped[str] = mapped_column(String(ID_LEN), primary_key=True)
    block_id: Mapped[str] = mapped_column(
        String(ID_LEN), ForeignKey("blocks.block_id", ondelete="CASCADE"), nullable=False
    )
    #: REPLICATE (copiar de un nodo a otro) o DELETE (borrar un huerfano).
    #:
    #: Las dos ordenes comparten tabla porque comparten TODO lo que las hace no
    #: triviales: van por el mismo stream, tienen que sobrevivir a un cambio de lider,
    #: no se pueden reenviar en cada latido, y hay que saber si el nodo las cumplio.
    #: Dos tablas serian dos copias del mismo mecanismo.
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="REPLICATE")
    #: PENDING -> IN_FLIGHT -> DONE | FAILED
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    #: De donde tirar los bytes. Se decide al despachar, no al detectar el hueco: entre
    #: una cosa y otra el origen elegido puede haberse caido.
    source_node_id: Mapped[str | None] = mapped_column(String(ID_LEN), nullable=True)
    target_node_id: Mapped[str | None] = mapped_column(String(ID_LEN), nullable=True)
    #: Copias que tenia el bloque cuando se detecto el hueco. Es la clave de prioridad.
    replicas_at_schedule: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    dispatched_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    #: Ultima vez que la orden se empujo por el stream. Sin esto se reenviaria en cada
    #: latido, tres veces por segundo y por nodo.
    sent_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    #: Si el destino no confirma antes de esto, la tarea vuelve a PENDING. Es el mismo
    #: mecanismo de expiracion que las reservas de escritura de la Etapa 1 y el lease de
    #: liderazgo: un cliente (aqui, un DataNode) que se cae no bloquea el recurso.
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        # Una sola tarea VIVA por bloque. Sin esto, dos pasadas del planificador (o dos
        # lideres solapados durante un relevo) programarian la misma copia dos veces.
        Index(
            "uq_rereplication_block_activa",
            "block_id",
            unique=True,
            sqlite_where=text("state IN ('PENDING', 'IN_FLIGHT')"),
            postgresql_where=text("state IN ('PENDING', 'IN_FLIGHT')"),
        ),
        Index("ix_rereplication_state", "state"),
        Index("ix_rereplication_target", "target_node_id", "state"),
    )
