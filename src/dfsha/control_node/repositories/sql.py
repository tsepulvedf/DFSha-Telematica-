"""Implementacion SQLAlchemy de los repositorios de `base.py`.

Este modulo es el unico sitio del ControlNode donde se escribe SQL. Traduce filas a
entidades de dominio en la frontera: nada de aqui sale hacia arriba.

Nota sobre el flush en los metodos `add`: los modelos no declaran `relationship()`, asi
que SQLAlchemy no conoce las dependencias entre ellos y ordena los INSERT de un mismo
flush por nombre de tabla. Eso mandaria `directories` antes que `users` y
`block_replicas` antes que `blocks`, y la clave foranea fallaria. Cada `add` hace flush
para que el orden de insercion sea el orden logico del llamador. Sigue habiendo una sola
transaccion: el flush escribe, el commit es el que confirma.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Iterable, Sequence

from sqlalchemy import case, delete, func, select, update
from sqlalchemy.orm import Session

from dfsha.control_node.domain.entities import (
    Block,
    BlockReplica,
    DataNode,
    DataNodeState,
    Directory,
    File,
    FileState,
    ReplicaState,
    User,
)

from .models import BlockReplicaRow, BlockRow, DataNodeRow, DirectoryRow, FileRow, UserRow

__all__ = [
    "new_id",
    "SqlUserRepository",
    "SqlDirectoryRepository",
    "SqlFileRepository",
    "SqlBlockRepository",
    "SqlDataNodeRepository",
    "SqlUnitOfWork",
]


def new_id() -> str:
    return str(uuid.uuid4())


# --- Traduccion fila -> entidad -------------------------------------------


def _to_user(row: UserRow) -> User:
    return User(
        id=row.id,
        username=row.username,
        password_hash=row.password_hash,
        created_at=row.created_at,
    )


def _to_directory(row: DirectoryRow) -> Directory:
    return Directory(
        id=row.id,
        parent_id=row.parent_id,
        name=row.name,
        owner_id=row.owner_id,
        created_at=row.created_at,
        deleted_at=row.deleted_at,
    )


def _to_file(row: FileRow) -> File:
    return File(
        id=row.id,
        directory_id=row.directory_id,
        name=row.name,
        owner_id=row.owner_id,
        size=row.size,
        block_size=row.block_size,
        state=FileState(row.state),
        created_at=row.created_at,
        committed_at=row.committed_at,
        expires_at=row.expires_at,
        deleted_at=row.deleted_at,
    )


def _to_block(row: BlockRow) -> Block:
    return Block(
        block_id=row.block_id,
        file_id=row.file_id,
        index=row.index,
        size=row.size,
        checksum_sha256=row.checksum_sha256,
    )


def _to_replica(row: BlockReplicaRow) -> BlockReplica:
    return BlockReplica(
        block_id=row.block_id,
        data_node_id=row.data_node_id,
        state=ReplicaState(row.state),
        created_at=row.created_at,
    )


def _to_data_node(row: DataNodeRow) -> DataNode:
    return DataNode(
        id=row.id,
        base_url=row.base_url,
        capacity_bytes=row.capacity_bytes,
        used_bytes=row.used_bytes,
        state=DataNodeState(row.state),
        registered_at=row.registered_at,
    )


# --- Repositorios ----------------------------------------------------------


class SqlUserRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, user: User) -> None:
        self._session.add(
            UserRow(
                id=user.id,
                username=user.username,
                password_hash=user.password_hash,
                created_at=user.created_at,
            )
        )
        self._session.flush()

    def get(self, user_id: str) -> User | None:
        row = self._session.get(UserRow, user_id)
        return _to_user(row) if row else None

    def get_by_username(self, username: str) -> User | None:
        row = self._session.scalar(select(UserRow).where(UserRow.username == username))
        return _to_user(row) if row else None


class SqlDirectoryRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, directory: Directory) -> None:
        self._session.add(
            DirectoryRow(
                id=directory.id,
                parent_id=directory.parent_id,
                name=directory.name,
                owner_id=directory.owner_id,
                created_at=directory.created_at,
            )
        )
        self._session.flush()

    def get(self, directory_id: str) -> Directory | None:
        row = self._session.get(DirectoryRow, directory_id)
        return _to_directory(row) if row else None

    def get_root(self, owner_id: str) -> Directory | None:
        row = self._session.scalar(
            select(DirectoryRow).where(
                DirectoryRow.owner_id == owner_id,
                DirectoryRow.parent_id.is_(None),
                DirectoryRow.deleted_at.is_(None),
            )
        )
        return _to_directory(row) if row else None

    def get_child(self, parent_id: str, name: str) -> Directory | None:
        row = self._session.scalar(
            select(DirectoryRow).where(
                DirectoryRow.parent_id == parent_id,
                DirectoryRow.name == name,
                DirectoryRow.deleted_at.is_(None),
            )
        )
        return _to_directory(row) if row else None

    def list_children(self, directory_id: str) -> list[Directory]:
        rows = self._session.scalars(
            select(DirectoryRow)
            .where(
                DirectoryRow.parent_id == directory_id,
                DirectoryRow.deleted_at.is_(None),
            )
            .order_by(DirectoryRow.name)
        )
        return [_to_directory(row) for row in rows]

    def count_children(self, directory_id: str) -> int:
        subdirs = self._session.scalar(
            select(func.count())
            .select_from(DirectoryRow)
            .where(
                DirectoryRow.parent_id == directory_id,
                DirectoryRow.deleted_at.is_(None),
            )
        )
        files = self._session.scalar(
            select(func.count())
            .select_from(FileRow)
            .where(
                FileRow.directory_id == directory_id,
                FileRow.state == FileState.COMMITTED.value,
            )
        )
        return int(subdirs or 0) + int(files or 0)

    def move(self, directory_id: str, new_parent_id: str, new_name: str) -> None:
        self._session.execute(
            update(DirectoryRow)
            .where(DirectoryRow.id == directory_id)
            .values(parent_id=new_parent_id, name=new_name)
        )

    def mark_deleted(self, directory_id: str, deleted_at: datetime) -> None:
        self._session.execute(
            update(DirectoryRow)
            .where(DirectoryRow.id == directory_id)
            .values(deleted_at=deleted_at)
        )

    def mark_many_deleted(self, directory_ids: Sequence[str], deleted_at: datetime) -> None:
        if not directory_ids:
            return
        self._session.execute(
            update(DirectoryRow)
            .where(DirectoryRow.id.in_(list(directory_ids)))
            .values(deleted_at=deleted_at)
        )

    def list_descendants(self, directory_id: str) -> list[Directory]:
        """Recorrido por niveles: una consulta por nivel de profundidad.

        Un CTE recursivo seria una sola consulta, pero la sintaxis difiere entre SQLite y
        PostgreSQL y el arbol de un usuario no justifica esa deuda. Si `rmdir -r` sobre
        arboles muy profundos se vuelve un problema, este es el sitio que hay que cambiar.
        """
        encontrados: list[Directory] = []
        nivel = [directory_id]
        while nivel:
            rows = self._session.scalars(
                select(DirectoryRow).where(
                    DirectoryRow.parent_id.in_(nivel),
                    DirectoryRow.deleted_at.is_(None),
                )
            ).all()
            if not rows:
                break
            encontrados.extend(_to_directory(row) for row in rows)
            nivel = [row.id for row in rows]
        return encontrados


class SqlFileRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, file: File) -> None:
        self._session.add(
            FileRow(
                id=file.id,
                directory_id=file.directory_id,
                name=file.name,
                owner_id=file.owner_id,
                size=file.size,
                block_size=file.block_size,
                state=file.state.value,
                created_at=file.created_at,
                committed_at=file.committed_at,
                expires_at=file.expires_at,
                deleted_at=file.deleted_at,
            )
        )
        self._session.flush()

    def get(self, file_id: str) -> File | None:
        row = self._session.get(FileRow, file_id)
        return _to_file(row) if row else None

    def get_live_by_name(self, directory_id: str, name: str) -> File | None:
        # El COMMITTED manda; si no lo hay, la reserva WRITING mas reciente. Ordenar por
        # estado no valdria (alfabeticamente COMMITTED va antes que WRITING por
        # casualidad), asi que se consulta en dos pasos explicitos.
        committed = self._session.scalar(
            select(FileRow).where(
                FileRow.directory_id == directory_id,
                FileRow.name == name,
                FileRow.state == FileState.COMMITTED.value,
            )
        )
        if committed is not None:
            return _to_file(committed)

        writing = self._session.scalar(
            select(FileRow)
            .where(
                FileRow.directory_id == directory_id,
                FileRow.name == name,
                FileRow.state == FileState.WRITING.value,
            )
            .order_by(FileRow.created_at.desc())
            .limit(1)
        )
        return _to_file(writing) if writing else None

    def list_committed(self, directory_id: str) -> list[File]:
        rows = self._session.scalars(
            select(FileRow)
            .where(
                FileRow.directory_id == directory_id,
                FileRow.state == FileState.COMMITTED.value,
            )
            .order_by(FileRow.name)
        )
        return [_to_file(row) for row in rows]

    def count_committed(self, directory_id: str) -> int:
        total = self._session.scalar(
            select(func.count())
            .select_from(FileRow)
            .where(
                FileRow.directory_id == directory_id,
                FileRow.state == FileState.COMMITTED.value,
            )
        )
        return int(total or 0)

    def mark_committed(self, file_id: str, committed_at: datetime) -> None:
        self._session.execute(
            update(FileRow)
            .where(FileRow.id == file_id)
            .values(
                state=FileState.COMMITTED.value,
                committed_at=committed_at,
                expires_at=None,  # un archivo confirmado ya no vence
            )
        )

    def mark_deleted(self, file_id: str, deleted_at: datetime) -> None:
        self._session.execute(
            update(FileRow)
            .where(FileRow.id == file_id)
            .values(state=FileState.DELETED.value, deleted_at=deleted_at, expires_at=None)
        )

    def mark_many_deleted(self, file_ids: Sequence[str], deleted_at: datetime) -> None:
        if not file_ids:
            return
        self._session.execute(
            update(FileRow)
            .where(FileRow.id.in_(list(file_ids)))
            .values(state=FileState.DELETED.value, deleted_at=deleted_at, expires_at=None)
        )

    def move(self, file_id: str, directory_id: str, name: str) -> None:
        self._session.execute(
            update(FileRow)
            .where(FileRow.id == file_id)
            .values(directory_id=directory_id, name=name)
        )

    def list_ids_in_directories(self, directory_ids: Sequence[str]) -> list[str]:
        if not directory_ids:
            return []
        rows = self._session.scalars(
            select(FileRow.id).where(
                FileRow.directory_id.in_(list(directory_ids)),
                FileRow.state != FileState.DELETED.value,
            )
        )
        return list(rows)


class SqlBlockRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add_plan(self, blocks: Iterable[Block], replicas: Iterable[BlockReplica]) -> None:
        """Inserta el plan completo con exactamente dos flushes, pase lo que pase.

        Aqui no vale la regla de "un flush por add" del resto del modulo: un archivo de
        50 MB con bloques de 1 MB son 100 filas, y un flush por fila serian 100 viajes a
        la base de datos en la ruta caliente de `create`. Un flush para todos los bloques
        y otro para todas las replicas basta para respetar el orden de las claves
        foraneas. Sigue siendo una sola transaccion.
        """
        for block in blocks:
            self._session.add(
                BlockRow(
                    block_id=block.block_id,
                    file_id=block.file_id,
                    index=block.index,
                    size=block.size,
                    checksum_sha256=block.checksum_sha256,
                )
            )
        # Los bloques tienen que existir antes que sus replicas. Ver la nota de
        # ordenacion en el docstring de la clase.
        self._session.flush()
        for replica in replicas:
            self._session.add(
                BlockReplicaRow(
                    block_id=replica.block_id,
                    data_node_id=replica.data_node_id,
                    state=replica.state.value,
                    created_at=replica.created_at,
                )
            )
        self._session.flush()

    def list_for_file(self, file_id: str) -> list[Block]:
        rows = self._session.scalars(
            select(BlockRow).where(BlockRow.file_id == file_id).order_by(BlockRow.index)
        )
        return [_to_block(row) for row in rows]

    def list_replicas(self, block_ids: Sequence[str]) -> dict[str, list[BlockReplica]]:
        if not block_ids:
            return {}
        rows = self._session.scalars(
            select(BlockReplicaRow).where(BlockReplicaRow.block_id.in_(list(block_ids)))
        )
        agrupadas: dict[str, list[BlockReplica]] = {}
        for row in rows:
            agrupadas.setdefault(row.block_id, []).append(_to_replica(row))
        return agrupadas

    def mark_stored(
        self, block_id: str, data_node_id: str, size: int, checksum_sha256: str
    ) -> bool:
        block = self._session.get(BlockRow, block_id)
        if block is None:
            return False

        actualizadas = self._session.execute(
            update(BlockReplicaRow)
            .where(
                BlockReplicaRow.block_id == block_id,
                BlockReplicaRow.data_node_id == data_node_id,
            )
            .values(state=ReplicaState.STORED.value)
        ).rowcount
        if not actualizadas:
            # El bloque existe pero ese nodo no estaba en su plan: no se toca el
            # checksum, porque la notificacion no corresponde a esta copia.
            return False

        block.checksum_sha256 = checksum_sha256
        return True

    def pending_block_ids(self, file_id: str) -> list[str]:
        almacenados = (
            select(BlockReplicaRow.block_id)
            .where(BlockReplicaRow.state == ReplicaState.STORED.value)
            .scalar_subquery()
        )
        rows = self._session.scalars(
            select(BlockRow.block_id)
            .where(BlockRow.file_id == file_id, BlockRow.block_id.not_in(almacenados))
            .order_by(BlockRow.index)
        )
        return list(rows)

    def list_orphans(self, now: datetime) -> list[tuple[Block, list[BlockReplica]]]:
        huerfanos = self._session.scalars(
            select(BlockRow)
            .join(FileRow, FileRow.id == BlockRow.file_id)
            .where(
                (FileRow.state == FileState.DELETED.value)
                | (
                    (FileRow.state == FileState.WRITING.value)
                    & (FileRow.expires_at.is_not(None))
                    & (FileRow.expires_at <= now)
                )
            )
            .order_by(BlockRow.file_id, BlockRow.index)
        ).all()

        if not huerfanos:
            return []

        replicas = self.list_replicas([row.block_id for row in huerfanos])
        return [
            (_to_block(row), replicas.get(row.block_id, [])) for row in huerfanos
        ]

    def delete_blocks(self, block_ids: Sequence[str]) -> int:
        if not block_ids:
            return 0
        ids = list(block_ids)
        self._session.execute(
            delete(BlockReplicaRow).where(BlockReplicaRow.block_id.in_(ids))
        )
        borrados = self._session.execute(
            delete(BlockRow).where(BlockRow.block_id.in_(ids))
        ).rowcount
        return int(borrados or 0)

    def total_size(self, block_ids: Sequence[str]) -> int:
        if not block_ids:
            return 0
        total = self._session.scalar(
            select(func.coalesce(func.sum(BlockRow.size), 0)).where(
                BlockRow.block_id.in_(list(block_ids))
            )
        )
        return int(total or 0)


class SqlDataNodeRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def register(self, base_url: str, capacity_bytes: int, now: datetime) -> DataNode:
        row = self._session.scalar(
            select(DataNodeRow).where(DataNodeRow.base_url == base_url)
        )
        if row is None:
            row = DataNodeRow(
                id=new_id(),
                base_url=base_url,
                capacity_bytes=capacity_bytes,
                used_bytes=0,
                state=DataNodeState.ALIVE.value,
                registered_at=now,
            )
            self._session.add(row)
            self._session.flush()
        else:
            # Reinicio de un nodo ya conocido: conserva su id, porque las replicas
            # existentes apuntan a el.
            row.capacity_bytes = capacity_bytes
            row.state = DataNodeState.ALIVE.value
        return _to_data_node(row)

    def get(self, data_node_id: str) -> DataNode | None:
        row = self._session.get(DataNodeRow, data_node_id)
        return _to_data_node(row) if row else None

    def list_alive(self) -> list[DataNode]:
        rows = self._session.scalars(
            select(DataNodeRow)
            .where(DataNodeRow.state == DataNodeState.ALIVE.value)
            .order_by(DataNodeRow.registered_at)
        )
        return [_to_data_node(row) for row in rows]

    def add_used_bytes(self, data_node_id: str, delta: int) -> None:
        """Ajusta el ocupado que el ControlNode tiene registrado de ese nodo.

        Esto es una cache, no la fuente de verdad. La fuente de verdad es el disco del
        DataNode, que la reporta en `/health` calculandola del estado real de los
        ficheros. Este contador se desvia en cuanto haya un bloque escrito cuya
        notificacion se perdio, una notificacion duplicada o un borrado hecho fuera del
        GC.

        Importa para la Etapa 2: la politica de colocacion debe leer `used_bytes` y
        `disk_free_bytes` del heartbeat, no este contador. Sirve para decisiones baratas
        entre heartbeats, nunca para decidir si un nodo tiene sitio de verdad.
        """
        # Se calcula en la base de datos, no leyendo y reescribiendo desde Python, para
        # que dos confirmaciones simultaneas no se pisen la una a la otra.
        # El CASE mantiene el contador en cero o mas. Nada de `max(a, b)`: en SQLite es
        # una funcion escalar, pero en PostgreSQL `max` es agregada y esto reventaria en
        # la Etapa 3.
        nuevo = DataNodeRow.used_bytes + delta
        self._session.execute(
            update(DataNodeRow)
            .where(DataNodeRow.id == data_node_id)
            .values(used_bytes=case((nuevo < 0, 0), else_=nuevo))
        )


class SqlUnitOfWork:
    """Una sesion, una transaccion, todos los repositorios dentro.

    Se usa como context manager y sale haciendo rollback salvo que se haya llamado a
    `commit`. Esa es la garantia que hace que el commit con copy-on-write (viejo a
    DELETED, nuevo a COMMITTED) sea un solo paso indivisible.
    """

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None
        # Los repositorios existen desde la construccion, no desde el `with`: hay
        # colaboradores (la politica de colocacion) que se construyen por inyeccion de
        # dependencias antes de que el caso de uso abra la unidad de trabajo. SQLAlchemy
        # no toca la base de datos hasta la primera consulta, asi que una unidad que se
        # crea y no se usa no abre ninguna conexion.
        self._abrir_sesion()

    def _abrir_sesion(self) -> None:
        self._session = self._session_factory()
        self.users = SqlUserRepository(self._session)
        self.directories = SqlDirectoryRepository(self._session)
        self.files = SqlFileRepository(self._session)
        self.blocks = SqlBlockRepository(self._session)
        self.data_nodes = SqlDataNodeRepository(self._session)

    def __enter__(self) -> "SqlUnitOfWork":
        if self._session is None:
            self._abrir_sesion()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._session is None:
            return
        try:
            # Rollback incondicional: si ya hubo commit no deshace nada, y si el caso de
            # uso salio a medias sin confirmar, deja la transaccion cerrada en vez de
            # esperar a que el cierre de la sesion lo haga por casualidad.
            self._session.rollback()
        finally:
            self._session.close()
            self._session = None

    @property
    def session(self) -> Session:
        """Solo para el lado de lectura, que arma proyecciones sin pasar por entidades."""
        assert self._session is not None, "la unidad de trabajo no esta abierta"
        return self._session

    def commit(self) -> None:
        assert self._session is not None
        self._session.commit()

    def rollback(self) -> None:
        assert self._session is not None
        self._session.rollback()

    def flush(self) -> None:
        assert self._session is not None
        self._session.flush()
