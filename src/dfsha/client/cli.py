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
    almacen = _store()
    sesion = almacen.load(os.environ.get("DFSHA_CONTROL_URL", DEFAULT_CONTROL_URL))
    # El almacen viaja con la API para que el LSN de la ultima escritura se persista en
    # cuanto llega: cada invocacion de `dfsha` es un proceso nuevo.
    return ControlApi(sesion, store=almacen), sesion


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
        if info.replication_state:
            tabla.add_row("replicacion", _replicacion(info))
    tabla.add_row("creado", info.created_at.isoformat())
    console.print(tabla)


def _replicacion(info) -> str:
    """El estado de replicacion con el conteo real detras.

    El conteo importa tanto como el estado: `UNDER_REPLICATED` a secas no distingue un
    archivo al que le falta la tercera copia (tolera una caida mas) de uno que se quedo
    con una sola (esta a un fallo de perderse). Cuando los bloques del archivo no estan
    todos igual, se muestra el rango, para que una re-replicacion en curso se vea
    avanzar en vez de parecer estancada.
    """
    colores = {
        "FULLY_REPLICATED": "green",
        "UNDER_REPLICATED": "yellow",
        "UNAVAILABLE": "red",
    }
    color = colores.get(info.replication_state, "white")
    minimo, maximo = info.min_replicas, info.max_replicas
    copias = str(minimo) if minimo == maximo else f"{minimo}-{maximo}"
    return (
        f"[{color}]{info.replication_state}[/{color}] "
        f"[dim]({copias} de {info.replication_factor} copias por bloque)[/dim]"
    )


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
        # El liderazgo se muestra igualmente: un cluster sin DataNodes es justo cuando
        # mas falta hace saber si el plano de control esta en pie.
        _mostrar_liderazgo(api)
        return

    colores = {"ALIVE": "green", "SUSPECT": "yellow", "DEAD": "red"}

    tabla = Table(
        title=(
            f"DataNodes · R={estado.replication_factor} W={estado.write_quorum} · "
            f"SUSPECT a los {estado.suspect_after_ms / 1000:.0f}s, "
            f"DEAD a los {estado.dead_after_ms / 1000:.0f}s"
        ),
        box=None,
        pad_edge=False,
    )
    tabla.add_column("estado", width=7)
    tabla.add_column("dominio", style="cyan")
    # `fold` y no truncar: la direccion es justo el dato que hace falta para ir a
    # hablar con el nodo, y un "http://12..." no sirve de nada.
    tabla.add_column("direccion", overflow="fold")
    tabla.add_column("usado", justify="right", style="magenta")
    tabla.add_column("cap.", justify="right", style="magenta")
    tabla.add_column("libre", justify="right", style="magenta")
    tabla.add_column("bloq.", justify="right")
    tabla.add_column("repl.", justify="right", style="dim")
    tabla.add_column("latido", justify="right")

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
            _humano(nodo.capacity_bytes),
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
    _mostrar_replicacion(estado)
    _mostrar_liderazgo(api)


def _mostrar_replicacion(estado) -> None:
    """Cuantos bloques del cluster entero van cortos de copias.

    Los criticos se separan de los sub-replicados porque no cuestan lo mismo: un bloque
    con dos copias de tres todavia tolera una caida; uno con una sola esta a un fallo de
    desaparecer, y por eso va antes en la cola de re-replicacion.
    """
    if estado.under_replicated_blocks == 0:
        console.print("[green]todos los bloques con sus copias completas[/green]")
        return

    linea = (
        f"[yellow]{estado.under_replicated_blocks} bloques sub-replicados[/yellow] "
        f"[dim](menos de {estado.replication_factor} copias)[/dim]"
    )
    if estado.critical_blocks:
        linea += (
            f" · [red]{estado.critical_blocks} con UNA sola copia[/red]"
            f" [dim]— a un fallo de perderse[/dim]"
        )
    console.print(linea)


def _mostrar_liderazgo(api: ControlApi) -> None:
    """Quien sostiene el lease del ControlNode.

    Se pide aparte y su fallo no tumba el comando: `dfsha cluster` existe desde la
    Etapa 2 para ver los DataNodes, y perder esa informacion porque el endpoint nuevo
    devuelva un error seria un mal cambio.

    La epoca se muestra siempre, y no solo cuando cambia, porque es el numero que hay
    que mirar en la demostracion: al matar al lider, el siguiente tiene que salir con
    exactamente una mas.
    """
    try:
        lease = api.leadership()
    except DFShaError as error:
        console.print(f"[dim]liderazgo: no disponible ({error.message})[/dim]")
        return

    if lease.leader_id is None:
        console.print(
            "[yellow]sin lider[/yellow] [dim]— el lease esta libre; la siguiente "
            f"instancia que lo tome ira con epoca {lease.epoch + 1}[/dim]"
        )
        return

    quien = "esta instancia" if lease.is_self else f"otra instancia ({lease.leader_id[:8]})"
    console.print(
        f"[dim]lider:[/dim] {quien} [dim]· epoca[/dim] {lease.epoch} "
        f"[dim]· le quedan[/dim] {lease.expires_in_seconds:.1f}s "
        f"[dim]· te atendio[/dim] {lease.instance_id[:8]}"
    )


