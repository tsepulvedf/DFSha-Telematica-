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

import hashlib

import httpx

from dfsha.common.blocktoken import BLOCK_TOKEN_HEADER
from dfsha.common.errors import DFShaError
from dfsha.common.logging import configure_logging, get_logger, timed

from .api import ControlApi, resolve_path
from .chunker import file_size
from .session import Session, SessionStore
from dfsha.common.crypto import (
    TAG_BYTES,
    DecryptionError,
    FileCrypto,
    derive_master_key,
    new_file_key,
    unwrap_file_key,
    wrap_file_key,
)

from .tls import verificacion_para
from .transfer import PIPELINE_HEADER, download_blocks, upload_blocks

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


def _clave_maestra(sesion: Session) -> bytes | None:
    """La clave con la que cifrar o descifrar. `None` si la sesion no tiene cifrado.

    Si se inicio sesion con `--ask-password`, se pide la contrasena ahora y se deriva sin
    guardarla. La sal viene de la sesion: es la misma con la que se derivo la primera vez,
    y sin ella la clave saldria distinta y no abriria nada.
    """
    if sesion.master_key:
        return bytes.fromhex(sesion.master_key)
    if not sesion.kdf_salt:
        return None  # sesion de una etapa anterior: sin cifrado

    contrasena = typer.prompt("contrasena", hide_input=True)
    return derive_master_key(contrasena, bytes.fromhex(sesion.kdf_salt))


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
    ask_password: bool = typer.Option(
        False,
        "--ask-password",
        help=(
            "No guarda la clave de cifrado en disco; la pide en cada put y get. "
            "Mas seguro y menos comodo."
        ),
    ),
) -> None:
    """Inicia sesion y guarda el token en ~/.dfsha/session.json.

    Tambien deriva la **clave maestra** con la que se cifran tus archivos, a partir de tu
    contrasena y de la sal que devuelve el servidor. Esa clave **no se envia a ninguna
    parte**: el ControlNode y los DataNodes nunca la ven.

    Por defecto se guarda junto al token para que `put` y `get` no tengan que pedir la
    contrasena cada vez. El limite de eso, dicho con precision: el modelo es «el servidor
    nunca ve la clave», no «la clave nunca toca el disco». Con `--ask-password` no se
    guarda y se pide en cada operacion.
    """
    store = _store()
    sesion = store.load(os.environ.get("DFSHA_CONTROL_URL", DEFAULT_CONTROL_URL))
    try:
        token = ControlApi(sesion).login(username, password)
    except DFShaError as error:
        _fallar(error)

    sesion.token = token.access_token
    sesion.username = username
    sesion.cwd = "/"
    sesion.kdf_salt = token.kdf_salt
    sesion.master_key = (
        None
        if ask_password or not token.kdf_salt
        else derive_master_key(password, bytes.fromhex(token.kdf_salt)).hex()
    )
    try:
        store.save(sesion)
    except DFShaError as error:
        _fallar(error)
    console.print(f"[green]sesion iniciada como[/green] {username}")
    # Decir donde quedo guardada: si el siguiente comando se queja de que no hay sesion,
    # esta linea es la mitad del diagnostico. Pasa dentro de contenedores, cuando el
    # directorio no es el que persiste.
    console.print(f"[dim]sesion guardada en {store.path}[/dim]")
    if sesion.master_key:
        console.print(
            "[dim]tus archivos se cifran en este equipo antes de subirse; la clave no "
            "sale de aqui[/dim]"
        )
    elif token.kdf_salt:
        console.print(
            "[dim]la clave no se guardo: se pedira la contrasena en cada put y get"
            "[/dim]"
        )
    else:
        # Antes esto no decia nada, y la sesion quedaba sin cifrado sin que nadie lo
        # supiera. Ahora `put` se niega, y aqui se avisa de por que antes de que ocurra.
        console.print(
            "[yellow]aviso[/yellow] el servidor no devolvio sal de cifrado para este "
            "usuario: no podras subir archivos hasta que el ControlNode aplique la "
            "migracion 0008"
        )


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

    # La clave del archivo se genera AQUI, se envuelve con la maestra, y lo que viaja al
    # servidor es la envoltura. Una clave por archivo y no una por usuario: compartir un
    # archivo puede llegar a ser entregar su clave sin dar acceso a todo lo demas.
    maestra = _clave_maestra(sesion)
    if maestra is None:
        # **Nunca se sube en claro.** Esta rama subia el archivo SIN cifrar y sin decir
        # nada, y era la que tomaba cualquier usuario anterior a la migracion 0006: su sal
        # estaba vacia, el login no derivaba clave, y el criterio 11 dejaba de cumplirse en
        # silencio. «Archivo sin cifrar» sigue siendo un estado LEGIBLE —los de las Etapas
        # 1 y 2 se bajan igual—, pero ya no es algo que este cliente produzca.
        console.print(
            "[red]error[/red] esta sesion no tiene clave de cifrado, y el cliente no sube "
            "archivos en claro"
        )
        console.print(
            "[dim]vuelve a iniciar sesion con `dfsha login`. Si el error se repite, tu "
            "usuario no tiene sal de cifrado: el ControlNode necesita la migracion 0008 "
            "(`docker compose up` la aplica con el servicio migrate)[/dim]"
        )
        raise typer.Exit(code=1)
    clave_archivo = new_file_key()

    try:
        # El sobrecoste del cifrado viaja en la creacion, no en el commit: el ControlNode
        # planifica AHI los bloques y necesita saber cuanto ocupara cada uno de verdad.
        plan = api.create_file(
            destino,
            tamano,
            block_size,
            cipher_overhead=TAG_BYTES if clave_archivo is not None else 0,
        )
    except DFShaError as error:
        _fallar(error)

    # La envoltura se calcula DESPUES de crear, porque el `file_id` entra en ella como
    # dato autenticado y ata la clave a SU archivo. Y viaja en el COMMIT, no en un
    # endpoint aparte: es metadato que tiene que quedar durable justo cuando el archivo
    # se hace visible, que es la definicion del commit. Un viaje menos y un endpoint
    # menos que proteger.
    cripto = None
    envoltura = ""
    if clave_archivo is not None:
        envoltura = wrap_file_key(clave_archivo, maestra, plan.file_id).hex()
        cripto = FileCrypto(file_id=plan.file_id, file_key=clave_archivo)

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
                    crypto=cripto,
                )
            if not plan.blocks:
                progreso.advance(tarea, 1)  # archivo vacio: la barra igual se completa

        resultado = api.commit_file(plan.file_id, wrapped_key=envoltura)
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

    # `wrapped_key` vacia = archivo SIN cifrar. Se mira el campo en vez de suponerlo, que
    # es lo que permite que los archivos de las Etapas 1 y 2 se sigan pudiendo bajar.
    cripto = None
    if plan.wrapped_key:
        maestra = _clave_maestra(sesion)
        if maestra is None:
            console.print(
                "[red]error[/red] este archivo esta cifrado y esta sesion no tiene "
                "clave; vuelve a iniciar sesion"
            )
            raise typer.Exit(code=1)
        try:
            clave = unwrap_file_key(
                bytes.fromhex(plan.wrapped_key), maestra, plan.file_id
            )
        except DecryptionError:
            console.print(
                "[red]error[/red] no se pudo abrir la clave del archivo. Si cambiaste "
                "de contrasena, los archivos cifrados con la anterior no se recuperan."
            )
            raise typer.Exit(code=1)
        cripto = FileCrypto(file_id=plan.file_id, file_key=clave)

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
                    crypto=cripto,
                    plain_size=plan.size,
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


