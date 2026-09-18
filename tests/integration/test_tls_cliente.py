"""TLS para el trafico de CLIENTE (C2), con sockets de verdad.

Lo ultimo del hito. Distinto del mTLS del plano interno, y la diferencia es la decision:

| | Plano interno | Plano de cliente |
|---|---|---|
| Quien llama | un servicio nuestro | una **persona** |
| Como se identifica | su **certificado** | su **JWT** |
| El servidor exige certificado | **si** | **no** |

Darle un certificado a cada usuario seria montar una PKI para usuarios y acabar sabiendo lo
mismo que ya dice el token.

**El caso bueno va primero**, por la misma razon que en `test_mtls.py`: un cliente que
rechazara todo pasaria las pruebas de rechazo con nota y el sistema estaria roto.
"""

from __future__ import annotations

import ssl
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from dfsha.client.tls import CA_ENV, verificacion_para
from dfsha.common.errors import DFShaError
from dfsha.common.serve import ClientTls

from tests.certs import material


def _puerto_libre() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _ServidorHttps:
    """Un uvicorn con TLS de cliente, en un hilo, sobre un puerto real."""

    def __init__(self, tls: ClientTls) -> None:
        app = FastAPI()

        @app.get("/eco")
        def eco() -> dict:
            return {"ok": True}

        self.port = _puerto_libre()
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=self.port,
            log_level="error",
            access_log=False,
            **tls.uvicorn_kwargs(),
        )
        self._server = uvicorn.Server(config)
        self._hilo = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self) -> "_ServidorHttps":
        self._hilo.start()
        limite = time.monotonic() + 15
        while not self._server.started and time.monotonic() < limite:
            time.sleep(0.05)
        if not self._server.started:
            raise RuntimeError("el servidor HTTPS no arranco")
        return self

    def __exit__(self, *_) -> None:
        self._server.should_exit = True
        self._hilo.join(timeout=10)

    @property
    def url(self) -> str:
        return f"https://localhost:{self.port}"


@pytest.fixture(scope="module")
def servidor():
    m = material("control")
    with _ServidorHttps(ClientTls(str(m.cert), str(m.key))) as s:
        yield s


# --- El caso bueno, primero -------------------------------------------------


def test_el_cliente_habla_HTTPS_verificando_la_CA(servidor, monkeypatch) -> None:
    """Si esto falla, las pruebas de rechazo de abajo no prueban nada: rechazarian todo,
    incluido lo correcto."""
    monkeypatch.setenv(CA_ENV, str(material("control").ca_cert))

    respuesta = httpx.get(
        f"{servidor.url}/eco", verify=verificacion_para(servidor.url), timeout=10
    )

    assert respuesta.status_code == 200
    assert respuesta.json() == {"ok": True}


def test_al_cliente_NO_se_le_pide_certificado(servidor, monkeypatch) -> None:
    """La diferencia con el plano interno, comprobada por su efecto.

    Este cliente **no presenta ningun certificado** y aun asi es atendido. Si el servidor
    exigiera uno —`CERT_REQUIRED`, como el plano interno— la conexion se cerraria. Que no
    lo exija es deliberado: la identidad del usuario es el JWT.
    """
    monkeypatch.setenv(CA_ENV, str(material("control").ca_cert))
    contexto = verificacion_para(servidor.url)

    assert isinstance(contexto, ssl.SSLContext)
    # Un contexto sin cadena de certificado cargada: no hay nada que presentar.
    assert not contexto.get_ca_certs(binary_form=True) == []  # la CA si esta
    assert httpx.get(f"{servidor.url}/eco", verify=contexto, timeout=10).status_code == 200


# --- Lo que tiene que fallar ------------------------------------------------


def test_sin_la_CA_no_se_confia_en_el_servidor(servidor, monkeypatch) -> None:
    """El caso que `verify=False` haria desaparecer, y por eso no se usa.

    Con la verificacion por defecto de httpx —los almacenes del sistema— nuestra CA propia
    no esta, asi que el certificado se rechaza. Es lo correcto: un certificado firmado por
    alguien desconocido no vale mas que uno autofirmado.
    """
    monkeypatch.delenv(CA_ENV, raising=False)

    with pytest.raises(httpx.ConnectError):
        httpx.get(f"{servidor.url}/eco", timeout=10)


