#!/usr/bin/env python
"""Genera archivos de prueba de tamano arbitrario.

Existe para no versionar binarios en un repositorio publico. El contenido es
pseudoaleatorio pero reproducible: con la misma semilla y el mismo tamano sale
exactamente el mismo archivo, asi que dos personas pueden comparar un SHA-256 sin
mandarse el archivo.

Se genera en trozos: crear un archivo de 50 GB no puede necesitar 50 GB de RAM.

    python scripts/gen_testfile.py datos.bin --size 50MB
    python scripts/gen_testfile.py datos.bin --size 1GB --seed 7
"""

from __future__ import annotations

import argparse
import hashlib
import random
import sys
from pathlib import Path

CHUNK = 1024 * 1024

MULTIPLICADORES = {
    "": 1,
    "B": 1,
    "K": 1024,
    "KB": 1024,
    "M": 1024**2,
    "MB": 1024**2,
    "G": 1024**3,
    "GB": 1024**3,
}


def parse_size(texto: str) -> int:
    """Acepta 1048576, 50MB, 1.5GB..."""
    limpio = texto.strip().upper().replace(" ", "")
    numero = limpio
    sufijo = ""
    for posible in ("KB", "MB", "GB", "B", "K", "M", "G"):
        if limpio.endswith(posible):
            numero, sufijo = limpio[: -len(posible)], posible
            break
    try:
        valor = float(numero)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"tamano invalido: {texto}") from exc
    if valor < 0:
        raise argparse.ArgumentTypeError("el tamano no puede ser negativo")
    return int(valor * MULTIPLICADORES[sufijo])


def generar(destino: Path, size: int, seed: int) -> str:
    rng = random.Random(seed)
    digest = hashlib.sha256()
    destino.parent.mkdir(parents=True, exist_ok=True)

    with open(destino, "wb") as fh:
        restante = size
        while restante > 0:
            trozo = rng.randbytes(min(CHUNK, restante))
            digest.update(trozo)
            fh.write(trozo)
            restante -= len(trozo)

    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("destino", type=Path, help="Archivo a crear.")
    parser.add_argument(
        "--size", type=parse_size, default="1MB", help="Tamano: 1048576, 50MB, 1GB..."
    )
    parser.add_argument("--seed", type=int, default=1234, help="Semilla reproducible.")
    args = parser.parse_args(argv)

    sha = generar(args.destino, args.size, args.seed)
    print(f"{args.destino}  {args.size} bytes  sha256={sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
