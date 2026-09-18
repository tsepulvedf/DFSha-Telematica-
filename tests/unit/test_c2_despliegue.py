"""Lo que hace falta para ENCENDER el TLS de cliente (C2), no para implementarlo.

C2 estaba implementado y probado, y no se podia encender: el compose no pasaba las
variables, las sondas de salud preguntaban en `http://` fijo (y con ellas el stack no
llegaba a levantar), la URL guardada en la sesion ignoraba `DFSHA_CONTROL_URL`, y el
pipeline entre DataNodes no confiaba en la CA del proyecto. Cada pieza era correcta sola;
lo que faltaba era el camino entero. Esto fija las piezas de ese camino que no necesitan
red. El camino con red esta en `tests/integration/test_tls_cliente.py`.
"""

from __future__ import annotations

import json
import ssl
from pathlib import Path

import pytest

from dfsha.client.session import Session, SessionStore
from dfsha.common import healthcheck

from tests.certs import material

RAIZ = Path(__file__).resolve().parents[2]


# --- La sonda de salud ------------------------------------------------------


def test_sin_C2_la_sonda_pregunta_en_http(monkeypatch) -> None:
    monkeypatch.delenv("DFSHA_CLIENT_TLS_CERT", raising=False)
    url, verify = healthcheck.destino(8001)
    assert url == "http://127.0.0.1:8001/health"
    assert verify is True


def test_con_C2_la_sonda_pregunta_en_https_VERIFICANDO_la_CA(monkeypatch) -> None:
    """La misma variable que enciende TLS en el servidor enciende TLS en la sonda. Si
    fueran dos, podrian discrepar, y es lo que pasaba: el servidor en HTTPS y la sonda en
    HTTP, el contenedor declarado enfermo y el stack sin levantar."""
    monkeypatch.setenv("DFSHA_CLIENT_TLS_CERT", "/etc/dfsha/certs/control.crt")
    monkeypatch.setenv("DFSHA_TLS_CA_CERT", str(material("control").ca_cert))
    url, verify = healthcheck.destino(8000)
    assert url == "https://localhost:8000/health"
    assert isinstance(verify, ssl.SSLContext), "la sonda no puede aceptar cualquier certificado"
    assert verify.verify_mode == ssl.CERT_REQUIRED


def test_la_sonda_contesta_1_y_no_revienta_si_no_hay_nadie(monkeypatch) -> None:
    monkeypatch.delenv("DFSHA_CLIENT_TLS_CERT", raising=False)
    assert healthcheck.main(["1"]) == 1


def test_ningun_Dockerfile_ni_el_compose_fija_http_en_la_sonda() -> None:
    """Una sonda con `http://` escrito a mano es la que impedia encender C2."""
    for fichero in (
        RAIZ / "docker" / "control_node.Dockerfile",
        RAIZ / "docker" / "data_node.Dockerfile",
        RAIZ / "docker-compose.yml",
    ):
        texto = fichero.read_text(encoding="utf-8")
        assert "httpx.get('http://127.0.0.1" not in texto, fichero.name
    for dockerfile in ("control_node.Dockerfile", "data_node.Dockerfile"):
        texto = (RAIZ / "docker" / dockerfile).read_text(encoding="utf-8")
        assert "dfsha.common.healthcheck" in texto, dockerfile


# --- La URL del ControlNode en la sesion ------------------------------------


def _sesion_guardada(tmp_path: Path, url: str) -> SessionStore:
    store = SessionStore(tmp_path)
    store.save(Session(control_url=url, token="t", username="ana"))
    return store


def test_DFSHA_CONTROL_URL_gana_a_la_url_guardada(tmp_path, monkeypatch) -> None:
    """Antes ganaba la guardada, y encender C2 no se podia: exportar https:// no cambiaba
    nada, y ni un login nuevo lo arreglaba porque tambien parte de la sesion guardada."""
    store = _sesion_guardada(tmp_path, "http://localhost:8000")
    monkeypatch.setenv("DFSHA_CONTROL_URL", "https://localhost:8000")
    assert store.load("http://otro:1").control_url == "https://localhost:8000"


def test_sin_la_variable_se_usa_la_url_guardada(tmp_path, monkeypatch) -> None:
    """La otra mitad: la sesion sigue sirviendo para no repetir la URL en cada orden."""
    store = _sesion_guardada(tmp_path, "https://localhost:8000")
    monkeypatch.delenv("DFSHA_CONTROL_URL", raising=False)
    assert store.load("http://localhost:8000").control_url == "https://localhost:8000"


# --- El override de compose y la configuracion de nginx ----------------------


def test_el_override_enciende_C2_en_los_siete_servicios_y_las_ocho_URL() -> None:
    """Encender C2 son muchas piezas que tienen que cambiar juntas. Olvidar una URL deja
    un nodo anunciando http:// a un cliente que solo habla https; olvidar el certificado
    de un servicio lo deja en HTTP detras de un balanceador que ya habla HTTPS."""
    yaml = pytest.importorskip("yaml")
    servicios = yaml.safe_load(
        (RAIZ / "docker-compose.tls.yml").read_text(encoding="utf-8")
    )["services"]

    for n in (1, 2, 3):
        entorno = servicios[f"control-node-{n}"]["environment"]
        assert entorno["DFSHA_CLIENT_TLS_CERT"].endswith("control.crt")
        assert entorno["DFSHA_CLIENT_TLS_KEY"].endswith("control.key")
    for n in (1, 2, 3, 4):
        entorno = servicios[f"data-node-{n}"]["environment"]
        assert entorno["DFSHA_CLIENT_TLS_CERT"].endswith("data.crt")
        assert "https://localhost:800" in entorno["DFSHA_DATANODE_ADVERTISE_URL"]
        assert f"https://data-node-{n}:8001" in entorno["DFSHA_DATANODE_PEER_URL"]


def test_nginx_con_C2_verifica_a_los_ControlNodes() -> None:
    """El segundo tramo, nginx -> ControlNode, cifrado Y verificado. `proxy_ssl_verify
    off` haria que todo funcionara a la primera, que es justo por lo que se fija."""
    conf = (RAIZ / "docker" / "nginx" / "dfsha-tls.conf").read_text(encoding="utf-8")
    activas = [l.split("#")[0].strip() for l in conf.splitlines()]
    assert "listen 8000 ssl;" in activas
    assert "proxy_pass https://dfsha_control_rest;" in activas
    assert activas.count("proxy_ssl_verify              on;") == 2
    assert not any("proxy_ssl_verify" in l and "off" in l for l in activas)
    assert json.dumps(activas).count("grpc_pass") == 0
