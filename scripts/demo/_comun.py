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

## La otra regla: se graban en Windows

Ninguno de los cuatro se habia ejecutado nunca cuando se fueron a grabar, y dos fallaron
al primer intento por invocaciones que nadie habia corrido: un `grep` en el host y una
llamada a `gen_testfile.py` con argumentos que el script no acepta. De ahi estas reglas:

- **En el host, solo Python y `docker`.** Nada de `grep`, `find` ni `sh`: en Windows no
  existen. Dentro de un contenedor si, que ahi hay shell (`Demo.en_nodo`).
- **Rutas ancladas a la raiz del repositorio**, no al directorio desde el que se lance.
- **Subprocesos en UTF-8.** La consola de Windows decodifica en cp1252, y la salida de
  Rich o una linea de log con un caracter fuera de ese juego rompe la lectura.
- **Esperar al cluster antes de empezar** (`esperar_cluster`). Lanzar un guion segundos
  despues del `up` daba un 409 de quorum que parecia un fallo del sistema.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

#: Raiz del repositorio. Todo se ancla aqui para que de igual desde donde se lance.
RAIZ = Path(__file__).resolve().parents[2]

# `gen_testfile` se IMPORTA en vez de invocarse: una invocacion con argumentos que el
# script no acepta es exactamente el fallo que tuvo `replicacion_y_caida.py`, y un import
# roto lo atrapa `tests/unit/test_todo_compila.py`.
if str(RAIZ / "scripts") not in sys.path:
    sys.path.insert(0, str(RAIZ / "scripts"))

if sys.platform == "win32":
    # Activa las secuencias ANSI en la consola clasica de Windows (Windows Terminal ya las
    # entiende). Sin esto los colores salen como basura en la grabacion.
    os.system("")
for _flujo in (sys.stdout, sys.stderr):
    try:
        _flujo.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

VERDE = "\033[32m"
ROJO = "\033[31m"
AMARILLO = "\033[33m"
GRIS = "\033[90m"
NEGRITA = "\033[1m"
FIN = "\033[0m"

#: Donde los guiones dejan sus ficheros de trabajo. Esta en .gitignore.
TRABAJO = RAIZ / "tmp"

#: Contenedores, tal y como los fija docker-compose.yml.
DATANODES = [f"dfsha-data-node-{n}" for n in (1, 2, 3, 4)]
CONTROLNODES = [f"dfsha-control-node-{n}" for n in (1, 2, 3)]
DIR_BLOQUES = "/var/lib/dfsha/blocks"

#: Entorno de los subprocesos: el CLI escribe con Rich, y en Windows un hijo con la
#: salida redirigida codifica en cp1252 y revienta al primer caracter fuera de ese juego.
_ENTORNO_HIJO = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

#: La CA con la que verificar HTTPS (C2): la del repositorio si nadie dijo otra. Se fija
#: en el entorno para que la hereden tambien los `dfsha` que lanzan los guiones, que asi
#: no dependen del directorio desde el que se ejecuten.
if not os.environ.get("DFSHA_TLS_CA_CERT") and (RAIZ / "certs" / "ca.crt").is_file():
    os.environ["DFSHA_TLS_CA_CERT"] = str(RAIZ / "certs" / "ca.crt")


def _url_del_control() -> str:
    """El ControlNode, resuelto IGUAL que lo resuelve el CLI: la variable si esta, si no
    la de la sesion guardada, y si no el default. Escribirlo a mano en cada guion obligaria
    a editar cuatro ficheros para apuntar a otro despliegue; leer solo la variable hacia
    que, con C2 encendido y la sesion ya en https, los guiones siguieran preguntando en
    http y esperaran tres minutos a un ControlNode que si estaba.
    """
    try:
        from dfsha.client.session import SessionStore

        return SessionStore().load("http://localhost:8000").control_url
    except Exception:  # noqa: BLE001 - sin sesion legible, el default
        return os.environ.get("DFSHA_CONTROL_URL", "http://localhost:8000")


CONTROL_URL = _url_del_control()


