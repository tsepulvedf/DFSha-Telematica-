"""Cifrado extremo a extremo de punta a punta: el criterio 11 de la Etapa 3.

*Un bloque en disco no es legible sin la clave: comprobar que el contenido almacenado no
coincide con el texto claro.*

Estas pruebas miran **el disco del DataNode**, no la API. Comprobar el cifrado por la API
no probaria nada: el cliente cifra y descifra, asi que un `put` seguido de un `get` da el
mismo resultado este cifrado o no. Lo que distingue las dos implementaciones es lo que
queda escrito en el `.blk`.
"""

from __future__ import annotations

import hashlib
from pathlib import Path as FsPath

import pytest

from dfsha.client.api import ControlApi
from dfsha.client.session import Session
from dfsha.client.transfer import download_blocks, upload_blocks
from dfsha.common.crypto import (
    TAG_BYTES,
    DecryptionError,
    FileCrypto,
    derive_master_key,
    new_file_key,
    unwrap_file_key,
    wrap_file_key,
)

from .cluster import MB, Cluster, start_cluster

CONTRASENA = "contrasena-de-prueba"
#: Texto claro reconocible: si apareciera tal cual en el `.blk`, se veria a simple vista.
MARCA = b"ESTO-NO-DEBERIA-VERSE-EN-EL-DISCO"


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    c = start_cluster(tmp_path_factory.mktemp("cifrado"), block_size=MB, data_nodes=1)
    try:
        yield c
    finally:
        c.stop()


@pytest.fixture()
def sesion_cifrada(cluster, request):
    """Un usuario con su clave maestra derivada, como la tendria el CLI tras el login."""
    nombre = f"u-{request.node.name[:28]}"
    sesion = Session(control_url=cluster.control_url)
    api = ControlApi(sesion)
    api.register(nombre, CONTRASENA)
    token = api.login(nombre, CONTRASENA)
    sesion.token = token.access_token
    sesion.username = nombre

    assert token.kdf_salt, "el servidor no devolvio la sal del KDF"
    maestra = derive_master_key(CONTRASENA, bytes.fromhex(token.kdf_salt))
    return api, maestra


def _archivo(tmp_path: FsPath, nombre: str, repeticiones: int = 300) -> tuple[FsPath, bytes]:
    ruta = tmp_path / nombre
    datos = MARCA * repeticiones
    ruta.write_bytes(datos)
    return ruta, datos


def _blk(cluster: Cluster, block_id: str) -> FsPath:
    """El `.blk` de un bloque concreto, en el layout con sharding del DataNode."""
    ruta = cluster.nodes[0].data_dir / "blocks" / block_id[:2] / f"{block_id}.blk"
    assert ruta.exists(), f"no se escribio el bloque {block_id}"
    return ruta


def _subir_cifrado(api: ControlApi, maestra: bytes, local: FsPath, remoto: str) -> str:
    """Lo que hace `dfsha put`: clave nueva, envolver con la maestra, cifrar, commit."""
    tamano = local.stat().st_size
    plan = api.create_file(remoto, tamano, cipher_overhead=TAG_BYTES)

    clave = new_file_key()
    cripto = FileCrypto(file_id=plan.file_id, file_key=clave)
    upload_blocks(local, plan.blocks, parallel=1, crypto=cripto)

    api.commit_file(
        plan.file_id,
        wrapped_key=wrap_file_key(clave, maestra, plan.file_id).hex(),
        key_algo="AES-256-GCM",
    )
    return plan.file_id


# --- El criterio 11 ---------------------------------------------------------


def test_criterio_11_el_bloque_en_disco_no_es_el_texto_claro(
    cluster, sesion_cifrada, tmp_path
) -> None:
    """Lo que se mira es el `.blk`, no la API.

    El DataNode guarda estos bytes y no tiene la clave: con acceso completo a su disco,
    no puede leer el contenido.
    """
    api, maestra = sesion_cifrada
    local, claro = _archivo(tmp_path, "secreto.bin")

    _subir_cifrado(api, maestra, local, "/secreto.bin")

    blks = list((cluster.nodes[0].data_dir / "blocks").rglob("*.blk"))
    assert blks, "no se escribio ningun bloque"

    for blk in blks:
        en_disco = blk.read_bytes()
        assert MARCA not in en_disco, f"el texto claro esta en {blk.name}"
        assert en_disco != claro[: len(en_disco)]


