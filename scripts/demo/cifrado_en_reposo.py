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

Y al final se baja el archivo y se compara el SHA-256, porque «no se puede leer» sin
«pero se recupera intacto» describiria igual de bien un sistema que corrompe los datos.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

#: Frase larga, en una sola linea y sin caracteres raros: tiene que sobrevivir a un
#: `grep` dentro de un contenedor sin que haga falta escapar nada.
MARCA = "ESTO-ES-TEXTO-PLANO-LEGIBLE-QUE-NO-DEBERIA-APARECER-EN-DISCO"

VERDE = "\033[32m"
ROJO = "\033[31m"
GRIS = "\033[90m"
FIN = "\033[0m"


def _paso(numero: int, texto: str) -> None:
    print(f"\n{GRIS}[{numero}]{FIN} {texto}")


def _correr(orden: list[str], **kwargs) -> subprocess.CompletedProcess:
    print(f"    {GRIS}$ {' '.join(orden)}{FIN}")
    return subprocess.run(orden, capture_output=True, text=True, **kwargs)


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

    trabajo = Path("tmp")
    trabajo.mkdir(exist_ok=True)
    claro = trabajo / "demo-claro.txt"
    bajado = trabajo / "demo-bajado.txt"

    print("=" * 72)
    print("  Criterio 11: el bloque en disco no es el texto claro")
    print("=" * 72)

    # --- 1. Un texto que cualquiera reconoceria de un vistazo ---------------
    _paso(1, "Se crea un archivo con una frase legible")
    claro.write_text(MARCA + "\n", encoding="ascii")
    sha_original = hashlib.sha256(claro.read_bytes()).hexdigest()
    print(f"    contenido: {MARCA}")
    print(f"    sha256:    {sha_original}")

    # --- 2. Control: el grep funciona y el texto esta de verdad -------------
    #
    # Va ANTES de subir a proposito. Si se hiciera al final, y fallara, no se sabria si
    # es que el cifrado funciona o que la busqueda estaba rota desde el principio.
    _paso(2, "Control: el MISMO grep encuentra la frase en el fichero local")
    control = _correr(["grep", "-l", MARCA, str(claro)])
    if control.returncode != 0:
        print(f"    {ROJO}La busqueda no encuentra el texto ni donde SI esta.{FIN}")
        print("    La demostracion no vale: revisa el grep antes de seguir.")
        return 2
    print(f"    {VERDE}encontrado{FIN} en {claro} — la busqueda funciona")

    # --- 3. Subir ----------------------------------------------------------
    _paso(3, "Se sube. El cliente cifra ANTES de que los bytes salgan de esta maquina")
    subida = _correr(["dfsha", "put", str(claro), args.remote])
    if subida.returncode != 0:
        print(f"    {ROJO}fallo la subida{FIN}\n{subida.stdout}{subida.stderr}")
        print("    ¿Hay sesion iniciada? Prueba 'dfsha login <usuario>'.")
        return 1
    print(f"    {subida.stdout.strip()}")

    # --- 4. La comprobacion ------------------------------------------------
    _paso(4, f"Se busca esa misma frase en el disco de {args.node}")
    print(f"    {GRIS}(con acceso completo al disco del servidor, sin la clave){FIN}")
    busqueda = _correr(
        [
            "docker",
            "exec",
            args.node,
            "grep",
            "-rl",
            MARCA,
            args.data_dir,
        ]
    )

    # grep devuelve 1 cuando no encuentra nada, que aqui es el resultado BUENO.
    encontrado = busqueda.returncode == 0 and busqueda.stdout.strip()
    if encontrado:
        print(f"    {ROJO}ENCONTRADO{FIN} en:")
        for linea in busqueda.stdout.strip().splitlines():
            print(f"      {linea}")
        print(f"\n    {ROJO}El bloque NO esta cifrado. El criterio 11 no se cumple.{FIN}")
        return 1
    print(f"    {VERDE}NO ENCONTRADO{FIN} — el bloque esta cifrado")

    # --- 5. Y aun asi se recupera ------------------------------------------
    #
    # Sin esto, un sistema que escribiera basura en vez de cifrar pasaria el paso 4.
    _paso(5, "Se baja y se compara: ilegible en disco, intacto al recuperarlo")
    bajado.unlink(missing_ok=True)
    descarga = _correr(["dfsha", "get", args.remote, str(bajado)])
    if descarga.returncode != 0:
        print(f"    {ROJO}fallo la descarga{FIN}\n{descarga.stdout}{descarga.stderr}")
        return 1

    sha_bajado = hashlib.sha256(bajado.read_bytes()).hexdigest()
    print(f"    sha256 original: {sha_original}")
    print(f"    sha256 bajado:   {sha_bajado}")
    if sha_bajado != sha_original:
        print(f"    {ROJO}NO COINCIDEN{FIN}")
        return 1
    print(f"    {VERDE}coinciden{FIN}")

    if not args.keep:
        _correr(["dfsha", "rm", args.remote])

    print(f"\n{VERDE}{'=' * 72}{FIN}")
    print(f"{VERDE}  El servidor guarda estos datos y no puede leerlos.{FIN}")
    print(f"{VERDE}  La clave se deriva en el cliente y nunca sale de aqui.{FIN}")
    print(f"{VERDE}{'=' * 72}{FIN}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