# --- RF3: lock / read por rango / append -----------------------------------
#
# El titular de un lock es una SESION, no una persona: Ana desde dos maquinas tiene que
# poder excluirse a si misma. El identificador de sesion se guarda junto al token, y por
# eso `dfsha lock` y `dfsha append` funcionan entre invocaciones distintas del CLI sin
# que el usuario tenga que pasarlo a mano.


@app.command()
def lock(
    path: str,
    shared: bool = typer.Option(
        False, "--shared", help="Compartido (varios lectores) en vez de exclusivo."
    ),
) -> None:
    """Bloquea un archivo. Vuelve a ejecutarlo para RENOVAR el lease.

    El bloqueo vence solo: si este proceso muere sin soltarlo, el archivo queda libre
    pasado el plazo. Eso es lo que evita que un cliente caido deje un archivo bloqueado
    para siempre, y el precio es que un cliente muy lento puede perderlo sin haberse
    caido.
    """
    api, sesion = _api()
    try:
        concedido = api.lock(
            resolve_path(sesion, path),
            mode="shared" if shared else "exclusive",
            holder=sesion.holder_id,
        )
    except DFShaError as error:
        _fallar(error)

    sesion.locks[concedido.file_id] = concedido.epoch
    _store().save(sesion)
    console.print(
        f"bloqueado [bold]{path}[/bold] en modo {concedido.mode} "
        f"(epoca {concedido.epoch}, {concedido.lease_seconds:.0f}s)"
    )
    console.print(
        "[dim]renueva con el mismo comando antes de que venza; suelta con "
        "'dfsha unlock'[/dim]"
    )