def test_el_bloque_en_disco_ocupa_16_bytes_mas_por_la_etiqueta_GCM(
    cluster, sesion_cifrada, tmp_path
) -> None:
    """Efecto colateral visible, y util de conocer: el tamano en disco no es el tamano
    del archivo. GCM anade una etiqueta de autenticacion de 128 bits por bloque.

    Y la otra mitad, que es la que costo un fallo: **el metadato dice el tamano
    ALMACENADO**. `blocks.size` tiene que coincidir con lo que hay en el `.blk`, porque es
    contra ese numero contra el que el cliente comprueba lo que descarga. El tamano claro
    vive en `files.size`.
    """
    api, maestra = sesion_cifrada
    local, claro = _archivo(tmp_path, "tamano.bin", repeticiones=100)

    _subir_cifrado(api, maestra, local, "/tamano.bin")

    # Solo los bloques de ESTE archivo: el cluster es de modulo y su disco arrastra los
    # de las pruebas anteriores.
    plan = api.open_file("/tamano.bin")
    blks = [_blk(cluster, b.block_id) for b in plan.blocks]
    total_en_disco = sum(b.stat().st_size for b in blks)

    assert total_en_disco == len(claro) + TAG_BYTES * len(blks)
    assert plan.size == len(claro), "files.size tiene que ser el tamano CLARO"
    assert sum(b.size for b in plan.blocks) == total_en_disco, (
        "blocks.size tiene que ser el tamano ALMACENADO"
    )


def test_sin_cifrar_el_texto_claro_SI_aparece_en_disco(cluster, sesion_cifrada, tmp_path) -> None:
    """La prueba de control. Sin esto, la anterior pasaria aunque el cifrado no hiciera
    nada y el texto claro simplemente no estuviera en ese fichero."""
    api, _ = sesion_cifrada
    local, _ = _archivo(tmp_path, "en-claro.bin", repeticiones=50)

    plan = api.create_file("/en-claro.bin", local.stat().st_size)
    upload_blocks(local, plan.blocks, parallel=1)  # sin crypto
    api.commit_file(plan.file_id)

    en_disco = b"".join(
        b.read_bytes() for b in (cluster.nodes[0].data_dir / "blocks").rglob("*.blk")
    )
    assert MARCA in en_disco


# --- Ida y vuelta -----------------------------------------------------------


def test_ida_y_vuelta_cifrada(cluster, sesion_cifrada, tmp_path) -> None:
    api, maestra = sesion_cifrada
    local, claro = _archivo(tmp_path, "ida.bin")
    sha = hashlib.sha256(claro).hexdigest()

    _subir_cifrado(api, maestra, local, "/ida.bin")

    plan = api.open_file("/ida.bin")
    assert plan.wrapped_key, "el servidor no devolvio la clave envuelta"
    assert plan.key_algo == "AES-256-GCM"

    clave = unwrap_file_key(bytes.fromhex(plan.wrapped_key), maestra, plan.file_id)
    destino = tmp_path / "bajado.bin"
    download_blocks(
        destino,
        plan.blocks,
        parallel=1,
        crypto=FileCrypto(file_id=plan.file_id, file_key=clave),
        plain_size=plan.size,
    )

    assert hashlib.sha256(destino.read_bytes()).hexdigest() == sha


