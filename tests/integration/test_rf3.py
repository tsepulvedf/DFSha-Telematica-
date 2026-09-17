"""RF3: `open`, `read` por rango, `write` como append, y `lock` con lease.

La prueba que da sentido al fichero es `test_el_cliente_congelado_no_escribe_encima`. Es
el mismo escenario del lider congelado del Bloque A con un cliente en vez de un
ControlNode, y tiene la misma respuesta: la epoca viaja con la operacion y se verifica
dentro de la transaccion que escribe. Que dos problemas que no se parecen se resuelvan
igual es lo que hace que el diseno sea coherente.
"""

from __future__ import annotations

import hashlib

import httpx
import pytest

from dfsha.client.api import ControlApi
from dfsha.client.session import Session
from dfsha.common.blocktoken import BLOCK_TOKEN_HEADER

from .cluster import MB, Cluster, start_cluster


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    c = start_cluster(tmp_path_factory.mktemp("rf3"), block_size=MB, data_nodes=1)
    try:
        yield c
    finally:
        c.stop()


def _usuario(cluster: Cluster, nombre: str) -> ControlApi:
    sesion = Session(control_url=cluster.control_url)
    api = ControlApi(sesion)
    api.register(nombre, "contrasena-de-prueba")
    sesion.token = api.login(nombre, "contrasena-de-prueba").access_token
    sesion.username = nombre
    return api


@pytest.fixture()
def ana(cluster, request) -> ControlApi:
    return _usuario(cluster, f"ana-{request.node.name[:22]}")


@pytest.fixture()
def beto(cluster, request) -> ControlApi:
    return _usuario(cluster, f"beto-{request.node.name[:22]}")


def _subir(api: ControlApi, ruta: str, datos: bytes) -> str:
    """Sube un archivo pequeno de un bloque y devuelve su file_id."""
    plan = api.create_file(ruta, len(datos))
    bloque = plan.blocks[0]
    respuesta = httpx.put(
        f"{bloque.replicas[0].base_url}/api/v1/blocks/{bloque.block_id}",
        content=datos,
        headers={
            "X-DFSha-Checksum": hashlib.sha256(datos).hexdigest(),
            BLOCK_TOKEN_HEADER: bloque.token,
        },
        timeout=30,
    )
    assert respuesta.status_code == 201
    api.commit_file(plan.file_id)
    return plan.file_id


def _bajar_bloque(bloque) -> bytes:
    respuesta = httpx.get(
        f"{bloque.replicas[0].base_url}/api/v1/blocks/{bloque.block_id}",
        headers={BLOCK_TOKEN_HEADER: bloque.token},
        timeout=30,
    )
    assert respuesta.status_code == 200, respuesta.text
    return respuesta.content


def _escribir_bloque(bloque, datos: bytes) -> None:
    respuesta = httpx.put(
        f"{bloque.replicas[0].base_url}/api/v1/blocks/{bloque.block_id}",
        content=datos,
        headers={
            "X-DFSha-Checksum": hashlib.sha256(datos).hexdigest(),
            BLOCK_TOKEN_HEADER: bloque.token,
        },
        timeout=30,
    )
    assert respuesta.status_code == 201, respuesta.text


# --- lock -------------------------------------------------------------------


def test_un_lock_exclusivo_excluye_a_otro(ana, beto) -> None:
    ana.mkdir("/compartido", parents=True)
    _subir(ana, "/compartido/doc.txt", b"contenido inicial")
    ana.share("/compartido", beto.session.username, "write")
    ruta_beto = f"/compartido-conmigo/{ana.session.username}/compartido/doc.txt"

    ana.lock("/compartido/doc.txt")

    with pytest.raises(Exception) as fallo:
        beto.lock(ruta_beto)
    assert getattr(fallo.value, "code", "") == "file_locked"


def test_el_conflicto_dice_cuanto_falta(ana, beto) -> None:
    """«Esta bloqueado» sin decir hasta cuando es un mensaje con el que no se puede hacer
    nada. Con `retry_after_seconds` el cliente puede decidir si esperar."""
    ana.mkdir("/c2", parents=True)
    _subir(ana, "/c2/doc.txt", b"x")
    ana.share("/c2", beto.session.username, "write")
    ana.lock("/c2/doc.txt")

    with pytest.raises(Exception) as fallo:
        beto.lock(f"/compartido-conmigo/{ana.session.username}/c2/doc.txt")

    detalle = getattr(fallo.value, "details", {}) or {}
    assert float(detalle.get("retry_after_seconds", 0)) > 0


