"""API del DataNode: bloques opacos y estado del disco."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Request, Response, status
from fastapi.responses import StreamingResponse

from dfsha.common.dto import HealthResponse
from dfsha.common.errors import ChecksumMismatchError, StorageError
from dfsha.common.logging import get_logger, timed

__all__ = ["blocks_router", "health_router", "CHECKSUM_HEADER"]

CHECKSUM_HEADER = "X-DFSha-Checksum"

blocks_router = APIRouter(prefix="/api/v1", tags=["bloques"])
health_router = APIRouter(tags=["operacion"])


@blocks_router.put("/blocks/{block_id}", status_code=status.HTTP_201_CREATED)
async def put_block(
    block_id: str,
    request: Request,
    x_dfsha_checksum: Annotated[str | None, Header()] = None,
) -> Response:
    """Recibe un bloque en streaming y lo persiste si el checksum cuadra.

    El cuerpo se consume por trozos: un bloque de 64 MB no se carga entero en memoria, ni
    aqui ni en el cliente.
    """
    if not x_dfsha_checksum:
        raise ChecksumMismatchError(f"falta la cabecera {CHECKSUM_HEADER}", block_id=block_id)

    storage = request.app.state.storage
    control = request.app.state.control
    data_node_id = request.app.state.data_node_id
    log = get_logger("data_node")

    # El cuerpo llega como un generador asincrono y el almacenamiento escribe de forma
    # sincrona, asi que se materializa por trozos en una lista de bloques de memoria
    # acotada por el propio tamano de bloque acordado.
    trozos: list[bytes] = []
    async for chunk in request.stream():
        if chunk:
            trozos.append(chunk)

    with timed("block.write", logger=log, block_id=block_id, data_node_id=data_node_id) as t:
        meta = storage.write(block_id, iter(trozos), x_dfsha_checksum)
        t.bind(size_bytes=meta.size)

    # Antes del 201: cuando el cliente vea su bloque subido, el ControlNode ya lo sabra.
    try:
        control.notify_stored(block_id, data_node_id, meta.size, meta.checksum_sha256)
    except Exception as exc:
        # Los bytes estan en disco pero el metadato no lo refleja. Se deshace la escritura
        # para que el cliente pueda reintentar con el mismo block_id: dejarla convertiria
        # el bloque en inmutable e irreintentable, y el commit fallaria para siempre.
        storage.delete(block_id)
        log.error("block.notify_failed", block_id=block_id, error=type(exc).__name__)
        raise StorageError(
            "el bloque se escribio pero el ControlNode no pudo confirmarlo; reintenta",
            block_id=block_id,
        ) from exc

    return Response(
        status_code=status.HTTP_201_CREATED,
        headers={CHECKSUM_HEADER: meta.checksum_sha256},
    )


@blocks_router.get("/blocks/{block_id}")
def get_block(block_id: str, request: Request) -> StreamingResponse:
    storage = request.app.state.storage
    meta = storage.read_meta(block_id)

    return StreamingResponse(
        storage.read(block_id),
        media_type="application/octet-stream",
        headers={
            CHECKSUM_HEADER: meta.checksum_sha256,
            "Content-Length": str(meta.size),
        },
    )


@blocks_router.delete("/blocks/{block_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_block(block_id: str, request: Request) -> Response:
    """Borra un bloque. Idempotente: borrar lo que ya no esta tambien es 204, para que el
    GC pueda reintentar una pasada a medias sin que falle entera."""
    request.app.state.storage.delete(block_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@health_router.get("/health")
def health(request: Request) -> HealthResponse:
    """Estado real del disco.

    En la Etapa 2 este endpoint se convierte en el heartbeat y la politica de colocacion
    depende de estos numeros, asi que se calculan recorriendo el disco, no con contadores
    en memoria.
    """
    stats = request.app.state.storage.stats()
    return HealthResponse(
        status="ok",
        used_bytes=stats.used_bytes,
        capacity_bytes=request.app.state.capacity_bytes,
        block_count=stats.block_count,
        disk_free_bytes=stats.disk_free_bytes,
        data_node_id=request.app.state.data_node_id,
    )
