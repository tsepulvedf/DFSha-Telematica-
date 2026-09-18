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

from dfsha.common.serve import ClientTls
from dfsha.common.tls import ca_only_context
from dfsha.control_node.config import ControlNodeSettings
from dfsha.control_node.main import create_app as create_control_app
from dfsha.data_node.config import DataNodeSettings
from dfsha.data_node.main import create_app as create_data_app
from tests.certs import material

__all__ = ["Cluster", "DataNodeHandle", "start_cluster", "MB"]

MB = 1024 * 1024
SECRETO_JWT = "pruebas-jwt-no-es-un-secreto-real"
SECRETO_INTERNO = "pruebas-interno-no-es-un-secreto-real"


def puerto_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _verify(url: str):
    """Como verificar `url` desde las pruebas: la CA de pruebas si es https."""
    return ca_only_context(material("data").ca_cert) if url.startswith("https://") else True


def _tls_cliente(rol: str) -> dict:
    """Los mismos kwargs de uvicorn que usa `python -m` con C2 encendido."""
    m = material(rol)
    return ClientTls(str(m.cert), str(m.key)).uvicorn_kwargs()


class _ServidorEnHilo:
    def __init__(self, app, port: int, ssl_kwargs: dict | None = None) -> None:
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
            **(ssl_kwargs or {}),
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
    #: TLS de cliente (C2) en el puerto que sirve al cliente y a los pares.
    ssl_kwargs: dict | None = None

    def health(self, timeout: float = 5) -> dict:
        return httpx.get(
            f"{self.url}/health", timeout=timeout, verify=_verify(self.url)
        ).json()

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
        self._servidor = _ServidorEnHilo(
            create_data_app(self.settings), self.port, self.ssl_kwargs
        )
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
    #: Plano interno, en su propio puerto y con TLS mutuo.
    control_internal_url: str = ""
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

    def internal_client(self, rol: str = "client") -> httpx.Client:
        """Cliente del plano interno con certificado, como lo usaria el recolector.

        El secreto compartido de las etapas anteriores ya no existe: quien no presente un
        certificado de la CA no llega ni a enviar la peticion.
        """
        tls = material(rol)
        return httpx.Client(
            base_url=self.control_internal_url,
            verify=tls.httpx_verify(),
            timeout=30,
        )

    def uow_factory(self):
        """Acceso al metadato del ControlNode desde una prueba.

        Solo para montar escenarios que por el camino normal costarian minutos de reloj
        (por ejemplo, llegar a un bloque sub-replicado sin esperar a que muera un nodo).
        Lo que se COMPRUEBA se sigue comprobando por la API.
        """
        from dfsha.control_node.repositories.database import (
            build_engine,
            build_session_factory,
        )
        from dfsha.control_node.repositories.sql import SqlUnitOfWork

        assert self.settings is not None
        engine = build_engine(self.settings.db_url)
        factory = build_session_factory(engine)
        return lambda: SqlUnitOfWork(factory)

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
            verify=_verify(self.control_url),
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
            if httpx.get(url, timeout=2, verify=_verify(url)).status_code == 200:
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
    advertise_muerta: bool = False,
    tls_cliente: bool = False,
    **control_overrides,
) -> Cluster:
    """Levanta el ControlNode y `data_nodes` DataNodes.

    `fault_domains` y `capacities` permiten construir los escenarios de la Etapa 2: dos
    zonas, o un nodo con menos capacidad que el resto.

    `advertise_muerta=True` da a cada nodo una direccion de cliente que **no responde**,
    dejando la real solo como direccion de par. Es la inversion del escenario de Docker:
    alli la de cliente funciona desde fuera y no desde dentro; aqui no funciona desde
    ningun sitio. En los dos casos, cualquier camino nodo-a-nodo que use la direccion de
    cliente falla, que es justo lo que hay que poder detectar. Ver
    `test_addressing.py`.

    `replication_factor` y `write_quorum` se fijan aqui en 1 y NO se dejan al default del
    codigo, que desde la Etapa 3 es R=3 y W=2. El motivo es que las pruebas de las etapas
    anteriores describen el comportamiento con una replica por bloque: dejarlas heredar
    R=3 no las haria mejores, las haria medir otra cosa. Las pruebas de replicacion piden
    R=3 explicitamente, que es como debe ser: quien necesita tres nodos, los levanta.

    `tls_cliente=True` enciende C2 en el ControlNode y en TODOS los DataNodes, con el
    mismo `ClientTls` que usa `python -m`. Las direcciones pasan a `https://`, tambien la
    de par, asi que el pipeline y la re-replicacion viajan por TLS verificando la CA.
    Las pruebas de C2 anteriores levantaban un solo servidor suelto y no recorrian ese
    camino, que es por donde se colaba el fallo. Ver `test_tls_cliente.py`.
    """
    esquema = "https" if tls_cliente else "http"
    puerto_control = puerto_libre()
    puerto_grpc = puerto_libre()
    puerto_interno = puerto_libre()
    control_url = f"{esquema}://127.0.0.1:{puerto_control}"

    control_settings = ControlNodeSettings(
        db_url=f"sqlite:///{(tmp_path / 'dfsha.db').as_posix()}",
        jwt_secret=SECRETO_JWT,
        tls_ca_cert=str(material("control").ca_cert),
        tls_cert=str(material("control").cert),
        tls_key=str(material("control").key),
        internal_port=puerto_interno,
        block_size=block_size,
        write_ttl_seconds=write_ttl_seconds,
        log_level="WARNING",
        grpc_port=puerto_grpc,
        replication_factor=replication_factor,
        write_quorum=write_quorum,
        **control_overrides,
    )

    control = _ServidorEnHilo(
        create_control_app(control_settings),
        puerto_control,
        _tls_cliente("control") if tls_cliente else None,
    )
    control.start()
    _esperar(f"{control_url}/health")

    dominios = fault_domains or [f"local-{i + 1}" for i in range(data_nodes)]
    capacidades = capacities or [512 * MB] * data_nodes

    handles: list[DataNodeHandle] = []
    for indice in range(data_nodes):
        puerto = puerto_libre()
        url = f"{esquema}://127.0.0.1:{puerto}"
        data_dir = tmp_path / f"datanode-{indice + 1}"
        # Con `advertise_muerta`, la direccion de cliente apunta a un puerto que nadie
        # escucha: si algun camino nodo-a-nodo la usara, se veria enseguida.
        anunciada = f"{esquema}://127.0.0.1:{puerto_libre()}" if advertise_muerta else url
        settings = DataNodeSettings(
            data_dir=str(data_dir),
            control_url=control_url,
            control_grpc_url=f"127.0.0.1:{puerto_grpc}",
            datanode_advertise_url=anunciada,
            datanode_peer_url=url if advertise_muerta else "",
            datanode_fault_domain=dominios[indice],
            control_internal_url=f"https://127.0.0.1:{puerto_interno}",
            tls_ca_cert=str(material("data").ca_cert),
            tls_cert=str(material("data").cert),
            tls_key=str(material("data").key),
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
            ssl_kwargs=_tls_cliente("data") if tls_cliente else None,
        )
        handle.start()
        handles.append(handle)

    cluster = Cluster(
        control_url=control_url,
        control_grpc_port=puerto_grpc,
        control_internal_url=f"https://127.0.0.1:{puerto_interno}",
        nodes=handles,
        _control=control,
        settings=control_settings,
    )
    cluster.wait_for_nodes(data_nodes)
    return cluster