def test_varios_COMPARTIDOS_conviven(ana, beto) -> None:
    ana.mkdir("/lectura", parents=True)
    _subir(ana, "/lectura/doc.txt", b"para leer")
    ana.share("/lectura", beto.session.username, "read")

    ana.lock("/lectura/doc.txt", mode="shared")
    beto.lock(
        f"/compartido-conmigo/{ana.session.username}/lectura/doc.txt", mode="shared"
    )

    assert len(ana.locks("/lectura/doc.txt").holders) == 2


def test_pedir_el_mismo_lock_otra_vez_lo_RENUEVA(ana) -> None:
    """Un cliente que reintenta tras un timeout de red no puede saber si el primero
    llego. Que el reintento falle convertiria un reintento correcto en un error."""
    _subir(ana, "/renovable.txt", b"x")

    primero = ana.lock("/renovable.txt")
    segundo = ana.lock("/renovable.txt")

    assert segundo.epoch > primero.epoch  # cada concesion es un episodio nuevo
    assert len(ana.locks("/renovable.txt").holders) == 1


def test_soltar_libera_para_otro(ana, beto) -> None:
    ana.mkdir("/suelta", parents=True)
    _subir(ana, "/suelta/doc.txt", b"x")
    ana.share("/suelta", beto.session.username, "write")
    ruta_beto = f"/compartido-conmigo/{ana.session.username}/suelta/doc.txt"

    ana.lock("/suelta/doc.txt")
    ana.unlock("/suelta/doc.txt")

    assert beto.lock(ruta_beto).holder  # ya no estorba


def test_soltar_lo_que_no_se_tiene_no_es_un_error(ana) -> None:
    """Idempotente: un cliente que reintenta el cierre no debe recibir un fallo."""
    _subir(ana, "/nunca-bloqueado.txt", b"x")

    ana.unlock("/nunca-bloqueado.txt")
    ana.unlock("/nunca-bloqueado.txt")


# --- open -------------------------------------------------------------------


def test_open_devuelve_el_handle_y_bloquea_a_la_vez(ana) -> None:
    """Atomico a proposito: con dos llamadas, entre abrir y bloquear otro cliente puede
    tomar el lock y el primero creeria tener en exclusiva algo que ya no es suyo."""
    datos = b"contenido del handle"
    _subir(ana, "/handle.txt", datos)

    handle = ana.open_handle("/handle.txt", mode="write", lock=True)

    assert handle.size == len(datos)
    assert handle.lock is not None
    assert handle.lock.mode == "exclusive"
    assert ana.locks("/handle.txt").holders


def test_open_en_lectura_no_exige_permiso_de_escritura(ana, beto) -> None:
    ana.mkdir("/solo-lectura", parents=True)
    _subir(ana, "/solo-lectura/doc.txt", b"visible")
    ana.share("/solo-lectura", beto.session.username, "read")

    handle = beto.open_handle(
        f"/compartido-conmigo/{ana.session.username}/solo-lectura/doc.txt", mode="read"
    )

    assert handle.size == len(b"visible")


def test_open_en_escritura_SI_lo_exige(ana, beto) -> None:
    ana.mkdir("/ro", parents=True)
    _subir(ana, "/ro/doc.txt", b"visible")
    ana.share("/ro", beto.session.username, "read")

    with pytest.raises(Exception):
        beto.open_handle(
            f"/compartido-conmigo/{ana.session.username}/ro/doc.txt",
            mode="write",
            lock=True,
        )


# --- read por rango ---------------------------------------------------------


