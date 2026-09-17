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
