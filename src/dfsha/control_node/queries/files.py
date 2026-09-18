"""Lado lectura de la transferencia: el plan de descarga."""

from __future__ import annotations

from dataclasses import dataclass

from dfsha.common.errors import NotFoundError
from dfsha.control_node.domain.entities import ReplicaState
from dfsha.control_node.domain.path import Path
from dfsha.control_node.repositories.sql import SqlUnitOfWork
from dfsha.control_node.domain.acl import Permission
from dfsha.control_node.services.access import entry_for
from dfsha.control_node.tracing import query

__all__ = ["ReadBlock", "ReadPlan", "open_file"]


@dataclass(frozen=True, slots=True)
class ReadBlock:
    block_id: str
    index: int
    size: int
    checksum_sha256: str
    replicas: list[tuple[str, str]]  # (data_node_id, base_url)


@dataclass(frozen=True, slots=True)
class ReadPlan:
    file_id: str
    size: int
    block_size: int
    blocks: list[ReadBlock]
    #: Envoltura de la clave del archivo. Vacia = sin cifrar.
    wrapped_key: str = ""
    key_algo: str = ""


@query("files.open")
def open_file(uow: SqlUnitOfWork, owner_id: str, raw_path: str) -> ReadPlan:
    """Devuelve donde esta cada bloque y con que checksum verificarlo.

    Los bytes no pasan por aqui: el cliente se los pide directamente a los DataNodes de
    la lista. El ControlNode es routing tier solo para metadatos.
    """
    path = Path.parse(raw_path)

    with uow:
        # Descargar es leer: READ basta, y es lo que permite que alguien con solo
        # lectura sobre un directorio compartido pueda bajarse sus archivos.
        encontrado = entry_for(uow, owner_id, path, Permission.READ)
        if encontrado.is_directory:
            raise NotFoundError("es un directorio, no un archivo", path=str(path))

        archivo = encontrado.file
        if archivo is None or not archivo.is_visible():
            raise NotFoundError("no existe el archivo", path=str(path))

        bloques = uow.blocks.list_for_file(archivo.id)
        replicas = uow.blocks.list_replicas([b.block_id for b in bloques])

        plan: list[ReadBlock] = []
        for bloque in bloques:
            almacenadas = [
                r for r in replicas.get(bloque.block_id, []) if r.state is ReplicaState.STORED
            ]
            if not almacenadas or bloque.checksum_sha256 is None:
                # Con R=1, un nodo caido deja su archivo ilegible. El cliente tiene que
                # saber QUE bloque falta y EN QUE NODO estaba: un timeout generico le
                # haria buscar el problema en su red. Se dice donde estaba la replica
                # aunque ahora este MISSING, porque es justo el dato que permite decidir
                # si hay que levantar ese nodo o darlo por perdido.
                ubicaciones = [
                    f"{r.data_node_id}"
                    + (f" ({nodo.advertise_url}, {r.state.value})" if nodo else "")
                    for r in replicas.get(bloque.block_id, [])
                    if (nodo := uow.data_nodes.get(r.data_node_id)) or True
                ]
                raise NotFoundError(
                    "el archivo no se puede leer: le falta un bloque. "
                    f"Bloque {bloque.index} ({bloque.block_id}) de {len(bloques)}, "
                    + (
                        f"que estaba en: {'; '.join(ubicaciones)}"
                        if ubicaciones
                        else "sin ninguna replica registrada"
                    ),
                    path=str(path),
                    block_id=bloque.block_id,
                    block_index=bloque.index,
                    replicas=ubicaciones,
                )

            nodos = [
                (r.data_node_id, nodo.advertise_url)
                for r in almacenadas
                if (nodo := uow.data_nodes.get(r.data_node_id)) is not None
            ]
            plan.append(
                ReadBlock(
                    block_id=bloque.block_id,
                    index=bloque.index,
                    size=bloque.size,
                    checksum_sha256=bloque.checksum_sha256,
                    replicas=nodos,
                )
            )

        return ReadPlan(
            file_id=archivo.id,
            size=archivo.size,
            block_size=archivo.block_size,
            blocks=plan,
            # El servidor devuelve la envoltura sin poder abrirla. Vacia = sin cifrar.
            wrapped_key=archivo.wrapped_key,
            key_algo=archivo.key_algo,
        )