def test_un_rango_pide_solo_los_bloques_que_lo_tocan(ana) -> None:
    """El ahorro real de la lectura por rango: no bajar lo que no hace falta."""
    datos = bytes((i * 7 + 3) % 256 for i in range(3 * MB))
    plan = ana.create_file("/grande.bin", len(datos))
    for bloque in plan.blocks:
        trozo = datos[bloque.index * MB : (bloque.index + 1) * MB]
        _escribir_bloque(bloque, trozo)
    ana.commit_file(plan.file_id)

    rango = ana.read_range("/grande.bin", offset=2 * MB + 10, length=50)

    assert [b.index for b in rango.blocks] == [2]
    assert rango.blocks[0].skip == 10
    assert rango.blocks[0].take == 50


def test_un_rango_a_caballo_pide_los_dos_bloques(ana) -> None:
    datos = bytes((i * 11 + 5) % 256 for i in range(2 * MB))
    plan = ana.create_file("/caballo.bin", len(datos))
    for bloque in plan.blocks:
        _escribir_bloque(bloque, datos[bloque.index * MB : (bloque.index + 1) * MB])
    ana.commit_file(plan.file_id)

    rango = ana.read_range("/caballo.bin", offset=MB - 5, length=10)

    assert [b.index for b in rango.blocks] == [0, 1]
    assert (rango.blocks[0].skip, rango.blocks[0].take) == (MB - 5, 5)
    assert (rango.blocks[1].skip, rango.blocks[1].take) == (0, 5)


def test_el_rango_devuelve_los_bytes_correctos(ana) -> None:
    """De punta a punta: se pide un tramo y se comprueba contra el original."""
    datos = bytes((i * 13 + 1) % 256 for i in range(2 * MB))
    plan = ana.create_file("/exacto.bin", len(datos))
    for bloque in plan.blocks:
        _escribir_bloque(bloque, datos[bloque.index * MB : (bloque.index + 1) * MB])
    ana.commit_file(plan.file_id)

    inicio, largo = MB - 100, 300
    rango = ana.read_range("/exacto.bin", offset=inicio, length=largo)

    reconstruido = b"".join(
        _bajar_bloque(b)[b.skip : b.skip + b.take] for b in rango.blocks
    )
    assert reconstruido == datos[inicio : inicio + largo]


def test_pedir_mas_alla_del_final_se_recorta(ana) -> None:
    """Y no falla: es lo que hace `read(2)` de toda la vida, devolver lo que haya."""
    _subir(ana, "/corto.txt", b"0123456789")

    rango = ana.read_range("/corto.txt", offset=5, length=1000)

    assert rango.length == 5
    assert sum(b.take for b in rango.blocks) == 5


def test_un_rango_vacio_no_pide_ningun_bloque(ana) -> None:
    _subir(ana, "/vacio-rango.txt", b"0123456789")

    assert ana.read_range("/vacio-rango.txt", offset=10, length=10).blocks == []


# --- write (append) ---------------------------------------------------------


def test_anadir_a_un_bloque_a_medias_lo_REESCRIBE(ana) -> None:
    """Los bloques son inmutables, asi que llenar el ultimo es reescribirlo con OTRO
    `block_id` en el mismo indice. Copy-on-write, la decision 1 aplicada a un bloque."""
    inicial = b"primera linea\n"
    file_id = _subir(ana, "/diario.txt", inicial)
    viejo = ana.open_file("/diario.txt").blocks[0].block_id

    anadido = b"segunda linea\n"
    plan = ana.append(file_id, len(anadido))

    assert plan.tail is not None, "el bloque de cola tenia que reescribirse"
    assert plan.tail.block_id == viejo
    assert plan.tail_plain_size == len(inicial)
    assert len(plan.blocks) == 1
    assert plan.blocks[0].block_id != viejo  # id NUEVO, mismo indice


def test_el_append_completo_deja_el_archivo_bien(ana) -> None:
    """De punta a punta, haciendo lo que haria el cliente: bajar la cola, pegarle los
    bytes nuevos, y subir el resultado."""
    inicial = b"primera linea\n"
    file_id = _subir(ana, "/completo.txt", inicial)

    anadido = b"segunda linea\n"
    plan = ana.append(file_id, len(anadido))

    cola = _bajar_bloque(plan.tail)
    _escribir_bloque(plan.blocks[0], cola + anadido)
    ana.commit_append(
        file_id,
        [b.block_id for b in plan.blocks],
        len(inicial) + len(anadido),
        replaces=plan.tail.block_id,
    )

    leido = ana.open_file("/completo.txt")
    assert leido.size == len(inicial) + len(anadido)
    assert _bajar_bloque(leido.blocks[0]) == inicial + anadido


