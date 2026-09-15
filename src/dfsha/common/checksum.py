"""SHA-256 por bloque.

Siempre incremental: ni el cliente ni el DataNode deben cargar un bloque de 64 MB entero
en memoria solo para checksumearlo.
"""

from __future__ import annotations

import hashlib
from typing import BinaryIO, Iterable, Iterator

__all__ = [
    "CHUNK_SIZE",
    "sha256_bytes",
    "sha256_stream",
    "sha256_file",
    "Sha256Accumulator",
    "checksum_matches",
]

#: Tamano de lectura para el hashing incremental. No tiene relacion con el tamano de
#: bloque de DFSha: es solo cuanto se lee de disco o de red por iteracion.
CHUNK_SIZE = 1024 * 1024


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_stream(chunks: Iterable[bytes]) -> str:
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(chunk)
    return digest.hexdigest()


def sha256_file(path: str, chunk_size: int = CHUNK_SIZE) -> str:
    with open(path, "rb") as fh:
        return sha256_stream(iter(lambda: fh.read(chunk_size), b""))


def read_chunks(fh: BinaryIO, total: int, chunk_size: int = CHUNK_SIZE) -> Iterator[bytes]:
    """Lee exactamente `total` bytes de `fh` en trozos de como mucho `chunk_size`."""
    remaining = total
    while remaining > 0:
        chunk = fh.read(min(chunk_size, remaining))
        if not chunk:
            raise EOFError(f"fin de archivo inesperado, faltaban {remaining} bytes")
        remaining -= len(chunk)
        yield chunk


class Sha256Accumulator:
    """Acumula bytes vistos de paso y reporta hash y tamano.

    Lo usa el DataNode para verificar el checksum mientras escribe a disco, sin una
    segunda pasada de lectura.
    """

    def __init__(self) -> None:
        self._digest = hashlib.sha256()
        self._size = 0

    def update(self, chunk: bytes) -> None:
        self._digest.update(chunk)
        self._size += len(chunk)

    @property
    def hexdigest(self) -> str:
        return self._digest.hexdigest()

    @property
    def size(self) -> int:
        return self._size


def checksum_matches(expected: str, actual: str) -> bool:
    """Compara checksums sin distinguir mayusculas ni espacios de sobra."""
    return expected.strip().lower() == actual.strip().lower()
