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

## Los tiempos, que no son instantaneos

Un nodo parado no pasa a DEAD al momento: primero SUSPECT (10 s sin latir) y luego DEAD
(30 s). Y la re-replicacion espera ademas una **gracia** desde que entra en DEAD, para no
copiar un disco entero por un simple reinicio: 5 minutos por defecto. Para grabar, levanta
el stack con `DFSHA_REREPLICATION_GRACE_MS=30000`. El guion **espera** a cada una de esas
transiciones en vez de comprobarlas en un instante fijo.
"""

from __future__ import annotations

import argparse
import hashlib
import sys

from _comun import (
    DATANODES,
    DIR_BLOQUES,
    TRABAJO,
    Demo,
    esperar,
    esperar_cluster,
    mostrar_cluster,
    exigir_docker,
    exigir_sesion,
    sesion_del_cli,
)
from gen_testfile import generar

REMOTO = "/demo-replicacion.bin"
TAMANO_MB = 8
#: Bloques de 1 MB para que el archivo tenga VARIOS y se repartan: con el default de
#: 64 MB, 8 MB serian un unico bloque.
BLOQUE = 1024 * 1024


def bloques_por_nodo(demo: Demo, block_ids: list[str]) -> dict[str, int]:
    """Cuantos bloques DE ESTE ARCHIVO tiene cada nodo en su disco.

    Se cuenta EN EL DISCO, no en el metadato: el metadato es lo que el ControlNode cree, y
    aqui interesa la realidad. Y se cuentan los de este archivo, no todos los `.blk`: una
    subida concurrente o un resto de otra pasada no deben mover la cuenta.
    """
    rutas = " ".join(f"{DIR_BLOQUES}/{b[:2]}/{b}.blk" for b in block_ids)
    orden = f'n=0; for f in {rutas}; do [ -f "$f" ] && n=$((n+1)); done; echo $n'
    conteo: dict[str, int] = {}
    for nodo in DATANODES:
        salida = demo.en_nodo(nodo, orden)
        conteo[nodo] = int(salida.stdout.strip() or 0) if salida.returncode == 0 else -1
    return conteo


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--espera-reposicion",
        type=float,
        default=120.0,
        help="Segundos a esperar la copia repuesta. Con la gracia por defecto (5 min), "
        "pasa 400 o levanta el stack con DFSHA_REREPLICATION_GRACE_MS=30000.",
    )
    args = parser.parse_args(argv)

    from dfsha.client.api import ControlApi

    demo = Demo(
        "Replicacion R=3, quorum W=2 y recuperacion ante una caida",
        "los bytes salen del cliente UNA vez y la cadena hace el resto",
    )
    exigir_docker(demo)
    exigir_sesion(demo)
    api = ControlApi(sesion_del_cli())
    # Todos vivos, no solo R: matar uno tiene que dejar R para que haya donde reponer.
    inicial = esperar_cluster(demo, api, minimo=len(DATANODES))
    r = inicial.replication_factor
    vivos_al_empezar = sum(1 for n in inicial.nodes if n.state == "ALIVE")

    local = TRABAJO / "replicacion.bin"

    # --- 1. Un archivo de varios bloques -----------------------------------
    demo.titulo(f"Se genera un archivo de {TAMANO_MB} MB")
    sha_original = generar(local, TAMANO_MB * 1024 * 1024, seed=1234)
    demo.nota(f"{local.name}, sha256 {sha_original[:32]}...")

    # --- 2. Subir ----------------------------------------------------------
    demo.titulo("Se sube. El cliente manda los bytes a UNA replica; la cadena reenvia")
    subida = demo.dfsha("put", str(local), REMOTO, "--block-size", str(BLOQUE))
    if subida.returncode != 0:
        demo.mal(f"fallo la subida: {subida.stdout}{subida.stderr}")
        return demo.terminar("")
    demo.nota(subida.stdout.strip().splitlines()[-1] if subida.stdout.strip() else "")

    block_ids = [b.block_id for b in api.open_file(REMOTO).blocks]
    en_disco = bloques_por_nodo(demo, block_ids)
    demo.nota(f"{len(block_ids)} bloques; en disco, por nodo: {en_disco}")

    con_copias = [n for n, c in en_disco.items() if c > 0]
    demo.afirmar(
        sum(max(c, 0) for c in en_disco.values()) == r * len(block_ids),
        f"{r} copias de cada bloque: {r * len(block_ids)} ficheros .blk en "
        f"{len(con_copias)} nodos",
        f"hay {sum(max(c, 0) for c in en_disco.values())} copias en disco; se "
        f"esperaban {r * len(block_ids)} ({r} por bloque)",
    )

    # --- 3. stat dice lo mismo que el disco --------------------------------
    demo.titulo("El ControlNode coincide con lo que hay en los discos")
    info = demo.dfsha("stat", REMOTO)
    print("    " + "\n    ".join(info.stdout.strip().splitlines()[-6:]))
    estado = api.stat(REMOTO)
    demo.afirmar(
        estado.replication_state == "FULLY_REPLICATED",
        f"el metadato dice FULLY_REPLICATED ({estado.min_replicas} de {r} copias)",
        f"el metadato dice {estado.replication_state}",
    )

    # --- 4. EL CONTROL POSITIVO: elegir una victima que TENGA copias -------
    demo.titulo("Se elige la victima: un nodo que SI tiene copias de este archivo")
    demo.nota(
        "matar un nodo sin copias no demostraria nada: el archivo se leeria igual "
        "porque nunca dependio de el"
    )
    victima = max(con_copias, key=lambda n: en_disco[n])
    demo.ok(f"{victima} guarda {en_disco[victima]} de los {len(block_ids)} bloques")

    # --- 5. Matarlo, y comprobar que esta muerto de verdad -----------------
    demo.titulo(f"Se para {victima}")
    demo.correr("docker", "stop", victima)
    vivo = demo.en_nodo(victima, "true")
    demo.afirmar(
        vivo.returncode != 0,
        "el contenedor ya no responde: sus copias son inalcanzables",
        "el contenedor SIGUE respondiendo; la caida no ocurrio",
    )

    # --- 6. El archivo se sigue leyendo ------------------------------------
    demo.titulo("Se descarga con un nodo menos")
    bajado = TRABAJO / "replicacion-bajado.bin"
    bajado.unlink(missing_ok=True)
    descarga = demo.dfsha("get", REMOTO, str(bajado))
    if descarga.returncode != 0 or not bajado.is_file():
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

    # --- 7. El ControlNode lo da por muerto --------------------------------
    demo.titulo("El ControlNode da el nodo por muerto")
    plazo_dead = inicial.dead_after_ms / 1000 + 20
    demo.nota(
        f"SUSPECT a los {inicial.suspect_after_ms / 1000:.0f}s sin latir, DEAD a los "
        f"{inicial.dead_after_ms / 1000:.0f}s. Se espera hasta {plazo_dead:.0f}s"
    )

    def hay_uno_muerto():
        nodos = api.cluster_status().nodes
        vivos = sum(1 for n in nodos if n.state == "ALIVE")
        muertos = sum(1 for n in nodos if n.state == "DEAD")
        return muertos >= 1 and vivos == vivos_al_empezar - 1

    demo.afirmar(
        bool(esperar(hay_uno_muerto, plazo_dead)),
        "un nodo DEAD y el resto ALIVE: la caida se detecto",
        f"tras {plazo_dead:.0f}s el ControlNode todavia no marca DEAD al nodo parado",
    )
    mostrar_cluster(demo, api)

    # --- 8. El cluster repone la copia -------------------------------------
    demo.titulo("El cluster repone las copias que faltan")
    demo.nota(
        "la re-replicacion espera una GRACIA antes de copiar nada, para no gastar la "
        "red por un simple reinicio: 5 minutos por defecto, 30 s si el stack se levanto "
        "con DFSHA_REREPLICATION_GRACE_MS=30000"
    )

    def repuesto():
        info = api.stat(REMOTO)
        return info.replication_state == "FULLY_REPLICATED" and (info.min_replicas or 0) >= r

    # Con el nodo ya DEAD, el metadato tiene que haber bajado antes de volver a subir.
    # Sin ver el UNDER_REPLICATED, un FULLY_REPLICATED no probaria que hubo reposicion.
    bajo = esperar(lambda: api.stat(REMOTO).replication_state == "UNDER_REPLICATED", 15)
    if bajo:
        quedan = api.stat(REMOTO).min_replicas
        demo.ok(f"UNDER_REPLICATED: a algunos bloques les quedan {quedan} copias")
    else:
        demo.aviso("no se llego a ver UNDER_REPLICATED; puede que ya se haya repuesto")

    demo.nota(f"esperando la reposicion, hasta {args.espera_reposicion:.0f}s")
    ok_reposicion = bool(esperar(repuesto, args.espera_reposicion, cada_s=2))
    if ok_reposicion:
        demo.ok(f"FULLY_REPLICATED otra vez: {r} copias por bloque sin {victima}")
        print("    " + "\n    ".join(demo.dfsha("stat", REMOTO).stdout.strip().splitlines()[-6:]))
    else:
        demo.aviso(
            f"no se repuso en {args.espera_reposicion:.0f}s. Si el stack usa la gracia por "
            "defecto (5 min) es lo esperable: repite con --espera-reposicion 400 o levanta "
            "el stack con DFSHA_REREPLICATION_GRACE_MS=30000"
        )

    # --- 9. Devolver el nodo -----------------------------------------------
    demo.titulo(f"Se vuelve a levantar {victima}")
    demo.correr("docker", "start", victima)
    de_vuelta = esperar(
        lambda: sum(1 for n in api.cluster_status().nodes if n.state == "ALIVE")
        >= vivos_al_empezar,
        90,
        cada_s=2,
    )
    if de_vuelta:
        demo.ok("de vuelta: todos los DataNodes ALIVE; el siguiente guion puede empezar")
    else:
        demo.aviso("el nodo no volvio a ALIVE en 90 s; mira `dfsha cluster` antes de seguir")
    demo.nota(
        "al reincorporarse con su disco intacto, algunos bloques pueden quedar con "
        "4 copias en vez de 3. Es un estado esperable y NO se limpia solo; esta "
        "explicado en CLAUDE.md, 'Sobre-replicacion tras una reincorporacion'"
    )

    demo.dfsha("rm", REMOTO)
    if ok_reposicion:
        cierre = """
        Tres copias por bloque, repartidas en nodos distintos.
        Perder un nodo no cuesta disponibilidad: el archivo se lee de las otras.
        Y el hueco se repuso solo, pasada la espera de gracia.
        """
    else:
        cierre = """
        Tres copias por bloque, repartidas en nodos distintos.
        Perder un nodo no cuesta disponibilidad: el archivo se lee de las otras.
        (La reposicion no se llego a ver: ver el aviso del paso 8.)
        """
    return demo.terminar(cierre)


if __name__ == "__main__":
    sys.exit(main())
