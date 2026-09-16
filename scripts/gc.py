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
from pathlib import Path

import httpx

INTERNAL_SECRET_HEADER = "X-DFSha-Internal-Secret"


def leer_env(nombre: str) -> str | None:
    """Busca una variable en el entorno y, si no esta, en el .env de la raiz.

    El .env ya existe porque docker compose lo necesita, y sin esto el GC pedia que el
    secreto se exportara ademas a mano en la shell. Es el mismo fichero y el mismo valor:
    hacer que el script lo lea evita un paso que es facil de olvidar y cuyo unico sintoma
    es un "falta el secreto interno" que no dice de donde sacarlo.

    Parseo deliberadamente simple: KEY=VALOR, sin sustituciones ni comillas multilinea.
    Es lo que .env.example contiene.
    """
    valor = os.environ.get(nombre)
    if valor:
        return valor

    env = Path(__file__).resolve().parent.parent / ".env"
    if not env.exists():
        return None

    try:
        for linea in env.read_text(encoding="utf-8").splitlines():
            linea = linea.strip()
            if not linea or linea.startswith("#") or "=" not in linea:
                continue
            clave, _, bruto = linea.partition("=")
            if clave.strip() == nombre:
                return bruto.strip().strip("\"'") or None
    except OSError:
        return None
    return None


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


def _por_el_canal_de_control(
    control_url: str, internal_secret: str, dry_run: bool, timeout: float = 30.0
) -> int:
    """Encola los borrados en el ControlNode y deja que viajen por el heartbeat.

    La diferencia con el camino normal no es de eficiencia: es que por aqui **no hace
    falta tener ruta hasta los DataNodes**, solo hasta el ControlNode. En AWS los nodos
    anuncian su IP privada, asi que recolectar desde fuera de la VPC solo es posible por
    esta via.

    A cambio, el borrado es asincrono: se encola y se cumple cuando cada nodo recibe su
    orden en el siguiente latido. Por eso aqui NO se llama a `/gc/confirm`: las filas del
    metadato se quitan en una pasada posterior, cuando conste que el bloque ya no esta en
    ningun disco. El ControlNode no borra metadato sobre una promesa.
    """
    cabeceras = {INTERNAL_SECRET_HEADER: internal_secret}
    control = control_url.rstrip("/")

    if dry_run:
        respuesta = httpx.get(
            f"{control}/internal/v1/gc/orphan-blocks", headers=cabeceras, timeout=timeout
        )
        respuesta.raise_for_status()
        huerfanos = respuesta.json()["blocks"]
        print(f"[simulacion] encolaria el borrado de {len(huerfanos)} bloques huerfanos")
        for bloque in huerfanos:
            destinos = ", ".join(r["data_node_id"][:8] for r in bloque["replicas"])
            print(f"  [simulacion] {bloque['block_id']} -> {destinos}")
        return 0

    respuesta = httpx.post(
        f"{control}/internal/v1/gc/dispatch", headers=cabeceras, timeout=timeout
    )
    respuesta.raise_for_status()
    datos = respuesta.json()

    print(
        f"encoladas {datos['orders']} ordenes de borrado para {datos['blocks']} bloques "
        f"huerfanos ({datos['skipped']} ya tenian una orden en curso)."
    )
    print(
        "Las ordenes viajan en el proximo latido de cada DataNode. Vuelve a correr el "
        "GC sin --via-control-plane mas tarde para confirmar y limpiar el metadato."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--control-url",
        default=leer_env("DFSHA_CONTROL_URL") or "http://localhost:8000",
        help="URL del ControlNode (por defecto DFSHA_CONTROL_URL, o el .env de la raiz).",
    )
    parser.add_argument(
        "--internal-secret",
        default=leer_env("DFSHA_INTERNAL_SECRET"),
        help=(
            "Secreto del plano interno. Por defecto DFSHA_INTERNAL_SECRET del entorno "
            "y, si no esta, del .env de la raiz del repositorio."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Muestra que se borraria sin tocar nada.",
    )
    parser.add_argument(
        "--via-control-plane",
        action="store_true",
        help=(
            "Encola los borrados por el canal de control en vez de llamar a cada "
            "DataNode por REST. Util cuando quien recolecta no tiene ruta hasta los "
            "DataNodes, que es el caso en AWS: ahi anuncian su IP privada."
        ),
    )
    args = parser.parse_args(argv)

    if not args.internal_secret:
        raiz = Path(__file__).resolve().parent.parent
        print(
            "falta el secreto interno. El GC lo busca, en este orden:\n"
            "  1. la variable de entorno DFSHA_INTERNAL_SECRET\n"
            f"  2. DFSHA_INTERNAL_SECRET en {raiz / '.env'}\n"
            "  3. la opcion --internal-secret\n"
            "Tiene que ser el MISMO valor con el que arrancaron los servicios.",
            file=sys.stderr,
        )
        return 2

    if args.via_control_plane:
        return _por_el_canal_de_control(
            args.control_url, args.internal_secret, args.dry_run
        )

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
