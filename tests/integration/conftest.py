"""Utilidades comunes a las pruebas de integracion.

Levantan la app real de FastAPI contra una base de datos y un directorio temporales. No
se parchea ningun modulo: si algo hay que inyectar, se inyecta por donde la app ya lo
acepta, que es como se comprueba que las costuras existen de verdad.
"""

from __future__ import annotations

import socket
from pathlib import Path as FsPath

import pytest
from fastapi.testclient import TestClient

from dfsha.control_node.config import ControlNodeSettings
from dfsha.control_node.main import create_app

MB = 1024 * 1024

SECRETO_JWT = "pruebas-jwt-no-es-un-secreto-real"
SECRETO_INTERNO = "pruebas-interno-no-es-un-secreto-real"


def puerto_libre() -> int:
    """Un puerto que nadie esta usando.

    Cada app levanta su servidor gRPC del plano de control, asi que dos pruebas con el
    puerto por defecto chocarian. Se pide uno al sistema en vez de repartir numeros fijos
    entre ficheros de prueba.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build_settings(tmp_path: FsPath, **overrides) -> ControlNodeSettings:
    valores = dict(
        db_url=f"sqlite:///{(tmp_path / 'dfsha.db').as_posix()}",
        jwt_secret=SECRETO_JWT,
        internal_secret=SECRETO_INTERNO,
        block_size=MB,
        write_ttl_seconds=600,
        log_level="WARNING",
        grpc_port=puerto_libre(),
    )
    valores.update(overrides)
    return ControlNodeSettings(**valores)


@pytest.fixture()
def settings(tmp_path: FsPath) -> ControlNodeSettings:
    return build_settings(tmp_path)


@pytest.fixture()
def control(settings: ControlNodeSettings):
    with TestClient(create_app(settings)) as client:
        yield client


@pytest.fixture()
def internal_headers() -> dict[str, str]:
    return {"X-DFSha-Internal-Secret": SECRETO_INTERNO}


class Sesion:
    """Un usuario registrado con su token, para no repetir el login en cada prueba."""

    def __init__(self, client: TestClient, username: str, password: str = "contrasena-larga"):
        respuesta = client.post(
            "/api/v1/auth/register", json={"username": username, "password": password}
        )
        assert respuesta.status_code == 201, respuesta.text

        respuesta = client.post(
            "/api/v1/auth/login", json={"username": username, "password": password}
        )
        assert respuesta.status_code == 200, respuesta.text

        self.client = client
        self.username = username
        self.token = respuesta.json()["access_token"]
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def get(self, url: str, **kwargs):
        return self.client.get(url, headers=self.headers, **kwargs)

    def post(self, url: str, **kwargs):
        return self.client.post(url, headers=self.headers, **kwargs)

    def delete(self, url: str, **kwargs):
        return self.client.delete(url, headers=self.headers, **kwargs)

    def mkdir(self, path: str, parents: bool = False):
        return self.post("/api/v1/fs/mkdir", json={"path": path, "parents": parents})

    def ls(self, path: str = "/"):
        return self.get("/api/v1/fs/ls", params={"path": path})

    def nombres(self, path: str = "/") -> list[str]:
        return [e["name"] for e in self.ls(path).json()["entries"]]


@pytest.fixture()
def ana(control: TestClient) -> Sesion:
    return Sesion(control, "ana")


@pytest.fixture()
def beto(control: TestClient) -> Sesion:
    return Sesion(control, "beto")
