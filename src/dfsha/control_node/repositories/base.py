"""Interfaces de repositorio y unidad de trabajo.

Todo el acceso a datos pasa por aqui: ni un SELECT suelto en un router o en un caso de
uso. Son `Protocol`, no clases base, para que la implementacion SQL no herede de nada y
una implementacion en memoria para pruebas no tenga que registrarse en ningun sitio.

Las firmas hablan de entidades de dominio, nunca de filas ni de sesiones de SQLAlchemy.
Eso es lo que permite que la Etapa 3 cambie SQLite por PostgreSQL, o que el lado de
lectura CQRS acabe apuntando a una replica, sin tocar la logica.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable, Protocol, Sequence, runtime_checkable

from dfsha.control_node.domain.entities import (
    Block,
    BlockReplica,
    DataNode,
    DataNodeState,
    Directory,
    File,
    NodeStats,
    ReplicaState,
    User,
)

__all__ = [
    "UserRepository",
    "DirectoryRepository",
    "FileRepository",
    "BlockRepository",
    "DataNodeRepository",
    "UnitOfWork",
]


@runtime_checkable
class UserRepository(Protocol):
    def add(self, user: User) -> None: ...

    def get(self, user_id: str) -> User | None: ...

    def get_by_username(self, username: str) -> User | None: ...


@runtime_checkable
class DirectoryRepository(Protocol):
    def add(self, directory: Directory) -> None: ...

    def get(self, directory_id: str) -> Directory | None: ...

    def get_root(self, owner_id: str) -> Directory | None:
        """Raiz del arbol del usuario: la fila con `parent_id IS NULL`."""
        ...

    def get_child(self, parent_id: str, name: str) -> Directory | None: ...

    def list_children(self, directory_id: str) -> list[Directory]: ...

    def count_children(self, directory_id: str) -> int:
        """Subdirectorios mas archivos vivos. Es lo que mira `rmdir` para decidir si el
        directorio esta vacio; un archivo DELETED no cuenta aunque sus bloques sigan en
        disco esperando al GC."""
        ...

    def move(self, directory_id: str, new_parent_id: str, new_name: str) -> None: ...

    def mark_deleted(self, directory_id: str, deleted_at: datetime) -> None:
        """Borrado logico.

        No es fisico porque `files.directory_id` apunta aqui y las filas de archivos
        borrados tienen que sobrevivir hasta que el GC recoja sus bloques. Ver la nota de
        `DirectoryRow`.
        """
        ...

    def mark_many_deleted(self, directory_ids: Sequence[str], deleted_at: datetime) -> None:
        """Borrado logico en lote para `rmdir -r`, en una sola sentencia."""
        ...

    def list_descendants(self, directory_id: str) -> list[Directory]:
        """Subarbol completo, sin incluir el propio directorio. Lo usa `rmdir -r`."""
        ...


@runtime_checkable
class FileRepository(Protocol):
    def add(self, file: File) -> None: ...

    def get(self, file_id: str) -> File | None: ...

    def get_live_by_name(self, directory_id: str, name: str) -> File | None:
        """El archivo que ocupa el nombre: el COMMITTED si lo hay, si no la reserva
        WRITING mas reciente (vencida o no).

        Devuelve tambien las reservas vencidas a proposito: quien llama necesita verlas
        para marcarlas DELETED antes de reutilizar el nombre. Decidir si siguen ocupando
        el nombre es cosa del dominio (`File.holds_name`), no del repositorio.
        """
        ...

    def list_committed(self, directory_id: str) -> list[File]: ...

    def count_committed(self, directory_id: str) -> int: ...

    def mark_committed(self, file_id: str, committed_at: datetime) -> None:
        """Pasa a COMMITTED y pone `expires_at` a NULL: un archivo confirmado ya no vence."""
        ...

    def mark_deleted(self, file_id: str, deleted_at: datetime) -> None: ...

    def mark_many_deleted(self, file_ids: Sequence[str], deleted_at: datetime) -> None:
        """Borrado en lote para `rmdir -r`, en una sola sentencia."""
        ...

    def move(self, file_id: str, directory_id: str, name: str) -> None:
        """Metadato puro: cambia el padre y el nombre. No toca un solo byte del DataNode,
        que es la consecuencia directa de que el DataNode no conozca rutas logicas."""
        ...

    def list_ids_in_directories(self, directory_ids: Sequence[str]) -> list[str]:
        """Ids de archivos vivos que cuelgan de esos directorios, para `rmdir -r`."""
        ...


@runtime_checkable
class BlockRepository(Protocol):
    def add_plan(self, blocks: Iterable[Block], replicas: Iterable[BlockReplica]) -> None:
        """Inserta el plan de escritura completo: bloques y sus replicas en PENDING."""
        ...

    def list_for_file(self, file_id: str) -> list[Block]: ...

    def list_replicas(self, block_ids: Sequence[str]) -> dict[str, list[BlockReplica]]:
        """Replicas agrupadas por `block_id`. En lote para no hacer una consulta por
        bloque al construir el plan de lectura de un archivo de miles de bloques."""
        ...

    def mark_stored(
        self, block_id: str, data_node_id: str, size: int, checksum_sha256: str
    ) -> bool:
        """Confirma la replica y fija el checksum del bloque.

        Devuelve False si el bloque no existe en el metadato, que es como el ControlNode
        detecta la notificacion de un bloque que ya no le interesa a nadie.
        """
        ...

    def pending_block_ids(self, file_id: str) -> list[str]:
        """Bloques del archivo sin ninguna replica en STORED. Vacio = se puede confirmar."""
        ...

    def list_orphans(self, now: datetime) -> list[tuple[Block, list[BlockReplica]]]:
        """Bloques de archivos DELETED y de reservas vencidas, con donde vive cada copia.

        Es la entrada del GC. Sin esto, el `used_bytes` de cada DataNode queda inflado por
        bloques muertos, y ese numero alimenta la politica de colocacion de la Etapa 2.
        """
        ...

    def delete_blocks(self, block_ids: Sequence[str]) -> int:
        """Borra bloques y sus replicas del metadato. Solo lo llama el GC, despues de
        confirmar que los bytes ya no estan en disco."""
        ...

    def total_size(self, block_ids: Sequence[str]) -> int: ...

    def list_block_ids_on_node(
        self, data_node_id: str, states: Sequence[ReplicaState] = ...
    ) -> list[str]:
        """Lo que el metadato cree que ese nodo tiene: el lado 'esperado' del report."""
        ...

    def set_replicas_state(
        self, block_ids: Sequence[str], data_node_id: str, state: ReplicaState
    ) -> int: ...

    def mark_node_replicas(
        self,
        data_node_id: str,
        state: ReplicaState,
        only_from: Sequence[ReplicaState] | None = None,
    ) -> int:
        """Cambia de estado todas las replicas de un nodo. Nunca borra filas: MISSING es
        informacion que la Etapa 3 necesita para re-replicar."""
        ...

    def count_replicas_by_node(self) -> dict[str, int]: ...


@runtime_checkable
class DataNodeRepository(Protocol):
    def register(
        self,
        advertise_url: str,
        capacity_bytes: int,
        now: datetime,
        fault_domain: str = "",
        boot_id: str = "",
        data_node_id: str | None = None,
    ) -> DataNode:
        """Alta idempotente, identificada por `data_node_id` y, si falta, por la URL.

        Reiniciar un DataNode no puede crear un nodo nuevo: sus bloques seguirian
        apuntando al id viejo. Que significa el re-alta (mismo disco o disco perdido) lo
        decide el dominio con `classify_rejoin`, no este metodo.
        """
        ...

    def get(self, data_node_id: str) -> DataNode | None: ...

    def get_by_advertise_url(self, advertise_url: str) -> DataNode | None: ...

    def list_all(self) -> list[DataNode]:
        """Todos los nodos, en cualquier estado.

        Es lo que debe usar la colocacion: el estado real se deriva del ultimo heartbeat
        con `membership.state_for`, no de la columna `state`, que solo se persiste para
        poder detectar la transicion y emitir el evento.
        """
        ...

    def list_alive(self) -> list[DataNode]:
        """Nodos cuya columna `state` dice ALIVE. Para rutas sin reloj a mano."""
        ...

    def record_heartbeat(
        self, data_node_id: str, sequence: int, stats: NodeStats, now: datetime
    ) -> None:
        """Guarda el ultimo latido: la fuente de verdad de carga y espacio libre."""
        ...

    def set_state(self, data_node_id: str, state: DataNodeState) -> None: ...

    def add_used_bytes(self, data_node_id: str, delta: int) -> None:
        """Ajusta el ocupado registrado. Positivo al confirmar un bloque, negativo cuando
        el GC lo borra.

        Es una cache del ControlNode, no la fuente de verdad: esa es el disco del
        DataNode, que la reporta en `/health`. La politica de colocacion de la Etapa 2
        debe usar el valor del heartbeat, no este contador.
        """
        ...


class UnitOfWork(Protocol):
    """Una transaccion y los repositorios que viven dentro de ella.

    Existe para que operaciones que tocan varias tablas sean atomicas. El caso que la
    justifica es el commit con copy-on-write: el archivo viejo pasa a DELETED y el nuevo a
    COMMITTED en la misma transaccion. Hacerlo en dos pasos dejaria, ante una caida en el
    medio, una ruta sin ningun archivo visible.
    """

    users: UserRepository
    directories: DirectoryRepository
    files: FileRepository
    blocks: BlockRepository
    data_nodes: DataNodeRepository

    def __enter__(self) -> "UnitOfWork": ...

    def __exit__(self, exc_type, exc, tb) -> None:
        """Sale haciendo rollback salvo que se haya llamado a `commit`."""
        ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...
