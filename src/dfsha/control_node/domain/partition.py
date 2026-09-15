"""Particion de un archivo en bloques de tamano fijo.

Funcion pura y sin dependencias: dado `size` y `block_size`, cuantos bloques hay y cuanto
mide cada uno. Vive en el dominio porque el cliente y el ControlNode tienen que llegar
exactamente al mismo reparto; si discreparan en un byte, el commit fallaria con un
checksum que nadie sabria explicar.
"""

from __future__ import annotations

from dataclasses import dataclass

from dfsha.common.errors import DFShaError

__all__ = ["BlockSpec", "plan_blocks", "block_count_for", "InvalidBlockSizeError"]


class InvalidBlockSizeError(DFShaError):
    code = "invalid_block_size"


@dataclass(frozen=True, slots=True)
class BlockSpec:
    """Un hueco de bloque: su posicion en el archivo y cuanto ocupa."""

    index: int
    size: int
    offset: int

    @property
    def end(self) -> int:
        return self.offset + self.size


def _validate(size: int, block_size: int) -> None:
    if size < 0:
        raise InvalidBlockSizeError("el tamano del archivo no puede ser negativo", size=size)
    if block_size <= 0:
        raise InvalidBlockSizeError(
            "el tamano de bloque debe ser positivo", block_size=block_size
        )


def block_count_for(size: int, block_size: int) -> int:
    """Numero de bloques de un archivo de `size` bytes.

    Un archivo vacio tiene cero bloques, no un bloque vacio: no hay nada que almacenar y
    un bloque de cero bytes solo seria basura que el GC tendria que limpiar.
    """
    _validate(size, block_size)
    return (size + block_size - 1) // block_size


def plan_blocks(size: int, block_size: int) -> list[BlockSpec]:
    """Reparto completo del archivo.

    Todos los bloques miden `block_size` salvo el ultimo, que lleva el resto. Cuando
    `size` es multiplo exacto, el ultimo tambien va lleno y no sobra un bloque vacio.
    """
    _validate(size, block_size)

    specs: list[BlockSpec] = []
    offset = 0
    index = 0
    while offset < size:
        current = min(block_size, size - offset)
        specs.append(BlockSpec(index=index, size=current, offset=offset))
        offset += current
        index += 1
    return specs