def test_el_bloque_de_cola_viejo_queda_para_el_GC(ana) -> None:
    """No se borra —los bloques son inmutables y borrar bytes es cosa del GC— pero deja
    de pertenecer al archivo. Es el tercer caso de huerfano: un bloque SIN archivo."""
    inicial = b"algo\n"
    file_id = _subir(ana, "/huerfano.txt", inicial)
    viejo = ana.open_file("/huerfano.txt").blocks[0].block_id

    anadido = b"mas!\n"
    plan = ana.append(file_id, len(anadido))
    _escribir_bloque(plan.blocks[0], _bajar_bloque(plan.tail) + anadido)
    ana.commit_append(
        file_id,
        [b.block_id for b in plan.blocks],
        len(inicial) + len(anadido),
        replaces=viejo,
    )

    assert all(b.block_id != viejo for b in ana.open_file("/huerfano.txt").blocks)


def test_anadir_sobre_un_bloque_LLENO_no_reescribe_nada(ana) -> None:
    """El caso barato: si el archivo acaba justo en el limite de bloque, anadir solo
    escribe bloques nuevos y no hay amplificacion ninguna."""
    datos = bytes(MB)
    plan = ana.create_file("/lleno.bin", len(datos))
    _escribir_bloque(plan.blocks[0], datos)
    ana.commit_file(plan.file_id)

    append = ana.append(plan.file_id, 10)

    assert append.tail is None
    assert append.tail_plain_size == 0
    assert append.blocks[0].index == 1


# --- EL caso: el cliente congelado -----------------------------------------


def test_el_cliente_congelado_no_escribe_encima(ana, beto) -> None:
    """El escenario del Bloque A, con un cliente en vez de un ControlNode.

        t=0  Ana toma el lock (epoca N) y empieza a escribir
        t=1  Ana se congela: su portatil suspende, se le va la red
        t=7  el lease vence sin que Ana se entere
        t=8  Beto lo toma (epoca N+1) y escribe su version
        t=9  Ana despierta EN MEDIO, convencida de que sigue teniendo el lock

    Si lo unico que Ana comprobo fue «tengo el lock» antes de empezar, en t=9 escribe
    encima de lo de Beto y los dos creen haber escrito con exclusion. Lo que lo corta es
    que la epoca viaje con la operacion y se verifique dentro de la transaccion.

    Aqui se simula la congelacion sin esperar al vencimiento: basta con que OTRO tome el
    lock —lo que sube la epoca— para que la de Ana quede obsoleta.
    """
    ana.mkdir("/critico", parents=True)
    file_id = _subir(ana, "/critico/doc.txt", b"contenido\n")
    ana.share("/critico", beto.session.username, "write")
    ruta_beto = f"/compartido-conmigo/{ana.session.username}/critico/doc.txt"

    lock_de_ana = ana.lock("/critico/doc.txt", holder="sesion-de-ana")
    ana.unlock("/critico/doc.txt", holder="sesion-de-ana")  # el lease vence
    lock_de_beto = beto.lock(ruta_beto, holder="sesion-de-beto")

    # Ana despierta y escribe con su epoca vieja.
    with pytest.raises(Exception) as fallo:
        ana.append(
            file_id, 10, lock_holder="sesion-de-ana", lock_epoch=lock_de_ana.epoch
        )

    assert getattr(fallo.value, "code", "") == "stale_lock"
    # Y Beto, que si tiene el lock vigente, escribe sin problema.
    assert beto.append(
        file_id, 10, lock_holder="sesion-de-beto", lock_epoch=lock_de_beto.epoch
    ).blocks


def test_escribir_sin_traer_el_lock_cuando_otro_lo_tiene_falla(ana, beto) -> None:
    """El caso normal de «otro lo tiene abierto», distinto del cliente congelado."""
    ana.mkdir("/ocupado", parents=True)
    file_id = _subir(ana, "/ocupado/doc.txt", b"x")
    ana.share("/ocupado", beto.session.username, "write")
    beto.lock(
        f"/compartido-conmigo/{ana.session.username}/ocupado/doc.txt",
        holder="sesion-de-beto",
    )

    with pytest.raises(Exception) as fallo:
        ana.append(file_id, 10)

    assert getattr(fallo.value, "code", "") == "file_locked"