def sesion_del_cli():
    """La sesion que dejo `dfsha login`, para los guiones que necesitan la API directa.

    Se pasa `CONTROL_URL` explicitamente y no `None`: sin fichero de sesion, `load(None)`
    devolveria una sesion con `control_url=None` y el fallo aparecerian mas adelante como
    una URL malformada, que no dice nada. Quien llame a esto ya paso por `exigir_sesion`.
    """
    from dfsha.client.session import SessionStore

    return SessionStore().load(CONTROL_URL)


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

    def correr(
        self,
        *orden: str,
        mostrar: bool = True,
        entrada: str | None = None,
        entorno: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        """Ejecuta una orden y devuelve su resultado, nunca una excepcion.

        Si el ejecutable no existe devuelve codigo 127, como un shell, con el motivo en
        `stderr`. Un `FileNotFoundError` con traza en mitad de una grabacion es justo lo
        que paso con el `grep` del host, y no decia que faltaba ni por que.
        """
        if mostrar:
            print(f"    {GRIS}$ {' '.join(orden)}{FIN}")
        try:
            return subprocess.run(
                list(orden),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                input=entrada,
                env={**_ENTORNO_HIJO, **(entorno or {})},
            )
        except FileNotFoundError:
            return subprocess.CompletedProcess(
                list(orden), 127, "", f"no se encuentra el ejecutable '{orden[0]}'"
            )

    def dfsha(self, *args: str, mostrar: bool = True) -> subprocess.CompletedProcess:
        # Se muestra como `dfsha ...` aunque por debajo se llame por el interprete: es lo
        # que teclearia quien ve el video.
        if mostrar:
            print(f"    {GRIS}$ dfsha {' '.join(args)}{FIN}")
        return self.correr(*_ORDEN_DFSHA, *args, mostrar=False)

    def en_nodo(self, contenedor: str, orden: str) -> subprocess.CompletedProcess:
        """Un comando dentro de un contenedor. Ahi SI hay shell; en el host no."""
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
            # Sin `text=True`: la salida no se lee, y decodificarla en cp1252 podria fallar.
            subprocess.run(["docker", "info"], capture_output=True).returncode
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
    if demo.dfsha("pwd", mostrar=False).returncode != 0:
        demo.mal("no hay sesion iniciada")
        print(f"\n    {GRIS}dfsha register <usuario> && dfsha login <usuario>{FIN}\n")
        sys.exit(1)


#: Como invocar el CLI. `dfsha` si esta en el PATH (el venv activado); si no, el mismo
#: punto de entrada por el interprete que corre el guion, que en Windows evita depender
#: de que el lanzador `dfsha.exe` este donde el PATH lo busca.
_ORDEN_DFSHA: tuple[str, ...] = (
    (shutil.which("dfsha"),)
    if shutil.which("dfsha")
    else (sys.executable, "-c", "import sys; from dfsha.client.cli import main; sys.exit(main())")
)


def control_node_responde() -> bool:
    """`/health` del ControlNode a traves del balanceador. No exige sesion."""
    import httpx

    from dfsha.client.tls import verificacion_para

    url = f"{CONTROL_URL.rstrip('/')}/health"
    try:
        return httpx.get(url, timeout=3, verify=verificacion_para(url)).status_code == 200
    except Exception:  # noqa: BLE001 - arrancando, cualquier fallo es "todavia no"
        return False


def esperar_control_node(demo: Demo, limite_s: float = 180.0) -> None:
    """Espera a que el ControlNode conteste, o sale con el motivo."""
    inicio = time.monotonic()
    avisado = False
    while not control_node_responde():
        if time.monotonic() - inicio > limite_s:
            demo.mal(f"el ControlNode no responde en {CONTROL_URL} tras {limite_s:.0f}s")
            print(f"\n    {GRIS}docker compose ps{FIN}\n")
            sys.exit(1)
        if not avisado:
            demo.nota(f"esperando a que el ControlNode responda en {CONTROL_URL}...")
            avisado = True
        time.sleep(2)


def esperar_cluster(
    demo: Demo, api, minimo: int | None = None, limite_s: float = 180.0
):
    """No empieza hasta que haya al menos R DataNodes ALIVE (o `minimo`, si se da).

    **Por que existe.** Lanzar un guion segundos despues de `docker compose up` daba un
    `409 no alcanzan el quorum de escritura`: el primer `put` encontraba DataNodes todavia
    en `health: starting`. El sistema estaba bien; el guion no. Y un 409 de quorum se lee
    como un fallo del sistema, que es lo peor que puede ensenar una demostracion.

    Se comprueba con `cluster_status` por la API, no raspando `dfsha cluster`. Devuelve el
    ultimo estado leido, que quien llama suele querer para contar nodos.
    """
    esperar_control_node(demo, limite_s)
    inicio = time.monotonic()
    ultimo_mensaje = ""
    while True:
        try:
            estado = api.cluster_status()
            necesarios = minimo if minimo is not None else estado.replication_factor
            vivos = sum(1 for n in estado.nodes if n.state == "ALIVE")
            if vivos >= necesarios:
                demo.ok(
                    f"cluster listo: {vivos}/{len(estado.nodes)} DataNodes ALIVE "
                    f"(hacen falta {necesarios}; R={estado.replication_factor}, "
                    f"W={estado.write_quorum})"
                )
                return estado
            mensaje = f"{vivos} DataNodes ALIVE de {necesarios} necesarios"
        except Exception as exc:  # noqa: BLE001 - arrancando: 502, conexion, 401...
            mensaje = f"el cluster aun no contesta ({type(exc).__name__})"

        if time.monotonic() - inicio > limite_s:
            demo.mal(f"el cluster no estuvo listo en {limite_s:.0f}s: {mensaje}")
            print(f"\n    {GRIS}docker compose ps && dfsha cluster{FIN}\n")
            sys.exit(1)
        if mensaje != ultimo_mensaje:
            demo.nota(f"esperando al cluster: {mensaje}")
            ultimo_mensaje = mensaje
        time.sleep(2)


def mostrar_cluster(demo: Demo, api) -> None:
    """Vista compacta de `dfsha cluster`, que cabe en una consola de Windows.

    La tabla de Rich, con la salida capturada, se dibuja a 80 columnas y parte la
    direccion de cada nodo en tres lineas: ilegible en un video. Aqui una linea por nodo,
    con los mismos datos que importan para la demostracion, leidos por la API.
    """
    from urllib.parse import urlparse

    colores = {"ALIVE": VERDE, "SUSPECT": AMARILLO, "DEAD": ROJO}
    print(f"    {GRIS}$ dfsha cluster   (vista compacta){FIN}")
    try:
        estado = api.cluster_status()
    except Exception as exc:  # noqa: BLE001
        demo.aviso(f"no se pudo leer el estado del cluster: {exc}")
        return
    print(f"    {'DataNode':<22}{'estado':<9}{'dominio':<12}{'bloques':>8}")
    for n in sorted(estado.nodes, key=lambda n: n.advertise_url):
        color = colores.get(n.state, "")
        print(
            f"    {urlparse(n.advertise_url).netloc:<22}{color}{n.state:<9}{FIN}"
            f"{n.fault_domain:<12}{n.block_count:>8}"
        )
    resumen = f"R={estado.replication_factor} W={estado.write_quorum}"
    if estado.under_replicated_blocks:
        resumen += f", {estado.under_replicated_blocks} bloques sub-replicados"
    try:
        lider = api.leadership()
        if lider.leader_id:
            resumen += f"; lider {lider.leader_id[:8]} (epoca {lider.epoch})"
    except Exception:  # noqa: BLE001 - en pleno relevo puede fallar un instante
        pass
    print(f"    {GRIS}{resumen}{FIN}")


def esperar(condicion, limite_s: float, cada_s: float = 1.0):
    """Reintenta `condicion()` hasta que devuelva algo verdadero o venza el plazo.

    Devuelve lo ultimo que devolvio, verdadero o no. Las excepciones cuentan como «todavia
    no»: durante una caida provocada, que la API tropiece un momento es lo esperable.
    """
    limite = time.monotonic() + limite_s
    resultado = None
    while True:
        try:
            resultado = condicion()
        except Exception:  # noqa: BLE001
            resultado = None
        if resultado or time.monotonic() >= limite:
            return resultado
        time.sleep(cada_s)
