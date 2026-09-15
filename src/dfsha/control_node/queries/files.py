"""Lado lectura de la transferencia: el plan de descarga."""

from __future__ import annotations

from dataclasses import dataclass

from dfsha.common.errors import NotFoundError
from dfsha.control_node.domain.entities import ReplicaState
from dfsha.control_node.domain.path import Path
from dfsha.control_node.repositories.sql import SqlUnitOfWork
from dfsha.control_node.services.resolver import resolve_entry
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


@query("files.open")
def open_file(uow: SqlUnitOfWork, owner_id: str, raw_path: str) -> ReadPlan:
    """Devuelve donde esta cada bloque y con que checksum verificarlo.

    Los bytes no pasan por aqui: el cliente se los pide directamente a los DataNodes de
    la lista. El ControlNode es routing tier solo para metadatos.
    """
    path = Path.parse(raw_path)

    with uow:
        encontrado = resolve_entry(uow.directories, uow.files, owner_id, path)
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
                # Un archivo COMMITTED no deberia llegar aqui: el commit exige que todos
                # sus bloques esten STORED. Si pasa, el metadato esta corrupto y es mejor
                # decirlo que entregar un archivo incompleto.
                raise NotFoundError(
                    "el archivo tiene bloques sin replica disponible",
                    path=str(path),
                    block_id=bloque.block_id,
                )

            nodos = [
                (r.data_node_id, nodo.base_url)
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
        )
