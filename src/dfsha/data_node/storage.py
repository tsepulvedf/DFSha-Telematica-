"""Layout en disco del DataNode.

El DataNode no sabe nada de rutas logicas, de usuarios ni de archivos: solo guarda
bloques opacos indexados por `block_id`. El mapeo ruta -> [block_id] vive unicamente en
el ControlNode, y de ahi sale que `mv` y `rename` sean metadato puro.

    $DFSHA_DATA_DIR/blocks/<2 primeros chars del block_id>/<block_id>.blk
    $DFSHA_DATA_DIR/blocks/<2 primeros chars del block_id>/<block_id>.meta

El sharding por los dos primeros caracteres reparte los bloques en 256 directorios (los
ids son UUID hexadecimales), para no acabar con un unico directorio de cientos de miles
de entradas, donde cada listado se vuelve lento.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from dfsha.common.checksum import CHUNK_SIZE, Sha256Accumulator, checksum_matches
from dfsha.common.errors import (
    BlockAlreadyExistsError,
    BlockNotFoundError,
    ChecksumMismatchError,
    StorageError,
)

__all__ = ["BlockMeta", "StorageStats", "BlockStorage"]

BLOCK_SUFFIX = ".blk"
META_SUFFIX = ".meta"
TMP_SUFFIX = ".tmp"
SHARD_LEN = 2


@dataclass(frozen=True, slots=True)
class BlockMeta:
    size: int
    checksum_sha256: str
    created_at: str


@dataclass(frozen=True, slots=True)
class StorageStats:
    used_bytes: int
    block_count: int
    disk_free_bytes: int


class BlockStorage:
    def __init__(self, data_dir: str | Path) -> None:
        self.root = Path(data_dir)
        self.blocks_dir = self.root / "blocks"
        self.blocks_dir.mkdir(parents=True, exist_ok=True)

    # --- Rutas -------------------------------------------------------------

    def _shard(self, block_id: str) -> Path:
        if not block_id or "/" in block_id or "\\" in block_id or ".." in block_id:
            # El block_id llega por la URL y acaba en una ruta de disco: sin esto, un
            # id con '..' escribiria fuera del directorio de datos.
            raise StorageError("block_id invalido", block_id=block_id)
        return self.blocks_dir / block_id[:SHARD_LEN]

    def block_path(self, block_id: str) -> Path:
        return self._shard(block_id) / f"{block_id}{BLOCK_SUFFIX}"

    def meta_path(self, block_id: str) -> Path:
        return self._shard(block_id) / f"{block_id}{META_SUFFIX}"

    def exists(self, block_id: str) -> bool:
        return self.block_path(block_id).exists()

    # --- Escritura ---------------------------------------------------------

    def write(self, block_id: str, chunks: Iterator[bytes], expected_checksum: str) -> BlockMeta:
        """Escribe un bloque y devuelve su metadato.

        Tres propiedades que no son negociables:

        - Inmutable: si el bloque ya existe, no se toca. Un `block_id` repetido es un
          error del llamador, no una orden de sobrescribir.
        - Verificado: el SHA-256 se calcula sobre los bytes que van llegando. Si no
          coincide con el anunciado, no queda nada en disco.
        - Atomico: se escribe a `.tmp` y se renombra con `os.replace`. Nadie puede leer
          un bloque a medio escribir, ni siquiera si el proceso muere en mitad de la
          transferencia.
        """
        if self.exists(block_id):
            raise BlockAlreadyExistsError(
                "el bloque ya existe y los bloques son inmutables", block_id=block_id
            )

        destino = self.block_path(block_id)
        destino.parent.mkdir(parents=True, exist_ok=True)
        temporal = destino.with_suffix(BLOCK_SUFFIX + TMP_SUFFIX)

        acumulador = Sha256Accumulator()
        try:
            with open(temporal, "wb") as fh:
                for chunk in chunks:
                    acumulador.update(chunk)
                    fh.write(chunk)
                fh.flush()
                os.fsync(fh.fileno())

            if not checksum_matches(expected_checksum, acumulador.hexdigest):
                raise ChecksumMismatchError(
                    "el checksum no coincide con los bytes recibidos",
                    block_id=block_id,
                    expected=expected_checksum,
                    actual=acumulador.hexdigest,
                )

            meta = BlockMeta(
                size=acumulador.size,
                checksum_sha256=acumulador.hexdigest,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            # El .meta primero: un .blk sin .meta seria un bloque sin checksum con el que
            # verificarlo. Al reves solo deja un .meta suelto, que el GC recoge.
            self._write_meta(block_id, meta)
            os.replace(temporal, destino)
            return meta
        except Exception:
            temporal.unlink(missing_ok=True)
            raise

    def _write_meta(self, block_id: str, meta: BlockMeta) -> None:
        destino = self.meta_path(block_id)
        temporal = destino.with_suffix(META_SUFFIX + TMP_SUFFIX)
        temporal.write_text(
            json.dumps(
                {
                    "size": meta.size,
                    "checksum_sha256": meta.checksum_sha256,
                    "created_at": meta.created_at,
                }
            ),
            encoding="utf-8",
        )
        os.replace(temporal, destino)

    # --- Lectura -----------------------------------------------------------

    def read_meta(self, block_id: str) -> BlockMeta:
        ruta = self.meta_path(block_id)
        if not ruta.exists():
            raise BlockNotFoundError("no existe el bloque", block_id=block_id)
        datos = json.loads(ruta.read_text(encoding="utf-8"))
        return BlockMeta(
            size=datos["size"],
            checksum_sha256=datos["checksum_sha256"],
            created_at=datos["created_at"],
        )

    def read(self, block_id: str, chunk_size: int = CHUNK_SIZE) -> Iterator[bytes]:
        ruta = self.block_path(block_id)
        if not ruta.exists():
            raise BlockNotFoundError("no existe el bloque", block_id=block_id)

        def generador() -> Iterator[bytes]:
            with open(ruta, "rb") as fh:
                while chunk := fh.read(chunk_size):
                    yield chunk

        return generador()

    # --- Borrado -----------------------------------------------------------

    def delete(self, block_id: str) -> bool:
        """Borra bloque y metadato. Idempotente: devuelve False si no habia nada.

        Que sea idempotente importa porque el GC puede reintentar: un bloque ya borrado
        no puede hacer fallar la pasada entera.
        """
        blk = self.block_path(block_id)
        meta = self.meta_path(block_id)
        existia = blk.exists() or meta.exists()
        blk.unlink(missing_ok=True)
        meta.unlink(missing_ok=True)
        return existia

    # --- Estado ------------------------------------------------------------

    def list_block_ids(self) -> list[str]:
        """Todos los bloques que hay en disco ahora mismo.

        Es la entrada del block report completo, y por tanto lo unico que puede detectar
        un `.blk` borrado por fuera del sistema: los reports incrementales solo cuentan
        lo que paso por la API.
        """
        ids: list[str] = []
        for shard in self.blocks_dir.iterdir():
            if not shard.is_dir():
                continue
            ids.extend(
                entrada.stem
                for entrada in shard.iterdir()
                if entrada.suffix == BLOCK_SUFFIX and entrada.is_file()
            )
        return sorted(ids)

    def stats(self) -> StorageStats:
        """Recorre el disco de verdad, sin contadores en memoria.

        En la Etapa 2 estos numeros son el heartbeat del que depende la politica de
        colocacion, asi que tienen que ser fiables tras un reinicio, tras una pasada del
        GC y tras un borrado hecho a mano. Un contador en memoria no sobrevive a
        ninguna de las tres.

        `disk_free_bytes` sale de `shutil.disk_usage`, no de `capacity - used`: si el
        disco se llena por los logs, la base de datos u otro contenedor, la resta miente
        y la Etapa 2 mandaria bloques a un nodo que no puede recibirlos.
        """
        used = 0
        count = 0
        for shard in self.blocks_dir.iterdir():
            if not shard.is_dir():
                continue
            for entrada in shard.iterdir():
                if entrada.suffix == BLOCK_SUFFIX and entrada.is_file():
                    used += entrada.stat().st_size
                    count += 1

        return StorageStats(
            used_bytes=used,
            block_count=count,
            disk_free_bytes=shutil.disk_usage(self.blocks_dir).free,
        )