def test_un_archivo_SIN_bloquear_se_escribe_como_siempre(ana) -> None:
    """El lock es opcional, no obligatorio. Es lo que mantiene funcionando todo lo de las
    etapas anteriores, que no sabe nada de locks."""
    file_id = _subir(ana, "/sin-lock.txt", b"x")

    assert ana.append(file_id, 10).blocks


def test_un_lock_COMPARTIDO_no_autoriza_a_escribir(ana) -> None:
    """Sin esto, abrir en lectura y luego escribir se saltaria la exclusion entera:
    varios compartidos conviven, asi que dos escribirian a la vez creyendo tener
    permiso."""
    file_id = _subir(ana, "/compartido-escribe.txt", b"x")
    lock = ana.lock("/compartido-escribe.txt", mode="shared", holder="s1")

    with pytest.raises(Exception) as fallo:
        ana.append(file_id, 10, lock_holder="s1", lock_epoch=lock.epoch)

    assert getattr(fallo.value, "code", "") == "file_locked"


# --- El ciclo completo por el CLI, con cifrado ------------------------------


def test_el_append_por_el_CLI_con_cifrado_conserva_el_contenido(
    cluster, tmp_path, monkeypatch
) -> None:
    """De punta a punta con el cliente de verdad: login, put, append, get.

    Con cifrado activo, que es el caso que de verdad importa: la reescritura del bloque de
    cola exige DESCIFRARLO, y eso solo lo puede hacer el cliente. Si el append se hubiera
    disenado en el servidor, este caso seria imposible.
    """
    from typer.testing import CliRunner

    from dfsha.client.cli import app

    casa = tmp_path / "casa"
    casa.mkdir()
    monkeypatch.setenv("DFSHA_HOME", str(casa))
    monkeypatch.setenv("DFSHA_CONTROL_URL", cluster.control_url)
    runner = CliRunner()

    def correr(*args):
        resultado = runner.invoke(app, list(args))
        assert resultado.exit_code == 0, f"{args}: {resultado.output}{resultado.exception}"
        return resultado

    correr("register", "cli-rf3", "--password", "contrasena-de-prueba")
    correr("login", "cli-rf3", "--password", "contrasena-de-prueba")

    inicial = tmp_path / "inicial.txt"
    inicial.write_bytes(b"linea uno\n")
    correr("put", str(inicial), "/cli.txt")

    correr("lock", "/cli.txt")

    extra = tmp_path / "extra.txt"
    extra.write_bytes(b"linea dos\n")
    correr("append", "/cli.txt", str(extra))

    correr("unlock", "/cli.txt")

    bajado = tmp_path / "bajado.txt"
    correr("get", "/cli.txt", str(bajado))

    assert bajado.read_bytes() == b"linea uno\nlinea dos\n"


def test_read_por_rango_desde_el_CLI(cluster, tmp_path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from dfsha.client.cli import app

    casa = tmp_path / "casa2"
    casa.mkdir()
    monkeypatch.setenv("DFSHA_HOME", str(casa))
    monkeypatch.setenv("DFSHA_CONTROL_URL", cluster.control_url)
    runner = CliRunner()

    def correr(*args):
        resultado = runner.invoke(app, list(args))
        assert resultado.exit_code == 0, f"{args}: {resultado.output}{resultado.exception}"
        return resultado

    correr("register", "cli-rango", "--password", "contrasena-de-prueba")
    correr("login", "cli-rango", "--password", "contrasena-de-prueba")

    origen = tmp_path / "abecedario.txt"
    origen.write_bytes(b"abcdefghijklmnopqrstuvwxyz")
    correr("put", str(origen), "/abc.txt")

    salida = runner.invoke(app, ["read", "/abc.txt", "--offset", "10", "--length", "5"])

    assert salida.exit_code == 0, salida.output
    assert "klmno" in salida.output