@app.command()
def unlock(path: str) -> None:
    """Suelta el bloqueo. Soltar lo que ya no se tiene tambien vale."""
    api, sesion = _api()
    try:
        api.unlock(resolve_path(sesion, path), holder=sesion.holder_id)
    except DFShaError as error:
        _fallar(error)
    console.print(f"desbloqueado [bold]{path}[/bold]")


@app.command()
def locks(path: str) -> None:
    """Quien tiene bloqueado un archivo."""
    api, sesion = _api()
    try:
        vista = api.locks(resolve_path(sesion, path))
    except DFShaError as error:
        _fallar(error)

    if not vista.holders:
        console.print(f"[dim]{path}: sin bloqueos[/dim]")
        return

    tabla = Table(title=f"bloqueos de {path}", box=None)
    tabla.add_column("titular")
    tabla.add_column("modo")
    tabla.add_column("epoca", justify="right")
    tabla.add_column("vence")
    for h in vista.holders:
        tabla.add_row(h.holder, h.mode, str(h.epoch), h.expires_at.isoformat(" ", "seconds"))
    console.print(tabla)


@app.command()
def read(
    path: str,
    offset: int = typer.Option(0, "--offset", "-o", help="Byte donde empezar."),
    length: int = typer.Option(None, "--length", "-n", help="Cuantos bytes leer."),
) -> None:
    """Lee un TRAMO de un archivo y lo escribe por la salida estandar.

    Solo se descargan los bloques que tocan el tramo: leer 1 KB del final de un archivo de
    1 GB baja un bloque, no el archivo entero.

    **El bloque se baja entero aunque solo se quiera un byte de el.** Con cifrado no hay
    alternativa: la etiqueta de AES-GCM cubre el bloque completo, asi que no se puede
    descifrar ni verificar un tramo suelto.
    """
    api, sesion = _api()
    ruta = resolve_path(sesion, path)
    try:
        rango = api.read_range(ruta, offset=offset, length=length)
        plan = api.open_file(ruta)
    except DFShaError as error:
        _fallar(error)

    cripto = None
    if plan.wrapped_key:
        maestra = _clave_maestra(sesion)
        if maestra is None:
            console.print("[red]error[/red] archivo cifrado y sesion sin clave")
            raise typer.Exit(code=1)
        cripto = FileCrypto(
            file_id=plan.file_id,
            file_key=unwrap_file_key(bytes.fromhex(plan.wrapped_key), maestra, plan.file_id),
        )

    salida = sys.stdout.buffer
    for bloque in rango.blocks:
        crudo = _bajar_bloque_suelto(bloque)
        claro = cripto.decrypt(crudo, bloque.index) if cripto else crudo
        salida.write(claro[bloque.skip : bloque.skip + bloque.take])
    salida.flush()


def _bajar_bloque_suelto(bloque) -> bytes:
    """Un bloque de una replica, probandolas por orden. Mismo relevo que `download_blocks`.

    Vive aparte porque la lectura por rango no escribe a un archivo destino: devuelve los
    bytes para recortarlos en memoria. Reusar `download_blocks` obligaria a inventar un
    fichero temporal para tirarlo despues.
    """
    fallos: list[str] = []
    for replica in bloque.replicas:
        try:
            destino = f"{replica.base_url.rstrip('/')}/api/v1/blocks/{bloque.block_id}"
            respuesta = httpx.get(
                destino,
                headers=({BLOCK_TOKEN_HEADER: bloque.token} if bloque.token else {}),
                timeout=120,
                verify=verificacion_para(destino),
            )
            if respuesta.status_code == 200:
                return respuesta.content
            fallos.append(f"{replica.data_node_id[:8]}: HTTP {respuesta.status_code}")
        except httpx.HTTPError as exc:
            fallos.append(f"{replica.data_node_id[:8]}: {type(exc).__name__}")

    console.print(f"[red]error[/red] no se pudo leer el bloque {bloque.block_id}")
    for f in fallos:
        console.print(f"  [dim]{f}[/dim]")
    raise typer.Exit(code=1)


