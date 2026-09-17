"""Cosas compartidas por los guiones de demostracion.

## La regla que siguen los cuatro: control positivo junto al negativo

Todos estos guiones demuestran algo por **ausencia**: el texto claro no aparece en disco,
el intruso no puede leer el bloque, el archivo sigue disponible tras matar un nodo. Una
comprobacion por ausencia es facil de pasar por accidente:

- un `grep` que no encuentra nada tambien se escribe mal, o busca en el sitio equivocado
- un 403 tambien lo devuelve un servidor que rechaza a todo el mundo
- «el archivo se sigue leyendo» tambien pasa si el nodo que matamos no tenia ninguna copia

Por eso cada guion ejecuta **la misma comprobacion donde SI tiene que dar positivo**. Sin
esa mitad no se esta demostrando el sistema, se esta demostrando que la comprobacion no
encuentra nada, que no es lo mismo.

Es la leccion del Bloque C aplicada a una demostracion: *una prueba que solo comprueba que
lo malo se rechaza no comprueba que lo bueno funciona*.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

VERDE = "\033[32m"
ROJO = "\033[31m"
AMARILLO = "\033[33m"
GRIS = "\033[90m"
NEGRITA = "\033[1m"
FIN = "\033[0m"

#: Donde los guiones dejan sus ficheros de trabajo. Esta en .gitignore.
TRABAJO = Path("tmp")


class Demo:
    """Lleva la cuenta de los pasos y de si algo fallo."""

    def __init__(self, titulo: str, subtitulo: str = "") -> None:
        self.paso = 0
        self.fallos: list[str] = []
        TRABAJO.mkdir(exist_ok=True)
        print("=" * 76)
        print(f"  {NEGRITA}{titulo}{FIN}")
        if subtitulo:
            print(f"  {GRIS}{subtitulo}{FIN}")
        print("=" * 76)

    # --- narracion ---------------------------------------------------------

    def titulo(self, texto: str) -> None:
        self.paso += 1
        print(f"\n{GRIS}[{self.paso}]{FIN} {NEGRITA}{texto}{FIN}")

    def nota(self, texto: str) -> None:
        print(f"    {GRIS}{texto}{FIN}")

    def ok(self, texto: str) -> None:
        print(f"    {VERDE}OK{FIN}  {texto}")

    def mal(self, texto: str) -> None:
        print(f"    {ROJO}MAL{FIN} {texto}")
        self.fallos.append(texto)

    def aviso(self, texto: str) -> None:
        print(f"    {AMARILLO}!{FIN}   {texto}")

    def afirmar(self, condicion: bool, bien: str, mal: str) -> bool:
        (self.ok if condicion else self.mal)(bien if condicion else mal)
        return condicion

    def esperar(self, segundos: float, motivo: str) -> None:
        print(f"    {GRIS}esperando {segundos:.0f}s: {motivo}{FIN}")
        time.sleep(segundos)

    # --- ejecucion ---------------------------------------------------------

    def correr(self, *orden: str, mostrar: bool = True) -> subprocess.CompletedProcess:
        if mostrar:
            print(f"    {GRIS}$ {' '.join(orden)}{FIN}")
        return subprocess.run(list(orden), capture_output=True, text=True)

    def dfsha(self, *args: str) -> subprocess.CompletedProcess:
        return self.correr("dfsha", *args)

    def en_nodo(self, contenedor: str, orden: str) -> subprocess.CompletedProcess:
        """Un comando dentro del contenedor de un DataNode."""
        return self.correr("docker", "exec", contenedor, "sh", "-c", orden)

    # --- cierre ------------------------------------------------------------

    def terminar(self, mensaje_final: str) -> int:
        print()
        if self.fallos:
            print(f"{ROJO}{'=' * 76}{FIN}")
            print(f"{ROJO}  LA DEMOSTRACION NO PASO ({len(self.fallos)} comprobaciones){FIN}")
            for f in self.fallos:
                print(f"{ROJO}    - {f}{FIN}")
            print(f"{ROJO}{'=' * 76}{FIN}")
            return 1
        print(f"{VERDE}{'=' * 76}{FIN}")
        for linea in mensaje_final.strip().splitlines():
            print(f"{VERDE}  {linea.strip()}{FIN}")
        print(f"{VERDE}{'=' * 76}{FIN}")
        return 0


def hay_docker() -> bool:
    try:
        return (
            subprocess.run(
                ["docker", "info"], capture_output=True, text=True
            ).returncode
            == 0
        )
    except FileNotFoundError:
        return False


def exigir_docker(demo: Demo) -> None:
    if not hay_docker():
        demo.mal("no hay un demonio de Docker en marcha; levanta el stack primero")
        print(f"\n    {GRIS}docker compose up -d{FIN}\n")
        sys.exit(1)


def exigir_sesion(demo: Demo) -> None:
    """Falla pronto y con el comando exacto si no hay sesion iniciada.

    Fallar aqui es mucho mejor que fallar tres pasos mas adelante con un 401 en medio de
    una demostracion que alguien esta grabando.
    """
    if demo.correr("dfsha", "pwd", mostrar=False).returncode != 0:
        demo.mal("no hay sesion iniciada")
        print(f"\n    {GRIS}dfsha register <usuario> && dfsha login <usuario>{FIN}\n")
        sys.exit(1)
