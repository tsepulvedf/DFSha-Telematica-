"""TLS mutuo en el plano interno, comprobado abriendo sockets de verdad.

Es el criterio 10 de la Etapa 3: *toda la comunicacion entre nodos es mTLS; un nodo sin
certificado valido es rechazado.*

Estas pruebas no usan `TestClient`, y ese es justamente el punto. `TestClient` habla con
la aplicacion ASGI sin pasar por la red, asi que **no puede** comprobar nada de TLS: una
prueba de mTLS escrita con TestClient pasaria aunque el servidor aceptara a cualquiera.
Aqui se levanta el servidor real y se le conecta un cliente real.

Se comprueban las dos direcciones, porque son dos garantias distintas y es facil tener
una sola sin darse cuenta:

- **Servidor valida al cliente.** Sin certificado, o con uno de otra CA, la conexion se
  rechaza. Esto es lo que sustituye al secreto compartido.
- **Cliente valida al servidor.** Un cliente que no confia en nuestra CA no acepta al
  ControlNode. Esto es lo que impide que alguien se haga pasar por el.
"""

from __future__ import annotations

import ssl
import threading
import time

import grpc
import httpx
import pytest
import uvicorn

from dfsha.common.proto.gen import control_pb2, control_pb2_grpc
from dfsha.common.tls import grpc_channel_credentials
from dfsha.control_node.main import create_app, create_internal_app
from tests.certs import material, material_sin_firmar

from .cluster import puerto_libre
from .conftest import build_settings


@pytest.fixture()
def plano_interno(tmp_path):
    """El plano interno real, con TLS mutuo, en un puerto de verdad."""
    puerto = puerto_libre()
    ajustes = build_settings(tmp_path, internal_port=puerto, grpc_port=puerto_libre())
    tls = material("control")

    app = create_app(ajustes)
    interna = create_internal_app(app)

    config = uvicorn.Config(
        interna,
        host="127.0.0.1",
        port=puerto,
        log_level="error",
        access_log=False,
        ssl_certfile=str(tls.cert),
        ssl_keyfile=str(tls.key),
        ssl_ca_certs=str(tls.ca_cert),
        ssl_cert_reqs=ssl.CERT_REQUIRED,
    )
    servidor = uvicorn.Server(config)
    hilo = threading.Thread(target=servidor.run, daemon=True)
    hilo.start()

    limite = time.time() + 20
    while not servidor.started and time.time() < limite:
        time.sleep(0.05)
    assert servidor.started, "el plano interno no arranco"

    try:
        yield f"https://127.0.0.1:{puerto}"
    finally:
        servidor.should_exit = True
        hilo.join(timeout=10)


def test_con_certificado_de_la_ca_se_entra(plano_interno) -> None:
    """El caso bueno primero: si esto falla, las demas no prueban nada."""
    tls = material("client")
    # `tls.httpx_verify()` y no `verify=<ruta> + cert=tupla`: esa combinacion descarta el
    # certificado de cliente en silencio y esta prueba fallaria por el motivo equivocado.
    # Ver `common/tls.client_ssl_context`. Es el MISMO camino que usa el DataNode.
    with httpx.Client(verify=tls.httpx_verify(), timeout=15) as cliente:
        respuesta = cliente.get(f"{plano_interno}/internal/v1/gc/orphan-blocks")

    assert respuesta.status_code == 200


def test_sin_certificado_de_cliente_la_conexion_se_rechaza(plano_interno) -> None:
    """Lo que sustituye al secreto compartido.

    El fallo llega como error de TRANSPORTE, no como un 401: la peticion nunca llega a
    enviarse. Esa es la diferencia de fondo entre comprobar en el codigo y comprobar en
    el handshake — aqui no hay ninguna ruta que se pueda olvidar de mirar.
    """
    import ssl as _ssl

    tls = material("client")
    # Confia en la CA (para que el fallo sea por NO presentar certificado, no por no
    # reconocer al servidor) pero no carga ninguno propio.
    solo_ca = _ssl.create_default_context(cafile=str(tls.ca_cert))
    with httpx.Client(verify=solo_ca, timeout=15) as cliente:
        with pytest.raises(httpx.TransportError):
            cliente.get(f"{plano_interno}/internal/v1/gc/orphan-blocks")