@app.command()
def append(remoto: str, local: str) -> None:
    """Anade el contenido de un archivo local al FINAL de uno remoto. El `write` del RF3.

    Los bloques son inmutables, asi que si el ultimo del archivo remoto esta a medias hay
    que reescribirlo: se baja, se le pegan los bytes nuevos y se sube con otro `block_id`.
    El viejo queda para el GC.

    **La reescritura la hace este cliente, no el servidor**, y no por comodidad: con
    cifrado extremo a extremo el servidor no podria, porque concatenar exige descifrar y
    la clave no sale de aqui.
    """
    origen = Path(local)
    if not origen.is_file():
        console.print(f"[red]error[/red] no existe el archivo local {local}")
        raise typer.Exit(code=1)

    nuevos = origen.read_bytes()
    if not nuevos:
        console.print("[dim]el archivo local esta vacio: no hay nada que anadir[/dim]")
        return

    api, sesion = _api()
    ruta = resolve_path(sesion, remoto)
    try:
        actual = api.open_file(ruta)
    except DFShaError as error:
        _fallar(error)

    cripto = None
    maestra = None
    if actual.wrapped_key:
        maestra = _clave_maestra(sesion)
        if maestra is None:
            console.print("[red]error[/red] archivo cifrado y sesion sin clave")
            raise typer.Exit(code=1)
        cripto = FileCrypto(
            file_id=actual.file_id,
            file_key=unwrap_file_key(
                bytes.fromhex(actual.wrapped_key), maestra, actual.file_id
            ),
        )

    epoca = sesion.locks.get(actual.file_id, 0)
    try:
        plan = api.append(
            actual.file_id,
            len(nuevos),
            cipher_overhead=TAG_BYTES if cripto else 0,
            lock_holder=sesion.holder_id if epoca else "",
            lock_epoch=epoca,
        )
    except DFShaError as error:
        _fallar(error)

    # La cola, si la hay: bajarla, descifrarla y pegarle lo nuevo delante.
    cabeza = b""
    if plan.tail is not None:
        crudo = _bajar_bloque_suelto(plan.tail)
        cabeza = cripto.decrypt(crudo, plan.tail.index) if cripto else crudo

    contenido = cabeza + nuevos
    for bloque in plan.blocks:
        inicio = (bloque.index - plan.blocks[0].index) * plan.block_size
        claro = contenido[inicio : inicio + plan.block_size]
        cuerpo = cripto.encrypt(claro, bloque.index) if cripto else claro
        _subir_bloque_suelto(bloque, cuerpo)

    try:
        resultado = api.commit_append(
            actual.file_id,
            [b.block_id for b in plan.blocks],
            actual.size + len(nuevos),
            replaces=plan.tail.block_id if plan.tail else "",
            lock_holder=sesion.holder_id if epoca else "",
            lock_epoch=epoca,
        )
    except DFShaError as error:
        _fallar(error)

    console.print(
        f"anadidos [bold]{len(nuevos)}[/bold] B a {remoto} "
        f"({resultado.size} B en {resultado.block_count} bloques)"
    )
    if plan.tail is not None:
        console.print(
            f"[dim]se reescribio el bloque de cola ({plan.tail_plain_size} B): los "
            "bloques son inmutables, asi que llenarlo es escribirlo de nuevo[/dim]"
        )


def _subir_bloque_suelto(bloque, cuerpo: bytes) -> None:
    destino = f"{bloque.replicas[0].base_url.rstrip('/')}/api/v1/blocks/{bloque.block_id}"
    respuesta = httpx.put(
        destino,
        content=cuerpo,
        headers={
            "X-DFSha-Checksum": hashlib.sha256(cuerpo).hexdigest(),
            "Content-Type": "application/octet-stream",
            **({PIPELINE_HEADER: ",".join(bloque.pipeline)} if bloque.pipeline else {}),
            **({BLOCK_TOKEN_HEADER: bloque.token} if bloque.token else {}),
        },
        timeout=300,
        verify=verificacion_para(destino),
    )
    if respuesta.status_code != 201:
        console.print(
            f"[red]error[/red] el DataNode rechazo el bloque: HTTP "
            f"{respuesta.status_code} {respuesta.text[:200]}"
        )
        raise typer.Exit(code=1)