# --- Permisos --------------------------------------------------------------

grupo_app = typer.Typer(help="Grupos planos para compartir con varias personas a la vez.")
app.add_typer(grupo_app, name="group")


@grupo_app.command("create")
def group_create(name: str) -> None:
    """Crea un grupo. El nombre es tuyo: otro usuario puede tener uno igual."""
    api, _ = _api()
    try:
        api.create_group(name)
    except DFShaError as error:
        _fallar(error)
    console.print(f"grupo [cyan]{name}[/cyan] creado")


@grupo_app.command("add")
def group_add(name: str, username: str) -> None:
    """Anade un usuario al grupo."""
    api, _ = _api()
    try:
        api.add_member(name, username)
    except DFShaError as error:
        _fallar(error)
    console.print(f"[green]{username}[/green] anadido a [cyan]{name}[/cyan]")


@grupo_app.command("remove")
def group_remove(name: str, username: str) -> None:
    """Quita un usuario del grupo."""
    api, _ = _api()
    try:
        api.remove_member(name, username)
    except DFShaError as error:
        _fallar(error)
    console.print(f"[yellow]{username}[/yellow] quitado de [cyan]{name}[/cyan]")


@grupo_app.command("ls")
def group_ls() -> None:
    """Tus grupos y quien esta en cada uno."""
    api, _ = _api()
    try:
        respuesta = api.list_groups()
    except DFShaError as error:
        _fallar(error)

    if not respuesta.groups:
        console.print("[dim]no tienes ningun grupo[/dim]")
        return

    tabla = Table(box=None, pad_edge=False)
    tabla.add_column("grupo", style="cyan")
    tabla.add_column("miembros")
    for grupo in respuesta.groups:
        tabla.add_row(grupo.name, ", ".join(grupo.members) or "[dim](vacio)[/dim]")
    console.print(tabla)


@app.command()
def share(path: str, principal: str, permission: str) -> None:
    """Comparte un directorio con un usuario o grupo: read, write o admin."""
    api, sesion = _api()
    try:
        api.share(resolve_path(sesion, path), principal, permission)
    except DFShaError as error:
        _fallar(error)
    console.print(
        f"[green]{principal}[/green] tiene ahora [bold]{permission.upper()}[/bold] "
        f"sobre {path}"
    )


@app.command()
def unshare(path: str, principal: str) -> None:
    """Quita el acceso de un usuario o grupo.

    Lo que ya se descargo no se recupera: revocar corta el acceso futuro, no deshace el
    pasado. Es lo que pasa con cualquier sistema de permisos.
    """
    api, sesion = _api()
    try:
        api.unshare(resolve_path(sesion, path), principal)
    except DFShaError as error:
        _fallar(error)
    console.print(f"[yellow]{principal}[/yellow] ya no tiene acceso a {path}")


@app.command()
def acl(path: str) -> None:
    """Que puedes hacer aqui, y quien mas tiene acceso."""
    api, sesion = _api()
    try:
        vista = api.acl(resolve_path(sesion, path))
    except DFShaError as error:
        _fallar(error)

    origen = {
        "owner": "eres el dueno",
        "user": "concedido a ti",
        "group": "por un grupo tuyo",
    }.get(vista.source or "", vista.source or "")

    detalle = f" [dim]({origen}"
    if vista.inherited_from:
        detalle += f", heredado de {vista.inherited_from}"
    detalle += ")[/dim]"

    if vista.effective:
        console.print(f"tu permiso: [bold]{vista.effective}[/bold]{detalle}")
    else:
        console.print("[red]sin permiso[/red]")

    if not vista.grants:
        console.print("[dim]sin concesiones puestas en esta ruta[/dim]")
        return

    tabla = Table(title="concesiones en esta ruta", box=None, pad_edge=False)
    tabla.add_column("quien", style="cyan")
    tabla.add_column("tipo", style="dim")
    tabla.add_column("permiso")
    tabla.add_column("concedido por", style="dim")
    for concesion in vista.grants:
        tabla.add_row(
            concesion.principal,
            concesion.principal_type.lower(),
            concesion.permission,
            concesion.granted_by,
        )
    console.print(tabla)


@app.command("shared")
def shared() -> None:
    """Lo que otros comparten contigo. Vive en /compartido-conmigo, aparte de tu arbol."""
    api, _ = _api()
    try:
        respuesta = api.shared_with_me()
    except DFShaError as error:
        _fallar(error)

    if not respuesta.entries:
        console.print("[dim]nadie te ha compartido nada[/dim]")
        return

    tabla = Table(box=None, pad_edge=False)
    tabla.add_column("ruta", overflow="fold")
    tabla.add_column("de", style="cyan")
    tabla.add_column("permiso")
    tabla.add_column("por", style="dim")
    for entrada in respuesta.entries:
        tabla.add_row(
            entrada.path,
            entrada.owner,
            entrada.permission,
            f"grupo {entrada.via_group}" if entrada.via_group else "directo",
        )
    console.print(tabla)


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
