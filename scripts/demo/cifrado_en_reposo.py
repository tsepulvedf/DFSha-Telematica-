"""Demostracion del criterio 11: un bloque en disco no es legible sin la clave.

    python scripts/demo/cifrado_en_reposo.py

La forma mas directa de ensenarlo, y mejor que mirar bytes con `xxd`: se sube un texto
**conocido y legible**, y luego se busca esa misma frase dentro del disco de los
DataNodes con un `grep`. Si el cifrado funciona, no aparece.

## Por que hay controles, y por que no sobran

Un «no encontrado» se puede obtener por accidente de tres formas, y cada una tiene su
control:

| Por accidente | Control |
|---|---|
| el `grep` del contenedor esta mal escrito o no funciona | el MISMO `grep`, en el MISMO contenedor, sobre la frase en claro: tiene que encontrarla |
| se busca en un nodo que no tiene el bloque (con R=3 y cuatro nodos, uno de cada cuatro no lo tiene) | se busca en TODOS, y se exige que al menos uno tenga el `.blk` en disco |
| el `grep` falla (directorio inexistente) y eso se lee como «no encontrado» | `grep` devuelve 1 si no encuentra y 2 si falla: solo el 1 vale |

Los dos ultimos no son teoricos: la primera version de este guion buscaba solo en
`data-node-1` y trataba cualquier codigo distinto de 0 como «no encontrado», asi que un
`--data-dir` mal escrito, o un bloque que cayo en otros tres nodos, daban el criterio por
cumplido.

El fichero local en claro se comprueba en Python y no con `grep`: el guion se graba en
Windows, donde el host no tiene `grep`. Dentro del contenedor si lo hay.

Y al final se baja el archivo y se compara el SHA-256, porque «no se puede leer» sin «pero
se recupera intacto» describiria igual de bien un sistema que corrompe los datos.
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
    esperar_cluster,
    exigir_docker,
    exigir_sesion,
    sesion_del_cli,
)

#: Frase larga, en una sola linea y sin caracteres raros: tiene que sobrevivir a un
#: `grep` dentro de un contenedor sin que haga falta escapar nada.
MARCA = "ESTO-ES-TEXTO-PLANO-LEGIBLE-QUE-NO-DEBERIA-APARECER-EN-DISCO"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--node",
        action="append",
        help="Contenedor de DataNode donde buscar; repetible. Por defecto, los cuatro.",
    )
    parser.add_argument(
        "--remote", default="/demo-cifrado.txt", help="Ruta remota del archivo."
    )
    parser.add_argument(
        "--data-dir",
        default=DIR_BLOQUES,
        help="Donde guarda los bloques el DataNode dentro del contenedor.",
    )
    parser.add_argument(
        "--keep", action="store_true", help="No borrar el archivo remoto al terminar."
    )
    args = parser.parse_args(argv)
    nodos = args.node or DATANODES

    from dfsha.client.api import ControlApi

    demo = Demo(
        "Criterio 11: el bloque en disco no es el texto claro",
        "la clave se deriva en el cliente y no sale de ahi",
    )
    exigir_docker(demo)
    exigir_sesion(demo)
    api = ControlApi(sesion_del_cli())
    esperar_cluster(demo, api)

    claro = TRABAJO / "demo-claro.txt"
    bajado = TRABAJO / "demo-bajado.txt"

    # --- 1. Un texto que cualquiera reconoceria de un vistazo --------------
    demo.titulo("Se crea un archivo con una frase legible")
    claro.write_text(MARCA + "\n", encoding="ascii")
    sha_original = hashlib.sha256(claro.read_bytes()).hexdigest()
    demo.nota(f"contenido: {MARCA}")
    demo.nota(f"sha256:    {sha_original}")

    # --- 2. CONTROLES POSITIVOS: el texto esta y la busqueda lo encuentra --
    #
    # Van ANTES de subir a proposito. Si se hicieran al final y fallaran, no se sabria si
    # es que el cifrado funciona o que la busqueda estaba rota desde el principio.
    demo.titulo("CONTROL POSITIVO: la frase esta en claro, y el grep la encuentra")
    if not demo.afirmar(
        MARCA.encode("ascii") in claro.read_bytes(),
        f"la frase esta en {claro.name}, en claro",
        f"la frase NO esta en {claro}; el fichero de partida esta mal",
    ):
        return demo.terminar("")

    for nodo in nodos:
        # El mismo `grep` que buscara en el disco, en el mismo contenedor, sobre la frase
        # en claro por la entrada estandar. Si aqui no la encuentra, un «no encontrado»
        # en el disco no significaria nada.
        control = demo.correr(
            "docker", "exec", "-i", nodo, "grep", "-l", MARCA, "-",
            mostrar=nodo == nodos[0],
            entrada=MARCA + "\n",
        )
        if not demo.afirmar(
            control.returncode == 0,
            f"el grep de {nodo} encuentra la frase cuando esta: la busqueda funciona",
            f"el grep de {nodo} no la encuentra NI en claro "
            f"(codigo {control.returncode}): {control.stderr.strip()}",
        ):
            return demo.terminar("")

    # --- 3. Subir -----------------------------------------------------------
    demo.titulo("Se sube. El cliente cifra ANTES de que los bytes salgan de esta maquina")
    subida = demo.dfsha("put", str(claro), args.remote)
    if subida.returncode != 0:
        demo.mal(f"fallo la subida: {subida.stdout}{subida.stderr}")
        return demo.terminar("")
    demo.nota(subida.stdout.strip().splitlines()[-1] if subida.stdout.strip() else "")

    # --- 4. CONTROL POSITIVO: se busca donde ESTA el bloque ----------------
    demo.titulo("CONTROL POSITIVO: el bloque esta en el disco de los nodos donde se busca")
    demo.nota(
        "con R=3 y cuatro nodos, uno no tiene copia: buscar solo ahi daria "
        "«no encontrado» sin demostrar nada"
    )
    bloques = [b.block_id for b in api.open_file(args.remote).blocks]
    rutas = " ".join(f"{args.data_dir}/{b[:2]}/{b}.blk" for b in bloques)
    con_bloque = [
        nodo
        for nodo in nodos
        if demo.en_nodo(nodo, f"ls {rutas} >/dev/null 2>&1").returncode == 0
    ]
    if not demo.afirmar(
        bool(con_bloque),
        f"el bloque {bloques[0][:8]}... esta en disco en: {', '.join(con_bloque)}",
        f"ninguno de {', '.join(nodos)} tiene el bloque en {args.data_dir}; "
        "se estaria buscando en el sitio equivocado",
    ):
        return demo.terminar("")

    # --- 5. La comprobacion -------------------------------------------------
    demo.titulo("Se busca la frase en el disco de esos nodos")
    demo.nota("con acceso completo al disco del servidor, y sin la clave")
    for nodo in con_bloque:
        busqueda = demo.correr(
            "docker", "exec", nodo, "grep", "-rl", MARCA, args.data_dir
        )
        # grep: 0 = encontrado, 1 = NO encontrado (el resultado bueno), 2 = error.
        # Solo el 1 demuestra algo; un 2 es una busqueda que no llego a hacerse.
        if busqueda.returncode == 0:
            for linea in busqueda.stdout.strip().splitlines():
                demo.nota(linea)
            demo.mal(f"ENCONTRADO en {nodo}: el bloque NO esta cifrado")
            return demo.terminar("")
        if busqueda.returncode != 1:
            demo.mal(
                f"la busqueda en {nodo} FALLO (codigo {busqueda.returncode}): "
                f"{busqueda.stderr.strip()}; eso no es un «no encontrado»"
            )
            return demo.terminar("")
        demo.ok(f"NO ENCONTRADO en {nodo}: su copia esta cifrada")

    # --- 6. Y aun asi se recupera ------------------------------------------
    #
    # Sin esto, un sistema que escribiera basura en vez de cifrar pasaria el paso 5.
    demo.titulo("Se baja y se compara: ilegible en disco, intacto al recuperarlo")
    bajado.unlink(missing_ok=True)
    descarga = demo.dfsha("get", args.remote, str(bajado))
    if descarga.returncode != 0 or not bajado.is_file():
        demo.mal(f"fallo la descarga: {descarga.stdout}{descarga.stderr}")
        return demo.terminar("")

    sha_bajado = hashlib.sha256(bajado.read_bytes()).hexdigest()
    demo.nota(f"sha256 original: {sha_original}")
    demo.nota(f"sha256 bajado:   {sha_bajado}")
    demo.afirmar(
        sha_bajado == sha_original,
        "coinciden: se recupera intacto",
        "NO coinciden: el archivo se corrompio",
    )

    if not args.keep:
        demo.dfsha("rm", args.remote)

    return demo.terminar(
        """
        El servidor guarda estos datos y no puede leerlos.
        La clave se deriva en el cliente y nunca sale de aqui.
        """
    )


if __name__ == "__main__":
    sys.exit(main())
