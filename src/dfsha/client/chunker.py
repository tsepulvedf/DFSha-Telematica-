"""Lectura de un archivo local por bloques, siempre en streaming.

Un bloque son 64 MB por defecto. Nada de esto carga un bloque entero en memoria: se lee
en trozos pequenos tanto para calcular el checksum como para enviarlo. Subir un archivo
de 10 GB tiene que costar lo mismo en memoria que subir uno de 10 MB.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterator

from dfsha.common.checksum import CHUNK_SIZE

__all__ = ["read_block", "checksum_block", "file_size"]


def file_size(path: str | Path) -> int:
    return Path(path).stat().st_size


def read_block(
    path: str | Path, offset: int, size: int, chunk_size: int = CHUNK_SIZE
) -> Iterator[bytes]:
    """Trozos de los `size` bytes que empiezan en `offset`.

    Abre su propio descriptor, asi que varios bloques se pueden leer a la vez desde
    hilos distintos sin pisarse el puntero de lectura.
    """
    restante = size
    with open(path, "rb") as fh:
        fh.seek(offset)
        while restante > 0:
            trozo = fh.read(min(chunk_size, restante))
            if not trozo:
                raise EOFError(
                    f"el archivo se quedo corto: faltaban {restante} bytes desde {offset}"
                )
            restante -= len(trozo)
            yield trozo


def checksum_block(
    path: str | Path, offset: int, size: int, chunk_size: int = CHUNK_SIZE
) -> str:
    """SHA-256 de un bloque del archivo.

    Implica una segunda pasada de lectura sobre el bloque, porque el checksum viaja en
    una cabecera y tiene que estar calculado antes de empezar a enviar el cuerpo. La
    alternativa seria tener el bloque entero en memoria; se prefiere leer dos veces del
    disco local, que es barato, a gastar 64 MB de RAM por bloque en vuelo.
    """
    digest = hashlib.sha256()
    for trozo in read_block(path, offset, size, chunk_size):
        digest.update(trozo)
    return digest.hexdigest()
