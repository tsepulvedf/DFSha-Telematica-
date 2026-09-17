"""Demostracion del criterio 11: un bloque en disco no es legible sin la clave.

    python scripts/demo/cifrado_en_reposo.py

La forma mas directa de ensenarlo, y mejor que mirar bytes con `xxd`: se sube un texto
**conocido y legible**, y luego se busca esa misma frase dentro del disco del DataNode con
un `grep`. Si el cifrado funciona, no aparece.

## Por que hay un control, y por que no sobra

Un `grep` que no encuentra nada no prueba gran cosa por si solo: podria estar mal escrito,
buscar en el directorio equivocado, o fallar en silencio. Por eso se corre **el mismo
grep** contra el fichero local en claro, donde SI tiene que encontrarlo.

Es la leccion del Bloque C aplicada a una demostracion: *una prueba que solo comprueba que
lo malo no aparece no comprueba que la busqueda funcione*. Con el control, las dos mitades
estan: la busqueda encuentra el texto donde esta y no lo encuentra donde no debe estar.

Y al final se baja el archivo y se compara el SHA-256, porque «no se puede leer» sin «pero
se recupera intacto» describiria igual de bien un sistema que corrompe los datos.
"""

from __future__ import annotations

import argparse
import hashlib
import sys

from _comun import TRABAJO, Demo, exigir_docker, exigir_sesion

#: Frase larga, en una sola linea y sin caracteres raros: tiene que sobrevivir a un
#: `grep` dentro de un contenedor sin que haga falta escapar nada.
MARCA = "ESTO-ES-TEXTO-PLANO-LEGIBLE-QUE-NO-DEBERIA-APARECER-EN-DISCO"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--node",
        default="dfsha-data-node-1",
        help="Contenedor del DataNode donde buscar. Por defecto el primero.",
    )
    parser.add_argument(
        "--remote", default="/demo-cifrado.txt", help="Ruta remota del archivo."
    )
    parser.add_argument(
        "--data-dir",
        default="/var/lib/dfsha/blocks",
        help="Donde guarda los bloques el DataNode dentro del contenedor.",
    )
    parser.add_argument(
        "--keep", action="store_true", help="No borrar el archivo remoto al terminar."
    )
    args = parser.parse_args(argv)

    demo = Demo(
        "Criterio 11: el bloque en disco no es el texto claro",
        "la clave se deriva en el cliente y no sale de ahi",
    )
    exigir_docker(demo)
    exigir_sesion(demo)

    claro = TRABAJO / "demo-claro.txt"
    bajado = TRABAJO / "demo-bajado.txt"

    # --- 1. Un texto que cualquiera reconoceria de un vistazo --------------
    demo.titulo("Se crea un archivo con una frase legible")
    claro.write_text(MARCA + "\n", encoding="ascii")
    sha_original = hashlib.sha256(claro.read_bytes()).hexdigest()
    demo.nota(f"contenido: {MARCA}")
    demo.nota(f"sha256:    {sha_original}")

    # --- 2. CONTROL POSITIVO: el grep funciona y el texto esta de verdad ---
    #
    # Va ANTES de subir a proposito. Si se hiciera al final y fallara, no se sabria si es
    # que el cifrado funciona o que la busqueda estaba rota desde el principio.
    demo.titulo("CONTROL POSITIVO: el MISMO grep encuentra la frase en el fichero local")
    control = demo.correr("grep", "-l", MARCA, str(claro))
    if not demo.afirmar(
        control.returncode == 0,
        f"encontrado en {claro}: la busqueda funciona",
        "la busqueda no encuentra el texto NI donde si esta; revisa el grep",
    ):
        return demo.terminar("")

    # --- 3. Subir -----------------------------------------------------------
    demo.titulo("Se sube. El cliente cifra ANTES de que los bytes salgan de esta maquina")
    subida = demo.dfsha("put", str(claro), args.remote)
    if subida.returncode != 0:
        demo.mal(f"fallo la subida: {subida.stdout}{subida.stderr}")
        return demo.terminar("")
    demo.nota(subida.stdout.strip())

    # --- 4. La comprobacion -------------------------------------------------
    demo.titulo(f"Se busca esa misma frase en el disco de {args.node}")
    demo.nota("con acceso completo al disco del servidor, y sin la clave")
    busqueda = demo.correr(
        "docker", "exec", args.node, "grep", "-rl", MARCA, args.data_dir
    )

    # grep devuelve 1 cuando no encuentra nada, que aqui es el resultado BUENO.
    encontrado = busqueda.returncode == 0 and busqueda.stdout.strip()
    if encontrado:
        for linea in busqueda.stdout.strip().splitlines():
            demo.nota(linea)
        demo.mal("ENCONTRADO: el bloque NO esta cifrado y el criterio 11 no se cumple")
        return demo.terminar("")
    demo.ok("NO ENCONTRADO: el bloque esta cifrado")

    # --- 5. Y aun asi se recupera ------------------------------------------
    #
    # Sin esto, un sistema que escribiera basura en vez de cifrar pasaria el paso 4.
    demo.titulo("Se baja y se compara: ilegible en disco, intacto al recuperarlo")
    bajado.unlink(missing_ok=True)
    descarga = demo.dfsha("get", args.remote, str(bajado))
    if descarga.returncode != 0:
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
