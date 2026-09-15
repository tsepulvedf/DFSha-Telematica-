"""Un DFSha completo en proceso: ControlNode y DataNode reales sobre puertos reales.

Hace falta que sean servidores de verdad y no un `TestClient`, porque lo que se quiere
comprobar es justo el camino que un cliente de mentira se saltaria: que el cliente habla
con el ControlNode para el plan y con el DataNode para los bytes, y que esos son dos
procesos distintos en dos puertos distintos.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import uvicorn

from dfsha.control_node.config import ControlNodeSettings
from dfsha.control_node.main import create_app as create_control_app
from dfsha.data_node.config import DataNodeSettings
from dfsha.data_node.main import create_app as create_data_app

__all__ = ["Cluster", "start_cluster"]

MB = 1024 * 1024
SECRETO_JWT = "pruebas-jwt-no-es-un-secreto-real"
SECRETO_INTERNO = "pruebas-interno-no-es-un-secreto-real"


def _puerto_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _ServidorEnHilo:
    def __init__(self, app, port: int) -> None:
        config = uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="warning", access_log=False
        )
        self.server = uvicorn.Server(config)
        self.hilo = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> None:
        self.hilo.start()

    def stop(self) -> None:
        self.server.should_exit = True
        self.hilo.join(timeout=10)


@dataclass
class Cluster:
    control_url: str
    data_url: str
    data_dir: Path
    internal_secret: str = SECRETO_INTERNO

    _servidores: list = None  # type: ignore[assignment]

    def stop(self) -> None:
        for servidor in reversed(self._servidores or []):
            servidor.stop()

    @property
    def internal_headers(self) -> dict[str, str]:
        return {"X-DFSha-Internal-Secret": self.internal_secret}

    def health(self) -> dict:
        return httpx.get(f"{self.data_url}/health", timeout=10).json()


def _esperar(url: str, timeout: float = 20.0) -> None:
    limite = time.time() + timeout
    ultimo: Exception | None = None
    while time.time() < limite:
        try:
            if httpx.get(url, timeout=2).status_code == 200:
                return
        except Exception as exc:
            ultimo = exc
        time.sleep(0.05)
    raise RuntimeError(f"el servidor no respondio en {url}: {ultimo}")


def start_cluster(tmp_path: Path, block_size: int = MB, write_ttl_seconds: int = 600) -> Cluster:
    puerto_control = _puerto_libre()
    puerto_datos = _puerto_libre()
    control_url = f"http://127.0.0.1:{puerto_control}"
    data_url = f"http://127.0.0.1:{puerto_datos}"
    data_dir = tmp_path / "datanode"

    control_settings = ControlNodeSettings(
        db_url=f"sqlite:///{(tmp_path / 'dfsha.db').as_posix()}",
        jwt_secret=SECRETO_JWT,
        internal_secret=SECRETO_INTERNO,
        block_size=block_size,
        write_ttl_seconds=write_ttl_seconds,
        log_level="WARNING",
    )
    data_settings = DataNodeSettings(
        data_dir=str(data_dir),
        control_url=control_url,
        datanode_base_url=data_url,
        internal_secret=SECRETO_INTERNO,
        datanode_capacity_bytes=512 * MB,
        log_level="WARNING",
        register_retry_seconds=0.1,
        register_max_attempts=50,
    )

    control = _ServidorEnHilo(create_control_app(control_settings), puerto_control)
    control.start()
    _esperar(f"{control_url}/health")

    datos = _ServidorEnHilo(create_data_app(data_settings), puerto_datos)
    datos.start()
    _esperar(f"{data_url}/health")

    return Cluster(
        control_url=control_url,
        data_url=data_url,
        data_dir=data_dir,
        _servidores=[control, datos],
    )
