"""Demostracion de R=3, quorum W=2 y re-replicacion ante una caida.

    python scripts/demo/replicacion_y_caida.py

Sube un archivo, comprueba que sus bloques estan en tres discos distintos, **mata el nodo
que tiene una de las copias**, y muestra que el archivo se sigue leyendo y que el cluster
repone la copia que falta.

## El control positivo, que es lo que hace honesta la demostracion

«El archivo se sigue leyendo tras matar un nodo» pasa tambien si el nodo que matamos **no
tenia ninguna copia**. Con cuatro nodos y R=3 hay una probabilidad real de elegir
justamente el que no guardaba nada, y entonces la demostracion no demuestra nada: se habria
matado a un espectador.

Por eso este guion **mira primero donde estan las copias y mata a un nodo que SI tiene
una**. Y ademas comprueba que ese nodo esta de verdad inalcanzable, porque un contenedor
que sigue respondiendo tampoco prueba nada.
"""

from __future__ import annotations

import hashlib
import re
import sys

from _comun import GRIS, TRABAJO, Demo, exigir_docker, exigir_sesion

#: Nombres de los contenedores, tal y como los fija docker-compose.yml.
NODOS = [f"dfsha-data-node-{n}" for n in (1, 2, 3, 4)]
REMOTO = "/demo-replicacion.bin"
TAMANO_MB = 8


def copias_por_nodo(demo: Demo) -> dict[str, int]:
    """Cuantos `.blk` tiene cada nodo en su disco. Se cuenta EN EL DISCO, no en el
    metadato: el metadato es lo que el ControlNode cree, y aqui interesa la realidad."""
    conteo: dict[str, int] = {}
    for nodo in NODOS:
        salida = demo.en_nodo(
            nodo, "find /var/lib/dfsha/blocks -name '*.blk' 2>/dev/null | wc -l"
        )
        conteo[nodo] = int(salida.stdout.strip() or 0) if salida.returncode == 0 else -1
    return conteo