def test_un_archivo_multibloque_se_reconstruye_en_orden(
    cluster, sesion_cifrada, tmp_path
) -> None:
    """Cada bloque lleva un nonce distinto derivado de su indice, asi que un
    reordenamiento no pasaria desapercibido: fallaria al descifrar."""
    api, maestra = sesion_cifrada
    ruta = tmp_path / "grande.bin"
    datos = bytes((i * 31 + 17) % 256 for i in range(3 * MB))
    ruta.write_bytes(datos)

    _subir_cifrado(api, maestra, ruta, "/grande.bin")

    plan = api.open_file("/grande.bin")
    assert len(plan.blocks) == 3

    clave = unwrap_file_key(bytes.fromhex(plan.wrapped_key), maestra, plan.file_id)
    destino = tmp_path / "grande-bajado.bin"
    download_blocks(
        destino,
        plan.blocks,
        parallel=2,
        crypto=FileCrypto(file_id=plan.file_id, file_key=clave),
        plain_size=plan.size,
    )

    assert destino.read_bytes() == datos


# --- Lo que el servidor NO puede hacer --------------------------------------


def test_con_el_metadato_entero_y_el_disco_no_se_descifra_sin_la_contrasena(
    cluster, sesion_cifrada, tmp_path
) -> None:
    """La afirmacion que justifica todo el esquema, comprobada por su efecto.

    Se simula a alguien con acceso TOTAL al servidor: la clave envuelta del metadato, la
    sal del usuario y los bloques del disco. Con todo eso y sin la contrasena, no puede
    abrir la clave del archivo.
    """
    api, maestra = sesion_cifrada
    local, _ = _archivo(tmp_path, "inalcanzable.bin", repeticiones=40)
    _subir_cifrado(api, maestra, local, "/inalcanzable.bin")

    plan = api.open_file("/inalcanzable.bin")
    envuelta = bytes.fromhex(plan.wrapped_key)

    # El atacante conoce la sal (viaja en el login y no es secreta) pero no la
    # contrasena, asi que prueba otra.
    otra_maestra = derive_master_key("contrasena-equivocada", b"\x00" * 16)
    with pytest.raises(DecryptionError):
        unwrap_file_key(envuelta, otra_maestra, plan.file_id)


def test_la_envoltura_de_un_archivo_no_abre_la_de_otro(
    cluster, sesion_cifrada, tmp_path
) -> None:
    """El `file_id` entra en la envoltura como dato autenticado.

    Sin eso, alguien con acceso de escritura al metadato podria intercambiar envolturas y
    hacer que el cliente descifrara el archivo equivocado con una clave que si valida.
    """
    api, maestra = sesion_cifrada
    a, _ = _archivo(tmp_path, "a.bin", repeticiones=10)
    b, _ = _archivo(tmp_path, "b.bin", repeticiones=10)
    _subir_cifrado(api, maestra, a, "/a.bin")
    _subir_cifrado(api, maestra, b, "/b.bin")

    plan_a = api.open_file("/a.bin")
    plan_b = api.open_file("/b.bin")

    with pytest.raises(DecryptionError):
        unwrap_file_key(bytes.fromhex(plan_a.wrapped_key), maestra, plan_b.file_id)


def test_los_archivos_sin_cifrar_de_etapas_anteriores_se_siguen_bajando(
    cluster, sesion_cifrada, tmp_path
) -> None:
    """`wrapped_key` vacia = sin cifrar, y el cliente lo MIRA en vez de suponerlo.

    Es lo que permite que un metadato migrado desde la Etapa 2 siga siendo utilizable.
    """
    api, _ = sesion_cifrada
    local, claro = _archivo(tmp_path, "viejo.bin", repeticiones=20)

    plan = api.create_file("/viejo.bin", local.stat().st_size)
    upload_blocks(local, plan.blocks, parallel=1)
    api.commit_file(plan.file_id)

    lectura = api.open_file("/viejo.bin")
    assert lectura.wrapped_key == ""

    destino = tmp_path / "viejo-bajado.bin"
    download_blocks(destino, lectura.blocks, parallel=1)
    assert destino.read_bytes() == claro


