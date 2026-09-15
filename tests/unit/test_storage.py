"""Almacenamiento del DataNode: inmutabilidad, verificacion, atomicidad y estadisticas."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dfsha.common.checksum import sha256_bytes
from dfsha.common.errors import (
    BlockAlreadyExistsError,
    BlockNotFoundError,
    ChecksumMismatchError,
    StorageError,
)
from dfsha.data_node.storage import BlockStorage

BLOQUE = "ab12cd34-0000-0000-0000-000000000001"
DATOS = b"contenido del bloque" * 100


@pytest.fixture()
def storage(tmp_path: Path) -> BlockStorage:
    return BlockStorage(tmp_path)


def escribir(storage: BlockStorage, block_id: str = BLOQUE, datos: bytes = DATOS):
    return storage.write(block_id, iter([datos]), sha256_bytes(datos))


class TestEscritura:
    def test_escribe_y_lee(self, storage: BlockStorage) -> None:
        meta = escribir(storage)
        assert meta.size == len(DATOS)
        assert meta.checksum_sha256 == sha256_bytes(DATOS)
        assert b"".join(storage.read(BLOQUE)) == DATOS

    def test_sharding_por_los_dos_primeros_caracteres(self, storage: BlockStorage) -> None:
        escribir(storage)
        assert storage.block_path(BLOQUE).parent.name == "ab"
        assert storage.block_path(BLOQUE).exists()
        assert storage.meta_path(BLOQUE).exists()

    def test_el_meta_guarda_tamano_checksum_y_fecha(self, storage: BlockStorage) -> None:
        escribir(storage)
        datos = json.loads(storage.meta_path(BLOQUE).read_text(encoding="utf-8"))
        assert datos["size"] == len(DATOS)
        assert datos["checksum_sha256"] == sha256_bytes(DATOS)
        assert datos["created_at"]

    def test_los_bloques_son_inmutables(self, storage: BlockStorage) -> None:
        escribir(storage)
        with pytest.raises(BlockAlreadyExistsError):
            escribir(storage, datos=b"otra cosa")
        # El contenido original sigue intacto.
        assert b"".join(storage.read(BLOQUE)) == DATOS

    def test_checksum_incorrecto_no_deja_nada_en_disco(self, storage: BlockStorage) -> None:
        with pytest.raises(ChecksumMismatchError):
            storage.write(BLOQUE, iter([DATOS]), "0" * 64)

        assert not storage.block_path(BLOQUE).exists()
        assert list(storage.blocks_dir.rglob("*.tmp")) == []

    def test_un_fallo_a_media_escritura_no_deja_temporales(self, storage: BlockStorage) -> None:
        def chunks_que_fallan():
            yield b"parte buena"
            raise OSError("se cayo la red")

        with pytest.raises(OSError):
            storage.write(BLOQUE, chunks_que_fallan(), sha256_bytes(DATOS))

        assert not storage.block_path(BLOQUE).exists()
        assert list(storage.blocks_dir.rglob("*.tmp")) == []

    def test_escritura_en_varios_trozos(self, storage: BlockStorage) -> None:
        partes = [b"uno", b"dos", b"tres"]
        meta = storage.write(BLOQUE, iter(partes), sha256_bytes(b"".join(partes)))
        assert meta.size == len(b"".join(partes))

    def test_bloque_vacio(self, storage: BlockStorage) -> None:
        meta = storage.write(BLOQUE, iter([]), sha256_bytes(b""))
        assert meta.size == 0
        assert b"".join(storage.read(BLOQUE)) == b""

    @pytest.mark.parametrize("block_id", ["../fuera", "a/b", "..", "", "a\\b"])
    def test_block_id_que_intenta_escapar_del_directorio(
        self, storage: BlockStorage, block_id: str
    ) -> None:
        # El block_id llega por la URL y acaba en una ruta de disco.
        with pytest.raises(StorageError):
            storage.block_path(block_id)


class TestLectura:
    def test_bloque_inexistente(self, storage: BlockStorage) -> None:
        with pytest.raises(BlockNotFoundError):
            list(storage.read(BLOQUE))
        with pytest.raises(BlockNotFoundError):
            storage.read_meta(BLOQUE)

    def test_lectura_por_trozos(self, storage: BlockStorage) -> None:
        escribir(storage)
        trozos = list(storage.read(BLOQUE, chunk_size=64))
        assert len(trozos) > 1
        assert b"".join(trozos) == DATOS


class TestBorrado:
    def test_borra_bloque_y_meta(self, storage: BlockStorage) -> None:
        escribir(storage)
        assert storage.delete(BLOQUE) is True
        assert not storage.block_path(BLOQUE).exists()
        assert not storage.meta_path(BLOQUE).exists()

    def test_borrar_dos_veces_no_falla(self, storage: BlockStorage) -> None:
        escribir(storage)
        assert storage.delete(BLOQUE) is True
        assert storage.delete(BLOQUE) is False

    def test_el_block_id_se_puede_reutilizar_tras_borrarlo(self, storage: BlockStorage) -> None:
        escribir(storage)
        storage.delete(BLOQUE)
        escribir(storage, datos=b"contenido nuevo")
        assert b"".join(storage.read(BLOQUE)) == b"contenido nuevo"


class TestEstadisticas:
    def test_disco_vacio(self, storage: BlockStorage) -> None:
        stats = storage.stats()
        assert (stats.used_bytes, stats.block_count) == (0, 0)
        assert stats.disk_free_bytes > 0

    def test_cuenta_solo_los_blk(self, storage: BlockStorage) -> None:
        # Los .meta no suman: used_bytes es lo que ocupan los datos, que es lo que la
        # politica de colocacion de la Etapa 2 compara contra capacity_bytes.
        escribir(storage)
        stats = storage.stats()
        assert stats.used_bytes == len(DATOS)
        assert stats.block_count == 1

    def test_refleja_el_borrado(self, storage: BlockStorage) -> None:
        escribir(storage)
        antes = storage.stats()
        storage.delete(BLOQUE)
        despues = storage.stats()
        assert antes.used_bytes == len(DATOS)
        assert (despues.used_bytes, despues.block_count) == (0, 0)

    def test_se_calcula_del_disco_no_de_memoria(self, tmp_path: Path) -> None:
        # Un DataNode recien arrancado tiene que ver los bloques que ya estaban ahi.
        primero = BlockStorage(tmp_path)
        escribir(primero)

        segundo = BlockStorage(tmp_path)
        assert segundo.stats().block_count == 1
        assert segundo.stats().used_bytes == len(DATOS)

    def test_disk_free_no_es_capacity_menos_used(self, storage: BlockStorage) -> None:
        import shutil

        escribir(storage)
        stats = storage.stats()
        # Sale de shutil.disk_usage, no de una resta: si se llenara el disco por fuera,
        # la resta mentiria y la Etapa 2 mandaria bloques a un nodo sin sitio.
        assert abs(stats.disk_free_bytes - shutil.disk_usage(storage.blocks_dir).free) < 100 * 1024 * 1024