def test_una_CA_DISTINTA_no_vale(servidor, monkeypatch, tmp_path) -> None:
    """El ataque de verdad: alguien se pone en medio con un certificado valido... firmado
    por OTRA autoridad. Sin esta comprobacion, TLS cifraria el canal hacia el atacante."""
    from dfsha.common.tls import ca_only_context

    # Una CA legitima, pero no la nuestra: la que trae el sistema.
    ajena = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    ajena.minimum_version = ssl.TLSVersion.TLSv1_2

    with pytest.raises(httpx.ConnectError):
        httpx.get(f"{servidor.url}/eco", verify=ajena, timeout=10)

    # Y con la nuestra, el mismo servidor y la misma peticion funcionan.
    buena = ca_only_context(material("control").ca_cert)
    assert httpx.get(f"{servidor.url}/eco", verify=buena, timeout=10).status_code == 200


def test_si_falta_la_CA_el_error_lo_DICE(monkeypatch, tmp_path) -> None:
    """Fallar con la ruta que buscó es la diferencia entre un mensaje util y un
    «SSL: CERTIFICATE_VERIFY_FAILED» que no dice que hacer."""
    monkeypatch.setenv(CA_ENV, str(tmp_path / "no-existe.crt"))

    with pytest.raises(DFShaError) as fallo:
        verificacion_para("https://localhost:9999")

    assert fallo.value.code == "tls_ca_no_encontrada"
    assert "no-existe.crt" in fallo.value.message


# --- Que el camino sin TLS sigue siendo el mismo codigo ---------------------


def test_con_http_no_se_verifica_nada_y_no_hace_falta_CA(monkeypatch) -> None:
    """El mismo codigo sirve para los dos esquemas, que es lo que evita que el camino con
    TLS sea uno aparte que nadie ejercita. Con `http://` no hay CA que buscar."""
    monkeypatch.delenv(CA_ENV, raising=False)

    assert verificacion_para("http://localhost:8000") is True


# --- La configuracion ------------------------------------------------------


def test_las_dos_variables_van_juntas_o_ninguna(tmp_path) -> None:
    """Configurar solo una es casi siempre un error de copia y pega. Arrancar en HTTP
    ignorandolo dejaria a alguien convencido de que su trafico va cifrado."""
    m = material("control")

    assert ClientTls.from_settings("", "") is None
    with pytest.raises(ValueError, match="van juntas"):
        ClientTls.from_settings(str(m.cert), "")
    with pytest.raises(ValueError, match="van juntas"):
        ClientTls.from_settings("", str(m.key))


def test_un_certificado_que_no_existe_falla_al_ARRANCAR(tmp_path) -> None:
    """Y no diez segundos despues con un error de handshake, que es el sintoma que costo
    una tarde en el Bloque C."""
    with pytest.raises(ValueError, match="no existe el fichero"):
        ClientTls.from_settings(str(tmp_path / "falta.crt"), str(tmp_path / "falta.key"))


def test_al_cliente_no_se_le_exige_certificado_en_la_configuracion() -> None:
    """`CERT_NONE` es LA diferencia con el plano interno, y esta fijada aqui para que un
    copia y pega desde `_ServidorInterno` no la convierta en `CERT_REQUIRED`."""
    m = material("control")

    kwargs = ClientTls(str(m.cert), str(m.key)).uvicorn_kwargs()

    assert kwargs["ssl_cert_reqs"] == ssl.CERT_NONE


# --- C2 con el sistema de VERDAD, no con un servidor de juguete ---------------
#
# Las pruebas de arriba levantan una app de una ruta (`/eco`) detras de `ClientTls`.
# Comprueban el TLS, no el sistema con TLS: ninguna monta una cadena de DataNodes sobre
# HTTPS. Y ahi estaba el fallo: el reenvio del pipeline y la descarga de una
# re-replicacion hablaban con el vecino con la verificacion por defecto de httpx, que no
# conoce nuestra CA. Con C2 encendido cada reenvio fallaba por certificado, el nodo
# respondia 201 con una sola copia (un fallo aguas abajo no tumba la subida) y el commit
# daba 409 de quorum: el sintoma apuntando a la capacidad del cluster, otra vez.
#
# Se vio al preparar la validacion de C2 en Docker: el compose ni siquiera pasaba las
# variables, y al revisar que haria falta para encenderlo aparecio esto.

RAPIDO_C2 = dict(
    heartbeat_interval_ms=300,
    suspect_after_ms=1500,
    dead_after_ms=3000,
    membership_interval_ms=100,
    min_free_bytes=0,
)


