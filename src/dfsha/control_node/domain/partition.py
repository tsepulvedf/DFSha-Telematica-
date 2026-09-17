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


@dataclass(frozen=True, slots=True)
class BlockSlice:
    """Un bloque que intersecta un rango pedido, y que trozo de el hace falta.

    `skip` y `take` van en bytes CLAROS y son relativos al principio del bloque
    descifrado. El cliente **descarga el bloque entero** y recorta despues.
    """

    index: int
    #: Bytes a descartar del principio del bloque ya descifrado.
    skip: int
    #: Bytes a quedarse a partir de ahi.
    take: int


def plan_range(
    sizes: list[int], offset: int, length: int, *, overhead: int = 0
) -> list[BlockSlice]:
    """Que bloques hacen falta para leer `length` bytes desde `offset`, y que trozo.

    `sizes` son los tamanos ALMACENADOS por indice, tal y como estan en `blocks.size`;
    `overhead` lo que el cifrado anade a cada uno. La resta se hace aqui para que el
    llamante no tenga que acordarse: quien pide un rango razona en bytes claros, que es
    lo que el usuario ve.

    ## El bloque se descarga ENTERO aunque solo se quiera un byte

    Es la consecuencia directa de cifrar con AES-GCM, y conviene tenerla escrita porque
    parece una ineficiencia gratuita y no lo es: **un bloque cifrado no se puede descifrar
    por partes**. La etiqueta de autenticacion cubre el bloque completo, asi que para
    obtener cualquier byte con garantia de que no fue alterado hay que tener los demas.

    Pedir un rango con `Range:` al DataNode daria los bytes cifrados de ese tramo, que no
    se pueden descifrar ni verificar. La alternativa —cifrar en trozos mas pequenos, con
    su propia etiqueta cada uno— es un diseno distinto: mas etiquetas, mas nonces que no
    repetir, y el limite util lo pondria el tamano de trozo elegido.

    Lo que SI ahorra el rango, y es el 90% del beneficio real: **no descargar los bloques
    que no intersectan**. Leer 1 KB del final de un archivo de 1 GB con bloques de 64 MB
    baja 64 MB en vez de 1 GB.

    Y el ahorro aparece igual sin cifrado: entonces `overhead` es 0 y un cliente que
    quiera puede pedir el tramo exacto al DataNode. Esta funcion no lo impide, solo dice
    que bloques tocar.
    """
    if offset < 0:
        raise ValueError("el desplazamiento no puede ser negativo")
    if length < 0:
        raise ValueError("la longitud no puede ser negativa")

    claros = [s - overhead for s in sizes]
    if any(c < 0 for c in claros):
        raise ValueError("un bloque no puede medir menos que el sobrecoste del cifrado")

    trozos: list[BlockSlice] = []
    if length == 0:
        return trozos

    fin = offset + length
    inicio_bloque = 0
    for index, tamano in enumerate(claros):
        fin_bloque = inicio_bloque + tamano
        # Solapamiento de dos intervalos semiabiertos. Se compara con el fin y no con el
        # indice calculado por division porque el ULTIMO bloque puede ser mas corto: una
        # division supondria que todos miden igual y leeria de mas al final del archivo.
        if fin_bloque > offset and inicio_bloque < fin:
            skip = max(0, offset - inicio_bloque)
            take = min(fin_bloque, fin) - (inicio_bloque + skip)
            if take > 0:
                trozos.append(BlockSlice(index=index, skip=skip, take=take))
        inicio_bloque = fin_bloque
        if inicio_bloque >= fin:
            break

    return trozos