def test_un_certificado_de_otra_ca_tampoco_vale(plano_interno) -> None:
    """La prueba que comprueba que la CA se valida de verdad.

    Un certificado bien formado, con su clave y todo, pero firmado por otra autoridad.
    Si solo se probara "sin certificado", un servidor que aceptara cualquier certificado
    pasaria igual.
    """
    intruso = material_sin_firmar()
    with httpx.Client(verify=intruso.httpx_verify(), timeout=15) as cliente:
        with pytest.raises(httpx.TransportError):
            cliente.get(f"{plano_interno}/internal/v1/gc/orphan-blocks")


def test_el_cliente_que_no_confia_en_la_ca_rechaza_al_servidor(plano_interno) -> None:
    """La otra direccion: sin esto, alguien podria hacerse pasar por el ControlNode.

    Se usa la verificacion por defecto del sistema, que no conoce nuestra CA.
    """
    import ssl as _ssl

    tls = material("client")
    # Contexto por defecto del sistema: presenta nuestro certificado pero NO conoce
    # nuestra CA, asi que rechaza al servidor.
    del_sistema = _ssl.create_default_context()
    del_sistema.load_cert_chain(str(tls.cert), str(tls.key))
    with httpx.Client(verify=del_sistema, timeout=15) as cliente:
        with pytest.raises(httpx.TransportError):
            cliente.get(f"{plano_interno}/internal/v1/gc/orphan-blocks")


# --- gRPC ------------------------------------------------------------------


@pytest.fixture()
def plano_grpc(tmp_path):
    """El servidor gRPC del plano de control, con TLS mutuo."""
    from dfsha.common.tls import TlsMaterial
    from dfsha.control_node.api.grpc import ControlPlaneServicer, build_grpc_server
    from dfsha.control_node.domain.membership import MembershipThresholds
    from dfsha.control_node.repositories.database import (
        build_engine,
        build_session_factory,
        prepare_schema,
    )
    from dfsha.control_node.repositories.sql import SqlUnitOfWork

    puerto = puerto_libre()
    engine = build_engine(f"sqlite:///{(tmp_path / 'grpc.db').as_posix()}")
    prepare_schema(engine)
    factory = build_session_factory(engine)

    servicer = ControlPlaneServicer(
        uow_factory=lambda: SqlUnitOfWork(factory),
        thresholds=MembershipThresholds.from_millis(10_000, 30_000),
        heartbeat_interval_ms=3000,
        full_report_every_n=20,
    )
    tls = material("control")
    servidor = build_grpc_server(
        servicer, puerto, max_workers=4, tls=TlsMaterial(tls.ca_cert, tls.cert, tls.key)
    )
    servidor.start()
    try:
        yield f"127.0.0.1:{puerto}"
    finally:
        servidor.stop(grace=1).wait(timeout=5)
        engine.dispose()


def _registrar(canal) -> control_pb2.RegisterResponse:
    stub = control_pb2_grpc.ControlPlaneStub(canal)
    return stub.Register(
        control_pb2.RegisterRequest(
            advertise_url="http://127.0.0.1:9999",
            fault_domain="zona-a",
            boot_id="boot-1",
            capacity_bytes=1024 * 1024,
        ),
        timeout=10,
    )


def test_un_datanode_con_certificado_se_registra(plano_grpc) -> None:
    with grpc.secure_channel(
        plano_grpc, grpc_channel_credentials(material("data"))
    ) as canal:
        respuesta = _registrar(canal)

    assert respuesta.data_node_id


def test_un_datanode_sin_certificado_es_rechazado(plano_grpc) -> None:
    """El criterio 10, en el plano de control.

    Sin `require_client_auth`, este canal inseguro se conectaria y el impostor recibiria
    los heartbeats del cluster entero.
    """
    with grpc.insecure_channel(plano_grpc) as canal:
        with pytest.raises(grpc.RpcError):
            _registrar(canal)


def test_un_datanode_con_certificado_de_otra_ca_es_rechazado(plano_grpc) -> None:
    intruso = material_sin_firmar()
    with grpc.secure_channel(plano_grpc, grpc_channel_credentials(intruso)) as canal:
        with pytest.raises(grpc.RpcError):
            _registrar(canal)
