"""CLI de DFSha.

    dfsha register / login
    dfsha ls / cd / pwd / mkdir / rmdir / rm / mv / stat
    dfsha put <local> <remoto>
    dfsha get <remoto> <local>

`cd` y `pwd` operan sobre un cwd del lado del cliente, guardado junto al token en
`~/.dfsha/session.json`. El ControlNode no guarda sesion.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TransferSpeedColumn,
)
from rich.table import Table

from dfsha.common.errors import DFShaError
from dfsha.common.logging import configure_logging, get_logger, timed

from .api import ControlApi, resolve_path
from .chunker import file_size
from .session import Session, SessionStore
from .transfer import download_blocks, upload_blocks

__all__ = ["app", "main"]

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Cliente de DFSha, sistema de archivos distribuido por bloques.",
)
console = Console()

DEFAULT_CONTROL_URL = "http://localhost:8000"


def _store() -> SessionStore:
    return SessionStore()


def _session() -> Session:
    return _store().load(os.environ.get("DFSHA_CONTROL_URL", DEFAULT_CONTROL_URL))


def _api() -> tuple[ControlApi, Session]:
    sesion = _session()
    return ControlApi(sesion), sesion


def _fallar(error: DFShaError) -> None:
    console.print(f"[red]error[/red] {error.message}")
    if error.details:
        console.print(f"[dim]{error.details}[/dim]")
    raise typer.Exit(code=1)


@app.callback()
def _configurar(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Emite los logs JSON."),
) -> None:
    # Por defecto el cliente calla: los logs JSON de tiempos van a stdout y ensuciarian
    # la salida de la CLI. Con -v se emiten, que es como se recogen los datos de los
    # benchmarks de la Etapa 4.
    configure_logging(service="client", level="INFO" if verbose else "CRITICAL")


# --- Autenticacion ---------------------------------------------------------


@app.command()
def register(
    username: str = typer.Argument(..., help="Nombre de usuario."),
    password: str = typer.Option(
        ..., prompt=True, hide_input=True, confirmation_prompt=True
    ),
) -> None:
    """Crea una cuenta nueva."""
    api, _ = _api()
    try:
        api.register(username, password)
    except DFShaError as error:
        _fallar(error)
    console.print(f"[green]usuario creado:[/green] {username}")


@app.command()
def login(
    username: str = typer.Argument(..., help="Nombre de usuario."),
    password: str = typer.Option(..., prompt=True, hide_input=True),
) -> None:
    """Inicia sesion y guarda el token en ~/.dfsha/session.json."""
    store = _store()
    sesion = store.load(os.environ.get("DFSHA_CONTROL_URL", DEFAULT_CONTROL_URL))
    try:
        token = ControlApi(sesion).login(username, password)
    except DFShaError as error:
        _fallar(error)

    sesion.token = token.access_token
    sesion.username = username
    sesion.cwd = "/"
    try:
        store.save(sesion)
    except DFShaError as error:
        _fallar(error)
    console.print(f"[green]sesion iniciada como[/green] {username}")
    # Decir donde quedo guardada: si el siguiente comando se queja de que no hay sesion,
    # esta linea es la mitad del diagnostico. Pasa dentro de contenedores, cuando el
    # directorio no es el que persiste.
    console.print(f"[dim]sesion guardada en {store.path}[/dim]")


@app.command()
def logout() -> None:
    """Olvida el token guardado."""
    _store().clear()
    console.print("sesion cerrada")


# --- Namespace -------------------------------------------------------------


@app.command()
def pwd() -> None:
    """Muestra el directorio de trabajo."""
    console.print(_session().cwd)


@app.command()
def cd(path: str = typer.Argument("/", help="Ruta absoluta o relativa.")) -> None:
    """Cambia el directorio de trabajo del cliente."""
    store = _store()
    sesion = store.load(os.environ.get("DFSHA_CONTROL_URL", DEFAULT_CONTROL_URL))
    destino = resolve_path(sesion, path)
    try:
        # Se comprueba contra el servidor para no dejar el cwd apuntando a la nada.
        info = ControlApi(sesion).stat(destino)
        if info.type != "directory":
            console.print(f"[red]error[/red] no es un directorio: {destino}")
            raise typer.Exit(code=1)
        sesion.cwd = info.path
    except DFShaError as error:
        _fallar(error)

    store.save(sesion)
    console.print(sesion.cwd)


@app.command(name="ls")
def listar(path: str = typer.Argument(None, help="Directorio a listar.")) -> None:
    """Lista un directorio."""
    api, sesion = _api()
    try:
        respuesta = api.ls(resolve_path(sesion, path))
    except DFShaError as error:
        _fallar(error)

    if not respuesta.entries:
        console.print("[dim](vacio)[/dim]")
        return

    tabla = Table(box=None, pad_edge=False)
    tabla.add_column("tipo", style="cyan", width=4)
    tabla.add_column("tamano", justify="right", style="magenta")
    tabla.add_column("creado", style="dim")
    tabla.add_column("nombre")
    for entrada in respuesta.entries:
        tabla.add_row(
            "dir" if entrada.type == "directory" else "arch",
            "-" if entrada.type == "directory" else _humano(entrada.size),
            entrada.created_at.strftime("%Y-%m-%d %H:%M"),
            entrada.name + ("/" if entrada.type == "directory" else ""),
        )
    console.print(tabla)


@app.command()
def mkdir(
    path: str,
    parents: bool = typer.Option(False, "--parents", "-p", help="Crea los padres."),
) -> None:
    """Crea un directorio."""
    api, sesion = _api()
    try:
        api.mkdir(resolve_path(sesion, path), parents)
    except DFShaError as error:
        _fallar(error)


@app.command()
def rmdir(
    path: str,
    recursive: bool = typer.Option(
        False, "--recursive", "-r", help="Borra tambien el contenido."
    ),
) -> None:
    """Borra un directorio."""
    api, sesion = _api()
    try:
        api.rmdir(resolve_path(sesion, path), recursive)
    except DFShaError as error:
        _fallar(error)


@app.command()
def rm(path: str) -> None:
    """Borra un archivo.

    El borrado es logico: los bloques siguen en disco hasta que se corra scripts/gc.py.
    """
    api, sesion = _api()
    try:
        api.rm(resolve_path(sesion, path))
    except DFShaError as error:
        _fallar(error)


@app.command()
def mv(src: str, dst: str) -> None:
    """Mueve o renombra. Es metadato puro: no se transfiere un solo byte."""
    api, sesion = _api()
    try:
        api.mv(resolve_path(sesion, src), resolve_path(sesion, dst))
    except DFShaError as error:
        _fallar(error)


@app.command()
def stat(path: str) -> None:
    """Muestra los metadatos de una ruta."""
    api, sesion = _api()
    try:
        info = api.stat(resolve_path(sesion, path))
    except DFShaError as error:
        _fallar(error)

    tabla = Table(box=None, pad_edge=False, show_header=False)
    tabla.add_row("ruta", info.path)
    tabla.add_row("tipo", info.type)
    tabla.add_row("tamano", _humano(info.size))
    if info.type == "file":
        tabla.add_row("tamano de bloque", _humano(info.block_size or 0))
        tabla.add_row("bloques", str(info.block_count))
    tabla.add_row("creado", info.created_at.isoformat())
    console.print(tabla)


# --- Cluster ---------------------------------------------------------------


@app.command()
def cluster() -> None:
    """Muestra el estado de los DataNodes del cluster."""
    api, _ = _api()
    try:
        estado = api.cluster_status()
    except DFShaError as error:
        _fallar(error)

    if not estado.nodes:
        console.print("[dim]no hay ningun DataNode registrado[/dim]")
        return

    colores = {"ALIVE": "green", "SUSPECT": "yellow", "DEAD": "red"}

    tabla = Table(
        title=(
            f"DataNodes · R={estado.replication_factor} · "
            f"SUSPECT a los {estado.suspect_after_ms / 1000:.0f}s, "
            f"DEAD a los {estado.dead_after_ms / 1000:.0f}s"
        ),
        box=None,
        pad_edge=False,
    )
    tabla.add_column("estado", width=8)
    tabla.add_column("dominio", style="cyan")
    tabla.add_column("direccion")
    tabla.add_column("usado", justify="right", style="magenta")
    tabla.add_column("libre", justify="right", style="magenta")
    tabla.add_column("bloques", justify="right")
    tabla.add_column("replicas", justify="right", style="dim")
    tabla.add_column("ultimo latido", justify="right")

    for nodo in sorted(estado.nodes, key=lambda n: (n.fault_domain, n.advertise_url)):
        color = colores.get(nodo.state, "white")
        latido = (
            f"{nodo.seconds_since_heartbeat:.0f}s"
            if nodo.seconds_since_heartbeat is not None
            else "nunca"
        )
        # El bloque de mas o de menos entre lo que dice el disco y lo que cree el
        # metadato es divergencia, y verlo aqui ahorra ir a los logs.
        replicas = str(nodo.replica_count)
        if nodo.replica_count != nodo.block_count:
            replicas = f"[yellow]{nodo.replica_count}[/yellow]"

        tabla.add_row(
            f"[{color}]{nodo.state}[/{color}]",
            nodo.fault_domain or "-",
            nodo.advertise_url,
            _humano(nodo.used_bytes),
            _humano(nodo.disk_free_bytes),
            str(nodo.block_count),
            replicas,
            latido,
        )

    console.print(tabla)

    vivos = sum(1 for n in estado.nodes if n.state == "ALIVE")
    dominios = {n.fault_domain for n in estado.nodes if n.state == "ALIVE"}
    console.print(
        f"[dim]{vivos}/{len(estado.nodes)} nodos vivos en {len(dominios)} "
        f"dominios de falla[/dim]"
    )


# --- Transferencia ---------------------------------------------------------


@app.command()
def put(
    local: Path = typer.Argument(..., exists=True, readable=True, help="Archivo local."),
    remote: str = typer.Argument(..., help="Ruta remota de destino."),
    parallel: int = typer.Option(4, "--parallel", help="Bloques simultaneos."),
    block_size: int = typer.Option(
        None, "--block-size", help="Tamano de bloque en bytes (por defecto, el del servidor)."
    ),
) -> None:
    """Sube un archivo: lo particiona, sube los bloques y confirma.

    Tres fases, porque los bloques son inmutables: se reserva el plan, se suben los bytes
    directamente a los DataNodes y solo entonces se confirma. Si algo falla por el
    camino, se aborta la reserva para no dejar el nombre ocupado.
    """
    api, sesion = _api()
    destino = resolve_path(sesion, remote)
    tamano = file_size(local)
    log = get_logger("client")

    try:
        plan = api.create_file(destino, tamano, block_size)
    except DFShaError as error:
        _fallar(error)

    confirmado = False
    try:
        with _barra("subiendo") as (progreso, tarea):
            progreso.update(tarea, total=max(tamano, 1))
            with timed(
                "file.put",
                logger=log,
                file_id=plan.file_id,
                size_bytes=tamano,
                block_count=len(plan.blocks),
                parallel=parallel,
            ):
                upload_blocks(
                    local,
                    plan.blocks,
                    parallel=parallel,
                    on_block=lambda n: progreso.advance(tarea, n),
                )
            if not plan.blocks:
                progreso.advance(tarea, 1)  # archivo vacio: la barra igual se completa

        resultado = api.commit_file(plan.file_id)
        confirmado = True
    except DFShaError as error:
        _fallar(error)
    except Exception as error:  # red, disco, interrupcion
        console.print(f"[red]error[/red] {error}")
        raise typer.Exit(code=1)
    finally:
        if not confirmado:
            # La reserva se libera siempre: sin esto, el nombre quedaria bloqueado hasta
            # que venciera el TTL de escritura.
            try:
                api.abort_file(plan.file_id)
            except Exception:
                pass

    console.print(
        f"[green]subido[/green] {resultado.path} "
        f"({_humano(resultado.size)} en {resultado.block_count} bloques)"
    )


@app.command()
def get(
    remote: str = typer.Argument(..., help="Ruta remota."),
    local: Path = typer.Argument(..., help="Archivo local de destino."),
    parallel: int = typer.Option(4, "--parallel", help="Bloques simultaneos."),
) -> None:
    """Descarga un archivo, verificando el checksum de cada bloque."""
    api, sesion = _api()
    origen = resolve_path(sesion, remote)
    log = get_logger("client")

    try:
        plan = api.open_file(origen)
    except DFShaError as error:
        _fallar(error)

    try:
        with _barra("descargando") as (progreso, tarea):
            progreso.update(tarea, total=max(plan.size, 1))
            with timed(
                "file.get",
                logger=log,
                file_id=plan.file_id,
                size_bytes=plan.size,
                block_count=len(plan.blocks),
                parallel=parallel,
            ):
                download_blocks(
                    local,
                    plan.blocks,
                    parallel=parallel,
                    on_block=lambda n: progreso.advance(tarea, n),
                )
            if not plan.blocks:
                progreso.advance(tarea, 1)
    except DFShaError as error:
        _fallar(error)

    console.print(f"[green]descargado[/green] {local} ({_humano(plan.size)})")


# --- Utilidades ------------------------------------------------------------


def _barra(descripcion: str):
    return _BarraContexto(descripcion)


class _BarraContexto:
    def __init__(self, descripcion: str) -> None:
        self.descripcion = descripcion
        self.progreso = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeElapsedColumn(),
            console=console,
            transient=True,
        )

    def __enter__(self):
        self.progreso.start()
        self.tarea = self.progreso.add_task(self.descripcion, total=None)
        return self.progreso, self.tarea

    def __exit__(self, *exc) -> None:
        self.progreso.stop()


def _humano(n: int) -> str:
    unidades = ["B", "KB", "MB", "GB", "TB"]
    valor = float(n)
    for unidad in unidades:
        if valor < 1024 or unidad == unidades[-1]:
            return f"{valor:.0f} {unidad}" if unidad == "B" else f"{valor:.1f} {unidad}"
        valor /= 1024
    return f"{n} B"


def main() -> None:
    try:
        app()
    except DFShaError as error:  # red de seguridad
        console.print(f"[red]error[/red] {error.message}")
        sys.exit(1)


if __name__ == "__main__":
    main()