@pytest.fixture()
def cluster_c2(tmp_path, monkeypatch):
    from tests.integration.cluster import start_cluster

    monkeypatch.setenv(CA_ENV, str(material("control").ca_cert))
    c = start_cluster(
        tmp_path,
        block_size=1024 * 1024,
        data_nodes=4,
        fault_domains=["zona-a", "zona-b", "zona-c", "zona-d"],
        replication_factor=3,
        write_quorum=2,
        tls_cliente=True,
        rereplication_grace_ms=1000,
        rereplication_interval_ms=200,
        rereplication_max_per_node=2,
        **RAPIDO_C2,
    )
    try:
        yield c
    finally:
        c.stop()


def _esperar_a(condicion, timeout: float = 45.0) -> bool:
    limite = time.time() + timeout
    while time.time() < limite:
        try:
            if condicion():
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def test_con_C2_el_put_del_CLI_replica_por_HTTPS_en_tres_nodos(
    cluster_c2, tmp_path, monkeypatch
) -> None:
    """El ciclo del usuario con TLS en TODOS los saltos: CLI -> ControlNode, CLI ->
    DataNode, y DataNode -> DataNode por el pipeline. Con cifrado, como en el video."""
    from typer.testing import CliRunner

    from dfsha.client.api import ControlApi
    from dfsha.client.cli import app
    from dfsha.client.session import SessionStore

    cluster = cluster_c2
    assert cluster.control_url.startswith("https://")
    assert all(n.url.startswith("https://") for n in cluster.nodes)

    casa = tmp_path / "casa"
    casa.mkdir()
    monkeypatch.setenv("DFSHA_HOME", str(casa))
    monkeypatch.setenv("DFSHA_CONTROL_URL", cluster.control_url)
    runner = CliRunner()

    def cli(*args):
        resultado = runner.invoke(app, list(args))
        assert resultado.exit_code == 0, f"{args}: {resultado.output}{resultado.exception}"
        return resultado

    cli("register", "usuario-c2", "--password", "contrasena-de-prueba")
    cli("login", "usuario-c2", "--password", "contrasena-de-prueba")

    local = tmp_path / "datos.bin"
    datos = bytes((i * 7 + 13) % 256 for i in range(3 * 1024 * 1024 + 100))
    local.write_bytes(datos)
    cli("put", str(local), "/datos.bin")

    api = ControlApi(SessionStore(casa).load(cluster.control_url))
    plan = api.open_file("/datos.bin")
    assert plan.wrapped_key, "con C2 el archivo tiene que seguir subiendose cifrado"
    assert all(r.base_url.startswith("https://") for b in plan.blocks for r in b.replicas)

    # Lo que fallaba: la cadena. Cada bloque en TRES discos, no en uno.
    for bloque in plan.blocks:
        con_copia = [n.name for n in cluster.nodes if bloque.block_id in n.blk_en_disco()]
        assert len(con_copia) == 3, (
            f"el bloque {bloque.index} esta en {con_copia}: el pipeline no reenvio por HTTPS"
        )
    assert api.stat("/datos.bin").replication_state == "FULLY_REPLICATED"

    bajado = tmp_path / "bajado.bin"
    cli("get", "/datos.bin", str(bajado))
    assert bajado.read_bytes() == datos


def test_con_C2_la_rereplicacion_copia_por_HTTPS(cluster_c2, tmp_path) -> None:
    """La otra salida de par: el destino DESCARGA la copia del origen por su direccion
    de par, que con C2 es https. Sin la CA, la orden fallaba y la copia no volvia nunca."""
    from dfsha.client.api import ControlApi
    from dfsha.client.session import Session
    from dfsha.client.transfer import upload_blocks

    cluster = cluster_c2
    sesion = Session(control_url=cluster.control_url)
    api = ControlApi(sesion)
    api.register("c2-rr", "contrasena-de-prueba")
    sesion.token = api.login("c2-rr", "contrasena-de-prueba").access_token

    local = tmp_path / "uno.bin"
    local.write_bytes(b"x" * (1024 * 1024))
    por_id = {n.data_node_id: n for n in cluster.nodes}

    plan = api.create_file("/uno.bin", 1024 * 1024)
    bloque_id = plan.blocks[0].block_id
    upload_blocks(local, plan.blocks, parallel=1)
    api.commit_file(plan.file_id)

    tenedores = {n for n, h in por_id.items() if bloque_id in h.blk_en_disco()}
    assert len(tenedores) == 3
    por_id[sorted(tenedores)[0]].stop()

    libres = set(por_id) - tenedores
    assert _esperar_a(
        lambda: any(bloque_id in por_id[n].blk_en_disco() for n in libres)
    ), "la re-replicacion no copio el bloque por HTTPS al nodo libre"
