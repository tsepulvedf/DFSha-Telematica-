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
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Sequence

from sqlalchemy import case, delete, func, select, update
from sqlalchemy.orm import Session

from dfsha.control_node.domain.leadership import Lease
from dfsha.control_node.domain.entities import (
    Block,
    BlockReplica,
    DataNode,
    DataNodeState,
    Directory,
    File,
    FileState,
    NodeStats,
    ReplicaState,
    User,
)

from .models import (
    LEADERSHIP_ROW_ID,
    RereplicationTaskRow,
    BlockReplicaRow,
    BlockRow,
    DataNodeRow,
    DirectoryRow,
    FileRow,
    LeadershipRow,
    UserRow,
)

__all__ = [
    "new_id",
    "SqlUserRepository",
    "SqlDirectoryRepository",
    "SqlFileRepository",
    "SqlBlockRepository",
    "SqlDataNodeRepository",
    "SqlLeadershipRepository",
    "SqlRereplicationRepository",
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
        advertise_url=row.advertise_url,
        peer_url=row.peer_url or "",
        capacity_bytes=row.capacity_bytes,
        used_bytes=row.used_bytes,
        state=DataNodeState(row.state),
        registered_at=row.registered_at,
        fault_domain=row.fault_domain,
        boot_id=row.boot_id,
        last_heartbeat_at=row.last_heartbeat_at,
        last_sequence=row.last_sequence,
        stats=NodeStats(
            used_bytes=row.stat_used_bytes,
            capacity_bytes=row.stat_capacity_bytes,
            disk_free_bytes=row.stat_disk_free_bytes,
            block_count=row.stat_block_count,
            writes_in_flight=row.stat_writes_in_flight,
            reads_in_flight=row.stat_reads_in_flight,
            bytes_written_60s=row.stat_bytes_written_60s,
        ),
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

    def add_replica(
        self, block_id: str, data_node_id: str, state: ReplicaState, now: datetime
    ) -> bool:
        """Registra una copia planificada en un nodo donde el bloque todavia no estaba.

        Lo usa la re-replicacion. Es la decision 3 de CLAUDE.md aplicada tambien aqui:
        **el ControlNode elige el destino y REGISTRA la eleccion**. Sin esta fila, el
        nodo destino copia el bloque y al avisar recibe un 404 ("bloque desconocido en el
        metadato"), porque `mark_stored` solo sabe actualizar una fila que ya existe; la
        copia queda en disco como huerfana y la tarea nunca se cierra.

        Idempotente: si la fila ya esta, no se toca.
        """
        existe = self._session.get(BlockReplicaRow, (block_id, data_node_id))
        if existe is not None:
            return False
        self._session.add(
            BlockReplicaRow(
                block_id=block_id,
                data_node_id=data_node_id,
                state=state.value,
                created_at=now,
            )
        )
        self._session.flush()
        return True

    def drop_replica(self, block_id: str, data_node_id: str) -> bool:
        """Quita una copia PLANIFICADA que nunca llego a escribirse.

        Solo borra si sigue en PENDING: una fila STORED es una copia real y borrarla
        seria perder la pista de bytes que estan en disco.
        """
        borradas = self._session.execute(
            delete(BlockReplicaRow).where(
                BlockReplicaRow.block_id == block_id,
                BlockReplicaRow.data_node_id == data_node_id,
                BlockReplicaRow.state == ReplicaState.PENDING.value,
            )
        ).rowcount
        return borradas > 0

    def get_many(self, block_ids: Sequence[str]) -> list[Block]:
        """Varios bloques por id, en una sola consulta.

        Existe para que empujar N ordenes por el stream no cueste N consultas: el
        heartbeat pasa por aqui cada 3 s y por cada nodo.
        """
        if not block_ids:
            return []
        filas = self._session.scalars(
            select(BlockRow).where(BlockRow.block_id.in_(list(block_ids)))
        )
        return [_to_block(fila) for fila in filas]

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
        """Bloques sin NINGUNA replica almacenada. Equivale a `blocks_below_quorum(1)`."""
        return self.blocks_below_quorum(file_id, quorum=1)

    def blocks_below_quorum(self, file_id: str, quorum: int) -> list[str]:
        """Bloques del archivo con menos de `quorum` replicas en estado STORED.

        Esta consulta es la que decide si un `commit` pasa, y por eso cuenta replicas en
        vez de mirar si hay alguna: con W=2 y R=3, un bloque con una sola copia no vale,
        aunque exista. Y con dos si vale, aunque falte la tercera.

        Un bloque sin ninguna fila de replica tambien sale aqui, porque su cuenta es 0.
        """
        almacenadas = (
            select(
                BlockReplicaRow.block_id.label("block_id"),
                func.count().label("copias"),
            )
            .where(BlockReplicaRow.state == ReplicaState.STORED.value)
            .group_by(BlockReplicaRow.block_id)
            .subquery()
        )
        rows = self._session.scalars(
            select(BlockRow.block_id)
            .outerjoin(almacenadas, almacenadas.c.block_id == BlockRow.block_id)
            .where(
                BlockRow.file_id == file_id,
                func.coalesce(almacenadas.c.copias, 0) < quorum,
            )
            .order_by(BlockRow.index)
        )
        return list(rows)

    def stored_replica_counts(self, file_id: str) -> dict[str, int]:
        """Cuantas copias STORED tiene cada bloque del archivo.

        Alimenta el estado de replicacion que muestra `stat`. Se deriva contando filas,
        nunca de una columna de estado guardada: una columna asi se queda vieja en cuanto
        una re-replicacion termina y nadie se acuerda de actualizarla.
        """
        filas = self._session.execute(
            select(BlockRow.block_id, func.count(BlockReplicaRow.block_id))
            .outerjoin(
                BlockReplicaRow,
                (BlockReplicaRow.block_id == BlockRow.block_id)
                & (BlockReplicaRow.state == ReplicaState.STORED.value),
            )
            .where(BlockRow.file_id == file_id)
            .group_by(BlockRow.block_id)
        )
        return {block_id: copias for block_id, copias in filas}

    def live_replica_counts(self) -> list[int]:
        """Copias STORED de cada bloque de un archivo COMMITTED.

        Solo archivos COMMITTED: los bloques de una reserva en curso todavia se estan
        subiendo, y contarlos como sub-replicados haria que el numero del cluster
        parpadeara con cada `put` en vuelo. Los de archivos DELETED son basura del GC, no
        replicacion que falte.
        """
        filas = self._session.execute(
            select(BlockRow.block_id, func.count(BlockReplicaRow.block_id))
            .join(FileRow, FileRow.id == BlockRow.file_id)
            .outerjoin(
                BlockReplicaRow,
                (BlockReplicaRow.block_id == BlockRow.block_id)
                & (BlockReplicaRow.state == ReplicaState.STORED.value),
            )
            .where(FileRow.state == FileState.COMMITTED.value)
            .group_by(BlockRow.block_id)
        )
        return [copias for _, copias in filas]

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

    def list_block_ids_on_node(
        self, data_node_id: str, states: Sequence[ReplicaState] = (ReplicaState.STORED,)
    ) -> list[str]:
        """Bloques que el metadato cree que ese nodo tiene.

        Es el lado "esperado" de la comparacion con lo que el nodo reporta. Por defecto
        solo STORED: un PENDING todavia se esta subiendo y su ausencia no es divergencia.
        """
        rows = self._session.scalars(
            select(BlockReplicaRow.block_id).where(
                BlockReplicaRow.data_node_id == data_node_id,
                BlockReplicaRow.state.in_([e.value for e in states]),
            )
        )
        return list(rows)

    def set_replicas_state(
        self, block_ids: Sequence[str], data_node_id: str, state: ReplicaState
    ) -> int:
        if not block_ids:
            return 0
        resultado = self._session.execute(
            update(BlockReplicaRow)
            .where(
                BlockReplicaRow.block_id.in_(list(block_ids)),
                BlockReplicaRow.data_node_id == data_node_id,
            )
            .values(state=state.value)
        )
        return int(resultado.rowcount or 0)

    def mark_node_replicas(
        self,
        data_node_id: str,
        state: ReplicaState,
        only_from: Sequence[ReplicaState] | None = None,
    ) -> int:
        """Cambia de estado TODAS las replicas de un nodo.

        Lo usan dos casos: un nodo que pasa a DEAD (sus replicas dejan de ser legibles) y
        uno que vuelve con el disco vacio (sus replicas se dan por perdidas). Nunca borra
        filas: MISSING es informacion que la Etapa 3 necesita para re-replicar.
        """
        condiciones = [BlockReplicaRow.data_node_id == data_node_id]
        if only_from is not None:
            condiciones.append(BlockReplicaRow.state.in_([e.value for e in only_from]))

        resultado = self._session.execute(
            update(BlockReplicaRow).where(*condiciones).values(state=state.value)
        )
        return int(resultado.rowcount or 0)

    def count_replicas_by_node(self) -> dict[str, int]:
        """Cuantas replicas STORED tiene cada nodo. Para `/cluster/status`."""
        filas = self._session.execute(
            select(BlockReplicaRow.data_node_id, func.count())
            .where(BlockReplicaRow.state == ReplicaState.STORED.value)
            .group_by(BlockReplicaRow.data_node_id)
        )
        return {nodo: int(total) for nodo, total in filas}

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

    def register(
        self,
        advertise_url: str,
        capacity_bytes: int,
        now: datetime,
        fault_domain: str = "",
        boot_id: str = "",
        data_node_id: str | None = None,
        peer_url: str = "",
    ) -> DataNode:
        """Alta o re-alta de un nodo.

        La identidad la manda el `data_node_id` que el nodo envia: lo persiste en su
        disco y lo reenvia en cada arranque, asi que reiniciarlo no puede producir un
        nodo nuevo al que ninguna replica apunte. Solo si no lo manda (primer arranque)
        se busca por `advertise_url`, que es el siguiente identificador mas estable.

        Este metodo no decide que significa el re-alta: eso es del dominio
        (`classify_rejoin`), que compara el `boot_id` guardado con el que llega. Aqui
        solo se escribe.
        """
        row = None
        if data_node_id:
            row = self._session.get(DataNodeRow, data_node_id)
        if row is None:
            row = self._session.scalar(
                select(DataNodeRow).where(DataNodeRow.advertise_url == advertise_url)
            )

        if row is None:
            row = DataNodeRow(
                id=data_node_id or new_id(),
                advertise_url=advertise_url,
                peer_url=peer_url,
                fault_domain=fault_domain,
                boot_id=boot_id,
                capacity_bytes=capacity_bytes,
                used_bytes=0,
                state=DataNodeState.ALIVE.value,
                registered_at=now,
                last_heartbeat_at=None,
                last_sequence=0,
                stat_capacity_bytes=capacity_bytes,
            )
            self._session.add(row)
        else:
            # Un nodo conocido conserva su id. Lo demas puede haber cambiado: se
            # redespliega con otra URL anunciada, se le cambia el dominio de falla, o
            # arranca con el disco vacio y otro boot_id.
            row.advertise_url = advertise_url
            row.peer_url = peer_url
            row.fault_domain = fault_domain
            row.boot_id = boot_id
            row.capacity_bytes = capacity_bytes
            row.state = DataNodeState.ALIVE.value
        self._session.flush()
        return _to_data_node(row)

    def get(self, data_node_id: str) -> DataNode | None:
        row = self._session.get(DataNodeRow, data_node_id)
        return _to_data_node(row) if row else None

    def get_by_advertise_url(self, advertise_url: str) -> DataNode | None:
        row = self._session.scalar(
            select(DataNodeRow).where(DataNodeRow.advertise_url == advertise_url)
        )
        return _to_data_node(row) if row else None

    def list_all(self) -> list[DataNode]:
        """Todos los nodos, en cualquier estado.

        Es lo que usan la colocacion y `/cluster/status`, porque el estado real se deriva
        del ultimo heartbeat, no de la columna. Filtrar aqui por la columna dejaria fuera
        a un nodo que acaba de volver y cuya fila todavia dice SUSPECT.
        """
        rows = self._session.scalars(
            select(DataNodeRow).order_by(DataNodeRow.registered_at)
        )
        return [_to_data_node(row) for row in rows]

    def list_alive(self) -> list[DataNode]:
        """Nodos cuya columna `state` dice ALIVE.

        Se conserva para las rutas que no tienen reloj ni umbrales a mano. Para decidir
        colocacion, usa `list_all()` y deriva el estado con `membership.state_for`.
        """
        rows = self._session.scalars(
            select(DataNodeRow)
            .where(DataNodeRow.state == DataNodeState.ALIVE.value)
            .order_by(DataNodeRow.registered_at)
        )
        return [_to_data_node(row) for row in rows]

    def record_heartbeat(
        self,
        data_node_id: str,
        sequence: int,
        stats: NodeStats,
        now: datetime,
    ) -> None:
        """Guarda el ultimo latido. Esto es la fuente de verdad de la colocacion."""
        self._session.execute(
            update(DataNodeRow)
            .where(DataNodeRow.id == data_node_id)
            .values(
                last_heartbeat_at=now,
                last_sequence=sequence,
                stat_used_bytes=stats.used_bytes,
                stat_capacity_bytes=stats.capacity_bytes,
                stat_disk_free_bytes=stats.disk_free_bytes,
                stat_block_count=stats.block_count,
                stat_writes_in_flight=stats.writes_in_flight,
                stat_reads_in_flight=stats.reads_in_flight,
                stat_bytes_written_60s=stats.bytes_written_60s,
            )
        )

    def set_state(self, data_node_id: str, state: DataNodeState) -> None:
        self._session.execute(
            update(DataNodeRow)
            .where(DataNodeRow.id == data_node_id)
            .values(state=state.value)
        )

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


@dataclass(frozen=True, slots=True)
class ExpiredTasks:
    """Lo que dejo una pasada de expiracion."""

    requeued: int
    gave_up: int
    #: (block_id, data_node_id) de los destinos que no llegaron a escribir el bloque.
    abandoned_targets: list[tuple[str, str]]


class SqlRereplicationRepository:
    """La cola de copias pendientes.

    Aqui solo hay consultas y escrituras. Quien decide QUE copiar y ADONDE es
    `domain/rereplication.py`, que es puro y se prueba sin base de datos.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    # --- Deteccion ---------------------------------------------------------

    def blocks_missing_replicas(self, replication_factor: int) -> list[dict]:
        """Bloques de archivos COMMITTED con menos de `replication_factor` copias STORED.

        Devuelve TODAS sus filas de replica, no solo las STORED: quien decide necesita
        saber que nodos las tienen (para no repetir destino), en que dominio estan, y
        desde cuando llevan pendientes (para la espera de gracia).

        Solo archivos COMMITTED. Los de una reserva en curso se estan subiendo ahora
        mismo y programar su copia seria correr detras del cliente; los de archivos
        DELETED son basura del GC, no replicacion que falte.
        """
        cortos = (
            select(BlockRow.block_id)
            .join(FileRow, FileRow.id == BlockRow.file_id)
            .outerjoin(
                BlockReplicaRow,
                (BlockReplicaRow.block_id == BlockRow.block_id)
                & (BlockReplicaRow.state == ReplicaState.STORED.value),
            )
            .where(FileRow.state == FileState.COMMITTED.value)
            .group_by(BlockRow.block_id)
            .having(func.count(BlockReplicaRow.block_id) < replication_factor)
            .scalar_subquery()
        )

        filas = self._session.execute(
            select(
                BlockRow.block_id,
                BlockRow.size,
                BlockRow.checksum_sha256,
                BlockReplicaRow.data_node_id,
                BlockReplicaRow.state,
                BlockReplicaRow.created_at,
                DataNodeRow.fault_domain,
                DataNodeRow.last_heartbeat_at,
                DataNodeRow.advertise_url,
            )
            .outerjoin(BlockReplicaRow, BlockReplicaRow.block_id == BlockRow.block_id)
            .outerjoin(DataNodeRow, DataNodeRow.id == BlockReplicaRow.data_node_id)
            .where(BlockRow.block_id.in_(cortos))
            .order_by(BlockRow.block_id)
        )

        agrupado: dict[str, dict] = {}
        for fila in filas:
            entrada = agrupado.setdefault(
                fila.block_id,
                {
                    "block_id": fila.block_id,
                    "size": fila.size,
                    "checksum_sha256": fila.checksum_sha256,
                    "replicas": [],
                },
            )
            if fila.data_node_id is not None:
                entrada["replicas"].append(
                    {
                        "data_node_id": fila.data_node_id,
                        "state": fila.state,
                        "created_at": fila.created_at,
                        "fault_domain": fila.fault_domain or "",
                        "last_heartbeat_at": fila.last_heartbeat_at,
                        "advertise_url": fila.advertise_url or "",
                    }
                )
        return list(agrupado.values())

    # --- Cola --------------------------------------------------------------

    def has_active_task(self, block_id: str) -> bool:
        total = self._session.scalar(
            select(func.count())
            .select_from(RereplicationTaskRow)
            .where(
                RereplicationTaskRow.block_id == block_id,
                RereplicationTaskRow.state.in_(("PENDING", "IN_FLIGHT")),
            )
        )
        return (total or 0) > 0

    def enqueue_delete(
        self, block_id: str, target_node_id: str, now: datetime, expires_at: datetime
    ) -> str | None:
        """Encola un borrado dirigido a un nodo concreto.

        Nace ya en IN_FLIGHT porque no hay nada que decidir: el destino lo dice quien
        encola, que es el GC y ya sabe en que nodos esta el huerfano.
        """
        if self.has_active_task(block_id):
            return None
        task_id = new_id()
        self._session.add(
            RereplicationTaskRow(
                id=task_id,
                block_id=block_id,
                kind="DELETE",
                state="IN_FLIGHT",
                target_node_id=target_node_id,
                replicas_at_schedule=0,
                created_at=now,
                dispatched_at=now,
                expires_at=expires_at,
                attempts=1,
            )
        )
        self._session.flush()
        return task_id

    def enqueue(self, block_id: str, replicas_now: int, now: datetime) -> str | None:
        """Encola una copia. `None` si ya habia una tarea viva para ese bloque.

        La comprobacion previa evita el caso normal, pero quien garantiza la unicidad es
        el indice unico parcial del esquema: dos lideres solapados durante un relevo
        pueden pasar los dos por aqui a la vez.
        """
        if self.has_active_task(block_id):
            return None
        task_id = new_id()
        self._session.add(
            RereplicationTaskRow(
                id=task_id,
                block_id=block_id,
                kind="REPLICATE",
                state="PENDING",
                replicas_at_schedule=replicas_now,
                created_at=now,
                attempts=0,
            )
        )
        self._session.flush()
        return task_id

    def list_pending(self) -> list[RereplicationTaskRow]:
        return list(
            self._session.scalars(
                select(RereplicationTaskRow)
                .where(
                    RereplicationTaskRow.state == "PENDING",
                    RereplicationTaskRow.kind == "REPLICATE",
                )
                .order_by(
                    RereplicationTaskRow.replicas_at_schedule,
                    RereplicationTaskRow.block_id,
                )
            )
        )

    def in_flight_by_target(self) -> dict[str, int]:
        filas = self._session.execute(
            select(RereplicationTaskRow.target_node_id, func.count())
            .where(
                RereplicationTaskRow.state == "IN_FLIGHT",
                RereplicationTaskRow.target_node_id.is_not(None),
            )
            .group_by(RereplicationTaskRow.target_node_id)
        )
        return {node_id: n for node_id, n in filas}

    def dispatch(
        self,
        task_id: str,
        source_node_id: str,
        target_node_id: str,
        now: datetime,
        expires_at: datetime,
    ) -> None:
        self._session.execute(
            update(RereplicationTaskRow)
            .where(RereplicationTaskRow.id == task_id)
            .values(
                state="IN_FLIGHT",
                source_node_id=source_node_id,
                target_node_id=target_node_id,
                dispatched_at=now,
                expires_at=expires_at,
                attempts=RereplicationTaskRow.attempts + 1,
            )
        )

    def orders_for(
        self, target_node_id: str, now: datetime, resend_after: timedelta
    ) -> list[RereplicationTaskRow]:
        """Ordenes que hay que empujarle a este nodo por su stream de heartbeat.

        `resend_after` evita reenviar la misma orden en cada latido: el nodo late cada
        3 s y copiar un bloque tarda mas que eso.
        """
        limite = now - resend_after
        return list(
            self._session.scalars(
                select(RereplicationTaskRow).where(
                    RereplicationTaskRow.target_node_id == target_node_id,
                    RereplicationTaskRow.state == "IN_FLIGHT",
                    (RereplicationTaskRow.sent_at.is_(None))
                    | (RereplicationTaskRow.sent_at < limite),
                )
            )
        )

    def mark_sent(self, task_ids: Sequence[str], now: datetime) -> None:
        if not task_ids:
            return
        self._session.execute(
            update(RereplicationTaskRow)
            .where(RereplicationTaskRow.id.in_(list(task_ids)))
            .values(sent_at=now)
        )

    def complete(self, block_id: str, target_node_id: str) -> int:
        """Cierra la tarea cuando el destino confirma que ya tiene el bloque.

        La confirmacion llega por el camino de siempre (el aviso de bloque almacenado, o
        el block report), no por un mensaje propio: un camino menos que mantener.
        """
        return self._session.execute(
            update(RereplicationTaskRow)
            .where(
                RereplicationTaskRow.block_id == block_id,
                RereplicationTaskRow.target_node_id == target_node_id,
                RereplicationTaskRow.state == "IN_FLIGHT",
            )
            .values(state="DONE", expires_at=None)
        ).rowcount

    def expire_stale(self, now: datetime, max_attempts: int) -> "ExpiredTasks":
        """Devuelve a la cola las copias que el destino no confirmo a tiempo.

        Mismo mecanismo de expiracion que las reservas de escritura de la Etapa 1 y que
        el lease de liderazgo: quien se cae a mitad no bloquea el recurso para siempre.
        Pasado `max_attempts` la tarea se marca FAILED en vez de reintentarse
        eternamente: un bloque que falla una y otra vez es un problema que hay que mirar,
        no uno que se arregle insistiendo.

        Devuelve tambien los destinos abandonados, para que quien llama pueda quitar la
        fila PENDING de `block_replicas` que quedo apuntando a un nodo que nunca escribio
        el bloque. Sin eso, ese nodo seguiria contando como "ya lo tiene" y el proximo
        despacho lo descartaria como destino para siempre.
        """
        vencidas = list(
            self._session.scalars(
                select(RereplicationTaskRow).where(
                    RereplicationTaskRow.state == "IN_FLIGHT",
                    RereplicationTaskRow.expires_at.is_not(None),
                    RereplicationTaskRow.expires_at < now,
                )
            )
        )
        devueltas = rendidas = 0
        abandonados: list[tuple[str, str]] = []
        for tarea in vencidas:
            if tarea.target_node_id:
                abandonados.append((tarea.block_id, tarea.target_node_id))
            if tarea.attempts >= max_attempts:
                tarea.state = "FAILED"
                tarea.last_error = f"sin confirmar tras {tarea.attempts} intentos"
                rendidas += 1
            else:
                tarea.state = "PENDING"
                tarea.target_node_id = None
                tarea.source_node_id = None
                tarea.sent_at = None
                tarea.expires_at = None
                devueltas += 1
        self._session.flush()
        return ExpiredTasks(
            requeued=devueltas, gave_up=rendidas, abandoned_targets=abandonados
        )

    def counts_by_state(self) -> dict[str, int]:
        filas = self._session.execute(
            select(RereplicationTaskRow.state, func.count()).group_by(
                RereplicationTaskRow.state
            )
        )
        return {estado: n for estado, n in filas}


class SqlLeadershipRepository:
    """El lease de liderazgo. Una fila, y toda la concurrencia del Bloque A pasa por ella.

    `lock()` no es un `get()` con otro nombre: toma un cerrojo de fila. Todo lo que lea
    o escriba el lease pasa por ahi, porque leerlo sin cerrojo y escribir despues deja
    una ventana en la que otra instancia se cuela entre la lectura y la escritura, que es
    exactamente el fallo que este mecanismo existe para evitar.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def lock(self) -> Lease | None:
        """Lee el lease tomando `SELECT ... FOR UPDATE` sobre su fila.

        El cerrojo dura hasta el final de la transaccion, asi que si el llamador hace la
        comprobacion de epoca y la escritura dentro del mismo `with`, ninguna otra
        instancia puede adquirir el lease en medio.

        **SQLite no implementa `FOR UPDATE`** y no se pide: escribe con un cerrojo de
        base entera, asi que el efecto se consigue igual. La consecuencia para las
        pruebas esta dicha en CLAUDE.md: la comparacion de epoca se prueba en SQLite, la
        exclusion mutua real solo contra PostgreSQL.
        """
        consulta = select(LeadershipRow).where(LeadershipRow.id == LEADERSHIP_ROW_ID)
        if self._session.bind is not None and self._session.bind.dialect.name != "sqlite":
            consulta = consulta.with_for_update()

        fila = self._session.execute(consulta).scalar_one_or_none()
        return _to_lease(fila) if fila is not None else None

    def peek(self) -> Lease | None:
        """Lee el lease SIN cerrojo. Solo para exponerlo por la API.

        Separada de `lock()` a proposito: un `GET /cluster/leadership` cada pocos
        segundos tomando cerrojo sobre la fila que el lider renueva cada 2 s convertiria
        una consulta informativa en un punto de contencion.
        """
        fila = self._session.get(LeadershipRow, LEADERSHIP_ROW_ID)
        return _to_lease(fila) if fila is not None else None

    def seed(self) -> Lease:
        """Crea la fila si no esta. En PostgreSQL la siembra la migracion 0002; esto
        cubre el esquema de SQLite de las pruebas, que se crea con `create_all`."""
        fila = LeadershipRow(id=LEADERSHIP_ROW_ID, leader_id=None, epoch=0)
        self._session.add(fila)
        self._session.flush()
        return _to_lease(fila)

    def acquire(
        self, leader_id: str, epoch: int, now: datetime, expires_at: datetime
    ) -> Lease:
        """Toma el lease con una epoca NUEVA. Nunca se reutiliza una epoca."""
        self._session.execute(
            update(LeadershipRow)
            .where(LeadershipRow.id == LEADERSHIP_ROW_ID)
            .values(
                leader_id=leader_id,
                epoch=epoch,
                acquired_at=now,
                renewed_at=now,
                expires_at=expires_at,
            )
        )
        return Lease(
            leader_id=leader_id,
            epoch=epoch,
            acquired_at=now,
            renewed_at=now,
            expires_at=expires_at,
        )

    def renew(
        self, leader_id: str, epoch: int, now: datetime, expires_at: datetime
    ) -> Lease:
        """Extiende el lease conservando la epoca.

        El `where` repite leader_id y epoch aunque la fila ya se leyo bajo cerrojo: es
        barato y convierte un error de programacion (renovar el lease de otro) en cero
        filas afectadas en vez de en una usurpacion silenciosa.
        """
        resultado = self._session.execute(
            update(LeadershipRow)
            .where(
                LeadershipRow.id == LEADERSHIP_ROW_ID,
                LeadershipRow.leader_id == leader_id,
                LeadershipRow.epoch == epoch,
            )
            .values(renewed_at=now, expires_at=expires_at)
        )
        if resultado.rowcount == 0:
            raise RuntimeError(
                "se intento renovar un lease que ya no es de esta instancia"
            )
        fila = self._session.get(LeadershipRow, LEADERSHIP_ROW_ID)
        return _to_lease(fila)

    def release(self, leader_id: str, epoch: int) -> None:
        """Suelta el lease al apagarse limpiamente.

        No hace falta para que el sistema sea correcto (el lease vence solo), pero hace
        que un apagado ordenado no cueste un TTL entero sin lider. La epoca NO se toca:
        el siguiente en tomarlo la subira.
        """
        self._session.execute(
            update(LeadershipRow)
            .where(
                LeadershipRow.id == LEADERSHIP_ROW_ID,
                LeadershipRow.leader_id == leader_id,
                LeadershipRow.epoch == epoch,
            )
            .values(leader_id=None, expires_at=None)
        )


def _to_lease(fila: LeadershipRow) -> Lease:
    return Lease(
        leader_id=fila.leader_id,
        epoch=fila.epoch,
        acquired_at=fila.acquired_at,
        renewed_at=fila.renewed_at,
        expires_at=fila.expires_at,
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
        self.leadership = SqlLeadershipRepository(self._session)
        self.rereplication = SqlRereplicationRepository(self._session)

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
