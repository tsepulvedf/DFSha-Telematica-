"""Un DFSha completo en proceso: ControlNode y N DataNodes reales sobre puertos reales.

Hace falta que sean servidores de verdad y no un `TestClient`, porque lo que se quiere
comprobar es justo el camino que un cliente de mentira se saltaria: que el cliente habla
con el ControlNode para el plan y con los DataNodes para los bytes, que esos son procesos
distintos en puertos distintos, y que el plano de control gRPC los mantiene al dia.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import uvicorn

from dfsha.control_node.config import ControlNodeSettings
from dfsha.control_node.main import create_app as create_control_app
from dfsha.data_node.config import DataNodeSettings
from dfsha.data_node.main import create_app as create_data_app

__all__ = ["Cluster", "DataNodeHandle", "start_cluster", "MB"]

MB = 1024 * 1024
SECRETO_JWT = "pruebas-jwt-no-es-un-secreto-real"
SECRETO_INTERNO = "pruebas-interno-no-es-un-secreto-real"


def puerto_libre() -> int:
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

    def stop(self, timeout: float = 10) -> None:
        self.server.should_exit = True
        self.hilo.join(timeout=timeout)


@dataclass
class DataNodeHandle:
    """Un DataNode del cluster, con lo necesario para pararlo y volver a arrancarlo."""

    name: str
    url: str
    port: int
    data_dir: Path
    fault_domain: str
    settings: DataNodeSettings
    _servidor: _ServidorEnHilo | None = None

    def health(self, timeout: float = 5) -> dict:
        return httpx.get(f"{self.url}/health", timeout=timeout).json()

    @property
    def data_node_id(self) -> str:
        return self.health()["data_node_id"]

    def blk_en_disco(self) -> set[str]:
        return {p.stem for p in (self.data_dir / "blocks").rglob("*.blk")}

    def start(self) -> None:
        """Arranca (o rearranca) el nodo. Conserva su volumen, asi que vuelve con el
        mismo `boot_id` y el ControlNode lo trata como reincorporacion."""
        if self._servidor is not None:
            return
        self._servidor = _ServidorEnHilo(create_data_app(self.settings), self.port)
        self._servidor.start()
        _esperar(f"{self.url}/health")

    def stop(self) -> None:
        """Lo para en seco. Deja de latir, asi que el ControlNode lo vera SUSPECT y
        luego DEAD por los umbrales, no por el cierre del stream."""
        if self._servidor is None:
            return
        self._servidor.stop()
        self._servidor = None

    def wipe_disk(self) -> None:
        """Borra su volumen: pierde bloques y `node.json`, asi que al volver traera otro
        `boot_id` y el ControlNode dara sus replicas por perdidas."""
        import shutil

        shutil.rmtree(self.data_dir, ignore_errors=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)


@dataclass
class Cluster:
    control_url: str
    control_grpc_port: int
    nodes: list[DataNodeHandle]
    internal_secret: str = SECRETO_INTERNO
    _control: _ServidorEnHilo | None = None
    settings: ControlNodeSettings | None = None

    # --- Compatibilidad con las pruebas de la Etapa 1 ----------------------
    # Escritas cuando solo habia un DataNode; siguen valiendo apuntando al primero.

    @property
    def data_url(self) -> str:
        return self.nodes[0].url

    @property
    def data_dir(self) -> Path:
        return self.nodes[0].data_dir

    def health(self) -> dict:
        return self.nodes[0].health()

    # --- Cluster -----------------------------------------------------------

    @property
    def internal_headers(self) -> dict[str, str]:
        return {"X-DFSha-Internal-Secret": self.internal_secret}

    def node(self, name: str) -> DataNodeHandle:
        for nodo in self.nodes:
            if nodo.name == name:
                return nodo
        raise KeyError(name)

    def cluster_status(self, token: str, timeout: float = 5) -> dict:
        """Estado del cluster. Va por /api/v1, asi que pide token de usuario."""
        respuesta = httpx.get(
            f"{self.control_url}/api/v1/cluster/status",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
        respuesta.raise_for_status()
        return respuesta.json()

    def wait_for_nodes(self, expected: int, timeout: float = 20.0) -> None:
        """Espera a que los N nodos esten registrados y hayan latido al menos una vez."""
        limite = time.time() + timeout
        while time.time() < limite:
            try:
                ids = {n.health().get("data_node_id") for n in self.nodes}
                if len(ids) == expected and "sin-registrar" not in ids and None not in ids:
                    return
            except Exception:
                pass
            time.sleep(0.1)
        raise RuntimeError(f"los {expected} DataNodes no se registraron a tiempo")

    def stop(self) -> None:
        for nodo in self.nodes:
            nodo.stop()
        if self._control is not None:
            self._control.stop()
            self._control = None


def _esperar(url: str, timeout: float = 30.0) -> None:
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


def start_cluster(
    tmp_path: Path,
    block_size: int = MB,
    write_ttl_seconds: int = 600,
    data_nodes: int = 1,
    fault_domains: list[str] | None = None,
    capacities: list[int] | None = None,
    replication_factor: int = 1,
    write_quorum: int = 1,
    **control_overrides,
) -> Cluster:
    """Levanta el ControlNode y `data_nodes` DataNodes.

    `fault_domains` y `capacities` permiten construir los escenarios de la Etapa 2: dos
    zonas, o un nodo con menos capacidad que el resto.

    `replication_factor` y `write_quorum` se fijan aqui en 1 y NO se dejan al default del
    codigo, que desde la Etapa 3 es R=3 y W=2. El motivo es que las pruebas de las etapas
    anteriores describen el comportamiento con una replica por bloque: dejarlas heredar
    R=3 no las haria mejores, las haria medir otra cosa. Las pruebas de replicacion piden
    R=3 explicitamente, que es como debe ser: quien necesita tres nodos, los levanta.
    """
    puerto_control = puerto_libre()
    puerto_grpc = puerto_libre()
    control_url = f"http://127.0.0.1:{puerto_control}"

    control_settings = ControlNodeSettings(
        db_url=f"sqlite:///{(tmp_path / 'dfsha.db').as_posix()}",
        jwt_secret=SECRETO_JWT,
        internal_secret=SECRETO_INTERNO,
        block_size=block_size,
        write_ttl_seconds=write_ttl_seconds,
        log_level="WARNING",
        grpc_port=puerto_grpc,
        replication_factor=replication_factor,
        write_quorum=write_quorum,
        **control_overrides,
    )

    control = _ServidorEnHilo(create_control_app(control_settings), puerto_control)
    control.start()
    _esperar(f"{control_url}/health")

    dominios = fault_domains or [f"local-{i + 1}" for i in range(data_nodes)]
    capacidades = capacities or [512 * MB] * data_nodes

    handles: list[DataNodeHandle] = []
    for indice in range(data_nodes):
        puerto = puerto_libre()
        url = f"http://127.0.0.1:{puerto}"
        data_dir = tmp_path / f"datanode-{indice + 1}"
        settings = DataNodeSettings(
            data_dir=str(data_dir),
            control_url=control_url,
            control_grpc_url=f"127.0.0.1:{puerto_grpc}",
            datanode_advertise_url=url,
            datanode_fault_domain=dominios[indice],
            internal_secret=SECRETO_INTERNO,
            datanode_capacity_bytes=capacidades[indice],
            log_level="WARNING",
            register_retry_seconds=0.1,
            register_max_attempts=50,
        )
        handle = DataNodeHandle(
            name=f"data-node-{indice + 1}",
            url=url,
            port=puerto,
            data_dir=data_dir,
            fault_domain=dominios[indice],
            settings=settings,
        )
        handle.start()
        handles.append(handle)

    cluster = Cluster(
        control_url=control_url,
        control_grpc_port=puerto_grpc,
        nodes=handles,
        _control=control,
        settings=control_settings,
    )
    cluster.wait_for_nodes(data_nodes)
    return cluster
