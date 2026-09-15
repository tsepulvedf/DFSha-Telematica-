#!/usr/bin/env python
"""Recolector de bloques huerfanos. Se corre a mano; no hay demonio ni scheduler.

Un bloque es huerfano cuando pertenece a un archivo DELETED o a una reserva de escritura
que vencio. El borrado en DFSha es logico: `rm` marca el archivo y devuelve el control
enseguida, y los bytes siguen en disco hasta que alguien corre esto.

Por que esto entra en la Etapa 1 y no despues: sin GC, el `used_bytes` que reporta cada
DataNode queda inflado por bloques muertos, y ese numero es justamente la entrada de la
politica de colocacion de la Etapa 2. Empezar la Etapa 2 con metricas de almacenamiento
falsas hace que el balanceo tome malas decisiones de forma dificil de diagnosticar.

El orden importa: primero se borra del disco y solo despues se borra el metadato. Al
reves, un fallo a mitad dejaria bloques en disco que ya nadie sabe que existen, y esos no
los recupera ninguna pasada posterior.

    python scripts/gc.py --dry-run
    python scripts/gc.py
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field

import httpx

INTERNAL_SECRET_HEADER = "X-DFSha-Internal-Secret"


@dataclass
class Resumen:
    bloques_vistos: int = 0
    bloques_borrados: int = 0
    bytes_liberados: int = 0
    fallos: list[str] = field(default_factory=list)

    def imprimir(self, dry_run: bool) -> None:
        titulo = "GC (simulacion)" if dry_run else "GC"
        print(f"\n{titulo}")
        print(f"  bloques huerfanos     {self.bloques_vistos}")
        print(f"  bloques borrados      {self.bloques_borrados}")
        print(f"  bytes liberados       {self.bytes_liberados} ({_humano(self.bytes_liberados)})")
        print(f"  fallos                {len(self.fallos)}")
        for fallo in self.fallos[:20]:
            print(f"    - {fallo}")
        if len(self.fallos) > 20:
            print(f"    ... y {len(self.fallos) - 20} mas")


def _humano(n: int) -> str:
    valor = float(n)
    for unidad in ("B", "KB", "MB", "GB", "TB"):
        if valor < 1024 or unidad == "TB":
            return f"{valor:.1f} {unidad}"
        valor /= 1024
    return f"{n} B"


def recolectar(
    control_url: str, internal_secret: str, dry_run: bool = False, timeout: float = 30.0
) -> Resumen:
    cabeceras = {INTERNAL_SECRET_HEADER: internal_secret}
    control = control_url.rstrip("/")
    resumen = Resumen()

    respuesta = httpx.get(
        f"{control}/internal/v1/gc/orphan-blocks", headers=cabeceras, timeout=timeout
    )
    respuesta.raise_for_status()
    huerfanos = respuesta.json()["blocks"]
    resumen.bloques_vistos = len(huerfanos)

    if not huerfanos:
        return resumen

    confirmables: list[str] = []

    for bloque in huerfanos:
        block_id = bloque["block_id"]
        replicas = bloque["replicas"]

        if dry_run:
            for replica in replicas:
                print(f"  [simulacion] borraria {block_id} de {replica['base_url']}")
            continue

        # El tamano viene en el plan: el ControlNode ya lo tiene en el metadato, asi que
        # no hace falta preguntarselo al DataNode bloque por bloque.
        tamano = bloque.get("size", 0)
        todas_fuera = True

        for replica in replicas:
            base = replica["base_url"].rstrip("/")
            try:
                borrado = httpx.delete(f"{base}/api/v1/blocks/{block_id}", timeout=timeout)
                if borrado.status_code not in (204, 404):
                    todas_fuera = False
                    resumen.fallos.append(
                        f"{block_id} en {base}: HTTP {borrado.status_code}"
                    )
            except httpx.HTTPError as exc:
                todas_fuera = False
                resumen.fallos.append(f"{block_id} en {base}: {type(exc).__name__}")

        if todas_fuera:
            # Solo se confirma lo que ya no esta en ningun disco. Un bloque que sigue en
            # una replica inalcanzable conserva su fila para que la proxima pasada lo
            # vuelva a intentar.
            confirmables.append(block_id)
            resumen.bloques_borrados += 1
            resumen.bytes_liberados += tamano

    if confirmables and not dry_run:
        confirmacion = httpx.post(
            f"{control}/internal/v1/gc/confirm",
            json={"block_ids": confirmables},
            headers=cabeceras,
            timeout=timeout,
        )
        confirmacion.raise_for_status()

    return resumen


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--control-url",
        default=os.environ.get("DFSHA_CONTROL_URL", "http://localhost:8000"),
        help="URL del ControlNode.",
    )
    parser.add_argument(
        "--internal-secret",
        default=os.environ.get("DFSHA_INTERNAL_SECRET"),
        help="Secreto del plano interno (por defecto, DFSHA_INTERNAL_SECRET).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Muestra que se borraria sin tocar nada.",
    )
    args = parser.parse_args(argv)

    if not args.internal_secret:
        print(
            "falta el secreto interno: exporta DFSHA_INTERNAL_SECRET o usa "
            "--internal-secret",
            file=sys.stderr,
        )
        return 2

    try:
        resumen = recolectar(args.control_url, args.internal_secret, args.dry_run)
    except httpx.HTTPStatusError as exc:
        print(
            f"el ControlNode respondio {exc.response.status_code}: {exc.response.text[:200]}",
            file=sys.stderr,
        )
        return 1
    except httpx.HTTPError as exc:
        print(f"no se pudo hablar con el ControlNode: {exc}", file=sys.stderr)
        return 1

    resumen.imprimir(args.dry_run)
    return 1 if resumen.fallos else 0


if __name__ == "__main__":
    sys.exit(main())
