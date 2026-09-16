"""Material TLS compartido: cargar certificados y armar los contextos.

Un solo sitio para esto porque los tres roles necesitan lo mismo con nombres distintos, y
porque la diferencia entre `CERT_REQUIRED` y `CERT_OPTIONAL` en el sitio equivocado es la
diferencia entre tener mTLS y creer que se tiene.

## Las dos direcciones, y por que las dos importan

**mTLS** no es "TLS pero mas". Son dos comprobaciones distintas:

- El **cliente valida al servidor**: que el certificado que presenta esta firmado por
  nuestra CA. Eso impide que alguien se haga pasar por el ControlNode y reciba los
  heartbeats del cluster, o por un DataNode y reciba bloques.
- El **servidor valida al cliente**: que quien llama tiene tambien un certificado de
  nuestra CA. Eso es lo que sustituye al secreto compartido, y es la direccion que casi
  todo el mundo se deja: un servidor con TLS pero sin `CERT_REQUIRED` acepta a cualquiera
  que sepa la URL.

## Por que se valida la CA y no el nombre del host

La verificacion de nombre esta activada para el cliente gRPC, pero el conjunto de nombres
lo fija `gen_certs.py` e incluye los de la red de compose. En AWS hay que anadir las IP
privadas con `--hosts`, y el sintoma de olvidarlo es un handshake que falla nombrando el
host que no cuadra, que es un error legible.
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from pathlib import Path

import grpc

__all__ = [
    "TlsMaterial",
    "client_ssl_context",
    "server_ssl_context",
    "grpc_server_credentials",
    "grpc_channel_credentials",
]


@dataclass(frozen=True, slots=True)
class TlsMaterial:
    """Los tres ficheros que necesita cualquier rol para hablar con los demas."""

    ca_cert: Path
    cert: Path
    key: Path

    @classmethod
    def from_paths(cls, ca_cert: str, cert: str, key: str) -> "TlsMaterial":
        return cls(ca_cert=Path(ca_cert), cert=Path(cert), key=Path(key))

    def read(self) -> tuple[bytes, bytes, bytes]:
        return (
            self.ca_cert.read_bytes(),
            self.cert.read_bytes(),
            self.key.read_bytes(),
        )

    def httpx_verify(self) -> ssl.SSLContext:
        """Lo que hay que pasarle a `httpx` como `verify=`.

        **Nunca `verify=<ruta>` con `cert=(crt, key)`.** Ver `client_ssl_context`: esa
        combinacion descarta el certificado de cliente sin avisar.
        """
        return client_ssl_context(self)


def client_ssl_context(material: TlsMaterial) -> ssl.SSLContext:
    """Contexto de cliente: confia en nuestra CA y presenta nuestro certificado.

    ## Por que se construye a mano y no con los parametros de httpx

    Porque `httpx.Client(verify="ca.crt", cert=("cli.crt", "cli.key"))` **descarta el
    certificado de cliente en silencio**. Comprobado con httpx 0.28.1 contra un servidor
    con `ssl_cert_reqs=CERT_REQUIRED`:

        verify=<ruta>      + cert=(crt, key)  -> el servidor cierra la conexion
        verify=SSLContext  + cert=(crt, key)  -> 200
        verify=SSLContext(con cert cargado)   -> 200

    `verify=<str>` esta deprecado en 0.28 y su camino construye el contexto por su cuenta
    **ignorando `cert`**. El unico aviso que emite httpx es sobre `verify`, no sobre el
    certificado que acaba de tirar, asi que el sintoma es un mTLS que parece funcionar:
    las conexiones se cifran, los intrusos se rechazan, y el cliente legitimo tambien.

    Es el mismo tipo de fallo que el direccionamiento del Bloque B: algo que **parece**
    configurado y no lo esta. Por eso hay un unico sitio donde se construye.
    """
    contexto = ssl.create_default_context(
        ssl.Purpose.SERVER_AUTH, cafile=str(material.ca_cert)
    )
    contexto.load_cert_chain(certfile=str(material.cert), keyfile=str(material.key))
    contexto.minimum_version = ssl.TLSVersion.TLSv1_2
    return contexto


def server_ssl_context(material: TlsMaterial, require_client_cert: bool = True) -> ssl.SSLContext:
    """Contexto para un servidor HTTPS.

    `require_client_cert=True` es lo que convierte esto en mTLS: sin un certificado
    firmado por nuestra CA, la conexion **ni siquiera llega a enviar la peticion**. Por
    eso el plano interno vive en su propio puerto: la exigencia la hace TLS, no el
    codigo, y no hay forma de saltarsela con una ruta mal protegida.

    `require_client_cert=False` para el puerto de cliente, donde la identidad la da el
    JWT y exigir certificado obligaria a repartir uno a cada usuario.
    """
    contexto = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    contexto.load_cert_chain(certfile=str(material.cert), keyfile=str(material.key))

    if require_client_cert:
        contexto.load_verify_locations(cafile=str(material.ca_cert))
        contexto.verify_mode = ssl.CERT_REQUIRED
    else:
        contexto.verify_mode = ssl.CERT_NONE

    # TLS 1.2 como minimo. Por debajo hay suites que no conviene ofrecer, y no hay ningun
    # cliente en este sistema que no llegue a 1.2: todos son Python del mismo repositorio.
    contexto.minimum_version = ssl.TLSVersion.TLSv1_2
    return contexto


def grpc_server_credentials(material: TlsMaterial) -> grpc.ServerCredentials:
    """Credenciales del servidor gRPC, exigiendo certificado de cliente.

    `require_client_auth=True` es el equivalente de `CERT_REQUIRED`. Sin el, el servidor
    cifraria la conexion pero aceptaria a cualquiera: tendriamos confidencialidad sin
    autenticacion, que para un plano de control es casi peor que nada, porque parece
    seguro.
    """
    ca, cert, key = material.read()
    return grpc.ssl_server_credentials(
        [(key, cert)],
        root_certificates=ca,
        require_client_auth=True,
    )


def grpc_channel_credentials(material: TlsMaterial) -> grpc.ChannelCredentials:
    """Credenciales del cliente gRPC: valida al servidor y se presenta a si mismo."""
    ca, cert, key = material.read()
    return grpc.ssl_channel_credentials(
        root_certificates=ca,
        private_key=key,
        certificate_chain=cert,
    )