# --- El camino REAL del cliente -----------------------------------------------
#
# Las pruebas de arriba reimplementan los pasos de `dfsha put` en `_subir_cifrado`, y es
# la PRUEBA la que decide cifrar. La decision que toma el cliente —cifrar si la sesion
# tiene clave, y antes, si no la tenia, subir en claro sin avisar— no la recorria ninguna.
# Por esa rama subio en claro todo usuario anterior a la migracion 0006, con estas pruebas
# en verde. Lo encontro el guion `cifrado_en_reposo.py`, no la suite.
#
# Estas dos pasan por el CLI entero: login, put, y el `.blk` en disco.


def _cli(cluster, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from dfsha.client.cli import app

    casa = tmp_path / "casa"
    casa.mkdir()
    monkeypatch.setenv("DFSHA_HOME", str(casa))
    monkeypatch.setenv("DFSHA_CONTROL_URL", cluster.control_url)
    runner = CliRunner()
    return lambda *args: runner.invoke(app, list(args)), casa


def _blks_con_la_marca(cluster: Cluster) -> list[str]:
    return [
        blk.name
        for blk in (cluster.nodes[0].data_dir / "blocks").rglob("*.blk")
        if MARCA in blk.read_bytes()
    ]


def test_el_put_del_CLI_cifra_lo_que_queda_en_disco(cluster, tmp_path, monkeypatch) -> None:
    """Criterio 11 por el camino que usa una persona, con un archivo como el del guion.

    62 bytes, un bloque incompleto: el mismo caso que destapo el fallo en Docker.
    """
    from dfsha.client.session import SessionStore

    cli, casa = _cli(cluster, tmp_path, monkeypatch)
    assert cli("register", "cli-cifra", "--password", CONTRASENA).exit_code == 0
    assert cli("login", "cli-cifra", "--password", CONTRASENA).exit_code == 0

    local = tmp_path / "claro.txt"
    local.write_bytes(MARCA + b"-" * (61 - len(MARCA)) + b"\n")
    assert local.stat().st_size == 62

    resultado = cli("put", str(local), "/claro.txt")
    assert resultado.exit_code == 0, resultado.output

    api = ControlApi(SessionStore(casa).load(cluster.control_url))
    plan = api.open_file("/claro.txt")
    assert plan.wrapped_key, "el put del CLI confirmo el archivo SIN clave envuelta"

    blk = _blk(cluster, plan.blocks[0].block_id)
    assert MARCA not in blk.read_bytes(), "el put del CLI dejo el texto claro en disco"
    assert blk.stat().st_size == 62 + TAG_BYTES


def test_un_usuario_sin_sal_no_sube_en_claro(cluster, tmp_path, monkeypatch) -> None:
    """El estado exacto que dejaba la 0006 en un usuario anterior: `kdf_salt` vacia.

    Antes, el login no derivaba clave y el put subia en claro con codigo 0. Ahora el put se
    niega, no confirma nada, y no deja la frase en ningun disco.
    """
    from sqlalchemy import text

    from dfsha.control_node.repositories.database import build_engine

    cli, _ = _cli(cluster, tmp_path, monkeypatch)
    assert cli("register", "sin-sal", "--password", CONTRASENA).exit_code == 0
    with build_engine(cluster.settings.db_url).begin() as conexion:
        conexion.execute(text("UPDATE users SET kdf_salt = '' WHERE username = 'sin-sal'"))

    login = cli("login", "sin-sal", "--password", CONTRASENA)
    assert login.exit_code == 0
    assert "sal de cifrado" in login.output, "el login no avisa de que no podra cifrar"

    antes = set(_blks_con_la_marca(cluster))
    local = tmp_path / "claro.txt"
    local.write_bytes(MARCA + b"\n")

    resultado = cli("put", str(local), "/claro.txt")

    assert resultado.exit_code != 0, "el put subio sin clave de cifrado"
    # Rich parte las lineas por el ancho: se compara sin saltos.
    assert "no sube archivos en claro" in " ".join(resultado.output.split())
    assert set(_blks_con_la_marca(cluster)) == antes, "quedo texto claro en disco"
    assert cli("ls", "/").output.count("claro.txt") == 0

