"""Transferencia de bloques entre el cliente y los DataNodes.

Los bytes van directos: el ControlNode entrega un plan con la URL de cada DataNode y no
vuelve a aparecer hasta el commit. Esa es la parte "hibrida" del routing, y es lo que
hace que anadir DataNodes en la Etapa 2 sume ancho de banda en vez de saturar un nodo
central.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

import httpx

from dfsha.common.checksum import CHUNK_SIZE, Sha256Accumulator, checksum_matches
from dfsha.common.dto import BlockReadPlan, BlockWritePlan
from dfsha.common.errors import ChecksumMismatchError, DFShaError, StorageError
from dfsha.common.logging import get_logger, timed

from .chunker import checksum_block, read_block

__all__ = ["CHECKSUM_HEADER", "upload_blocks", "download_blocks", "TransferError"]

CHECKSUM_HEADER = "X-DFSha-Checksum"


class TransferError(DFShaError):
    code = "transfer_failed"


@dataclass(frozen=True, slots=True)
class _Slot:
    """Un bloque y donde va o de donde viene, con su posicion en el archivo local."""

    block_id: str
    index: int
    size: int
    offset: int
    base_url: str
    data_node_id: str
    checksum: str | None = None


def _slots_de_escritura(blocks: Sequence[BlockWritePlan]) -> list[_Slot]:
    slots: list[_Slot] = []
    offset = 0
    for bloque in sorted(blocks, key=lambda b: b.index):
        if not bloque.replicas:
            raise TransferError(
                "el plan no trae ningun DataNode para el bloque", block_id=bloque.block_id
            )
        # Etapa 1: una replica por bloque. Cuando sean varias, aqui se abre el pipeline.
        replica = bloque.replicas[0]
        slots.append(
            _Slot(
                block_id=bloque.block_id,
                index=bloque.index,
                size=bloque.size,
                offset=offset,
                base_url=replica.base_url,
                data_node_id=replica.data_node_id,
            )
        )
        offset += bloque.size
    return slots


def upload_blocks(
    local_path: str | Path,
    blocks: Sequence[BlockWritePlan],
    parallel: int = 4,
    timeout: float = 300.0,
    on_block: Callable[[int], None] | None = None,
) -> int:
    """Sube todos los bloques y devuelve los bytes enviados.

    Cada bloque viaja a su DataNode con el checksum en una cabecera; el DataNode lo
    verifica y rechaza con 422 si no cuadra, de modo que una corrupcion en transito nunca
    llega a confirmarse como un archivo bueno.
    """
    slots = _slots_de_escritura(blocks)
    if not slots:
        return 0

    log = get_logger("client")
    enviados = 0

    def subir(slot: _Slot) -> int:
        checksum = checksum_block(local_path, slot.offset, slot.size)
        with timed(
            "block.upload",
            logger=log,
            block_id=slot.block_id,
            size_bytes=slot.size,
            data_node_id=slot.data_node_id,
        ):
            respuesta = httpx.put(
                f"{slot.base_url.rstrip('/')}/api/v1/blocks/{slot.block_id}",
                content=read_block(local_path, slot.offset, slot.size),
                headers={
                    CHECKSUM_HEADER: checksum,
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(slot.size),
                },
                timeout=timeout,
            )
        if respuesta.status_code != 201:
            raise TransferError(
                "el DataNode rechazo el bloque",
                block_id=slot.block_id,
                status=respuesta.status_code,
                detail=respuesta.text[:200],
            )
        return slot.size

    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        futuros = {pool.submit(subir, slot): slot for slot in slots}
        for futuro in as_completed(futuros):
            enviados += futuro.result()
            if on_block:
                on_block(futuros[futuro].size)

    return enviados


def download_blocks(
    destination: str | Path,
    blocks: Sequence[BlockReadPlan],
    parallel: int = 4,
    timeout: float = 300.0,
    on_block: Callable[[int], None] | None = None,
    chunk_size: int = CHUNK_SIZE,
) -> int:
    """Descarga los bloques y reconstruye el archivo.

    Los bloques se piden en paralelo pero se escriben cada uno en su offset, asi que el
    archivo queda en orden de `index` sin necesidad de juntarlos al final. Cada bloque se
    verifica contra el checksum del metadato antes de darlo por bueno: si un DataNode
    devolviera bytes corruptos, el `get` falla en vez de escribir basura.
    """
    ordenados = sorted(blocks, key=lambda b: b.index)
    destino = Path(destination)
    destino.parent.mkdir(parents=True, exist_ok=True)

    total = sum(b.size for b in ordenados)
    # Se reserva el archivo completo por adelantado para poder escribir cada bloque en su
    # sitio desde varios hilos.
    with open(destino, "wb") as fh:
        fh.truncate(total)

    if not ordenados:
        return 0

    log = get_logger("client")
    offsets: dict[str, int] = {}
    acumulado = 0
    for bloque in ordenados:
        offsets[bloque.block_id] = acumulado
        acumulado += bloque.size

    def bajar(bloque: BlockReadPlan) -> int:
        if not bloque.replicas:
            raise TransferError(
                "el plan no trae ninguna replica para el bloque", block_id=bloque.block_id
            )
        replica = bloque.replicas[0]
        offset = offsets[bloque.block_id]
        acumulador = Sha256Accumulator()

        with timed(
            "block.download",
            logger=log,
            block_id=bloque.block_id,
            size_bytes=bloque.size,
            data_node_id=replica.data_node_id,
        ):
            with httpx.stream(
                "GET",
                f"{replica.base_url.rstrip('/')}/api/v1/blocks/{bloque.block_id}",
                timeout=timeout,
            ) as respuesta:
                if respuesta.status_code != 200:
                    respuesta.read()
                    raise TransferError(
                        "el DataNode no devolvio el bloque",
                        block_id=bloque.block_id,
                        status=respuesta.status_code,
                    )
                with open(destino, "r+b") as fh:
                    fh.seek(offset)
                    for trozo in respuesta.iter_bytes(chunk_size):
                        acumulador.update(trozo)
                        fh.write(trozo)

        if not checksum_matches(bloque.checksum_sha256, acumulador.hexdigest):
            raise ChecksumMismatchError(
                "el bloque descargado no coincide con su checksum",
                block_id=bloque.block_id,
                expected=bloque.checksum_sha256,
                actual=acumulador.hexdigest,
            )
        if acumulador.size != bloque.size:
            raise StorageError(
                "el bloque descargado no tiene el tamano esperado",
                block_id=bloque.block_id,
                expected=bloque.size,
                actual=acumulador.size,
            )
        return acumulador.size

    recibidos = 0
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        futuros = {pool.submit(bajar, bloque): bloque for bloque in ordenados}
        for futuro in as_completed(futuros):
            recibidos += futuro.result()
            if on_block:
                on_block(futuros[futuro].size)

    return recibidos


def iter_sizes(blocks: Iterable) -> int:
    return sum(b.size for b in blocks)