def main() -> int:
    demo = Demo(
        "Replicacion R=3, quorum W=2 y recuperacion ante una caida",
        "los bytes salen del cliente UNA vez y la cadena hace el resto",
    )
    exigir_docker(demo)
    exigir_sesion(demo)

    local = TRABAJO / "replicacion.bin"

    # --- 1. Un archivo de varios bloques -----------------------------------
    demo.titulo(f"Se genera un archivo de {TAMANO_MB} MB")
    demo.correr(
        sys.executable, "scripts/gen_testfile.py", str(local), f"{TAMANO_MB}M"
    )
    if not local.is_file():
        demo.mal("no se pudo generar el archivo de prueba")
        return demo.terminar("")
    sha_original = hashlib.sha256(local.read_bytes()).hexdigest()
    demo.nota(f"sha256 {sha_original[:32]}...")

    antes = copias_por_nodo(demo)
    demo.nota(f"bloques en disco antes de subir: {antes}")

    # --- 2. Subir ----------------------------------------------------------
    demo.titulo("Se sube. El cliente manda los bytes a UNA replica; la cadena reenvia")
    subida = demo.dfsha("put", str(local), REMOTO)
    if subida.returncode != 0:
        demo.mal(f"fallo la subida: {subida.stdout}{subida.stderr}")
        return demo.terminar("")
    demo.nota(subida.stdout.strip().splitlines()[-1] if subida.stdout.strip() else "")

    despues = copias_por_nodo(demo)
    nuevas = {n: despues[n] - antes[n] for n in NODOS}
    demo.nota(f"bloques nuevos por nodo: {nuevas}")

    con_copias = [n for n, c in nuevas.items() if c > 0]
    demo.afirmar(
        len(con_copias) >= 3,
        f"los bloques se repartieron en {len(con_copias)} nodos distintos (R=3)",
        f"solo {len(con_copias)} nodos recibieron bloques; se esperaban 3 o mas",
    )

    # --- 3. stat dice lo mismo que el disco --------------------------------
    demo.titulo("El ControlNode coincide con lo que hay en los discos")
    info = demo.dfsha("stat", REMOTO)
    print("    " + "\n    ".join(info.stdout.strip().splitlines()[-6:]))
    demo.afirmar(
        "FULLY_REPLICATED" in info.stdout,
        "el metadato dice FULLY_REPLICATED",
        "el metadato NO dice FULLY_REPLICATED",
    )

    # --- 4. EL CONTROL POSITIVO: elegir una victima que TENGA copias -------
    demo.titulo("Se elige la victima: un nodo que SI tiene copias de este archivo")
    demo.nota(
        "matar un nodo sin copias no demostraria nada: el archivo se leeria igual "
        "porque nunca dependio de el"
    )
    victima = max(con_copias, key=lambda n: nuevas[n])
    demo.ok(f"{victima} guarda {nuevas[victima]} bloques de este archivo")

    # --- 5. Matarlo, y comprobar que esta muerto de verdad -----------------
    demo.titulo(f"Se para {victima}")
    demo.correr("docker", "stop", victima)
    vivo = demo.en_nodo(victima, "true")
    demo.afirmar(
        vivo.returncode != 0,
        "el contenedor ya no responde: la copia que tenia es inalcanzable",
        "el contenedor SIGUE respondiendo; la caida no ocurrio",
    )

    # --- 6. El archivo se sigue leyendo ------------------------------------
    demo.titulo("Se descarga con un nodo menos")
    bajado = TRABAJO / "replicacion-bajado.bin"
    bajado.unlink(missing_ok=True)
    descarga = demo.dfsha("get", REMOTO, str(bajado))
    if descarga.returncode != 0:
        demo.mal(f"la descarga fallo: {descarga.stdout}{descarga.stderr}")
    else:
        sha_bajado = hashlib.sha256(bajado.read_bytes()).hexdigest()
        demo.nota(f"sha256 original {sha_original[:32]}...")
        demo.nota(f"sha256 bajado   {sha_bajado[:32]}...")
        demo.afirmar(
            sha_bajado == sha_original,
            "el archivo se reconstruye identico leyendo de las replicas vivas",
            "el archivo bajado NO coincide con el original",
        )

    # --- 7. El cluster repone la copia -------------------------------------
    demo.titulo("El cluster repone la copia que falta")
    demo.nota(
        "la re-replicacion espera una GRACIA antes de copiar nada, para no gastar "
        "la red por un simple reinicio. Con el default son 5 minutos; para la "
        "demostracion se levanta el stack con DFSHA_REREPLICATION_GRACE_MS=30000"
    )
    estado = demo.dfsha("cluster")
    print("    " + "\n    ".join(estado.stdout.strip().splitlines()[-8:]))

    muertos = len(re.findall(r"\bDEAD\b", estado.stdout))
    demo.afirmar(
        muertos >= 1,
        "el ControlNode ya da el nodo por muerto",
        "el ControlNode todavia no lo marca DEAD (puede tardar unos segundos mas)",
    )

    demo.nota(
        "para ver la copia repuesta, espera la gracia y vuelve a mirar "
        "`dfsha stat` y `dfsha cluster`"
    )

    # --- 8. Devolver el nodo -----------------------------------------------
    demo.titulo(f"Se vuelve a levantar {victima}")
    demo.correr("docker", "start", victima)
    demo.nota(
        "al reincorporarse con su disco intacto, algunos bloques pueden quedar con "
        "4 copias en vez de 3. Es un estado esperable y NO se limpia solo; esta "
        "explicado en CLAUDE.md, 'Sobre-replicacion tras una reincorporacion'"
    )

    demo.dfsha("rm", REMOTO)
    return demo.terminar(
        """
        Tres copias por bloque, repartidas en nodos distintos.
        Perder un nodo no cuesta disponibilidad: el archivo se lee de las otras.
        Y el hueco se repone solo, pasada la espera de gracia.
        """
    )


if __name__ == "__main__":
    sys.exit(main())
