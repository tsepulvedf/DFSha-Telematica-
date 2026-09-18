"""Token de bloque: la autorizacion que el DataNode puede comprobar por su cuenta.

## El agujero que cierra

Sin esto, las ACLs del Bloque C son **comprobaciones que se saltan pidiendo el bloque
directamente al DataNode**. El ControlNode resuelve permisos con cuidado, decide que un
usuario no puede leer `/de-otro/secreto.bin`... y el DataNode sirve `GET /blocks/{id}` a
cualquiera que sepa el id. Los bytes van directos entre cliente y DataNode (decision 4 de
la seccion 1), asi que el unico sitio donde se comprobaba el permiso no esta en el camino
de los datos.

Un `block_id` es un UUID y no se adivina, pero **eso no es autorizacion**: aparece en
cualquier plan que el ControlNode haya entregado alguna vez, en los logs, y en el metadato
de un archivo que se dejo de compartir. Seguridad por desconocimiento del identificador es
justo lo que un sistema con ACLs explicitas no deberia necesitar.

## Como funciona

El ControlNode **firma** una autorizacion corta —este bloque, esta operacion, hasta esta
hora— y el DataNode la **verifica sin preguntarle a nadie**. No hay viaje extra por
bloque: el token viaja en el mismo plan que ya se entregaba.

    token = base64url(payload) . base64url(firma) . base64url(certificado del firmante)

Las tres partes, y por que la tercera:

- **payload**: `block_id`, operacion, expiracion y sujeto. JSON compacto.
- **firma**: RSA-PSS SHA-256 sobre los bytes del payload **ya codificados**, no sobre el
  JSON reserializado. Firmar lo que se transmite evita depender de que dos versiones de
  Python ordenen las claves igual.
- **certificado**: el del ControlNode que firmo. Va dentro para que el DataNode no
  necesite tenerlo configurado: le basta **la CA**, que ya tiene por el mTLS. Rotar el
  certificado del ControlNode no obliga a tocar ningun DataNode, que es la propiedad que
  hace esto mantenible. Es la misma idea que `x5c` en JWS.

## NO ES UN SECRETO COMPARTIDO, y la diferencia importa

`DFSHA_INTERNAL_SECRET` desaparecio en el Bloque C y esto **no lo reintroduce por la
puerta de atras**. Un HMAC con clave compartida obligaria a que el DataNode tuviera la
misma clave con la que se firma, y entonces **el DataNode podria emitir tokens**: podria
autorizarse a si mismo cualquier bloque, y quien comprometa un DataNode se lleva la
capacidad de firmar. Con una firma asimetrica el DataNode solo puede **verificar**. La
clave privada no sale del ControlNode.

## Firmado por la CA no basta. Esta es la trampa

La misma CA firmo `dfsha-control`, `dfsha-data` y `dfsha-client`. Si la verificacion se
quedara en «el certificado del firmante encadena a nuestra CA», **cualquier DataNode
podria emitir tokens con su propio certificado** y el de al lado se los aceptaria: se
habria construido un sistema de autorizacion que autoriza a los que debia limitar.

Por eso la verificacion exige **dos** cosas, y las dos son obligatorias:

1. Que el certificado del firmante encadene a nuestra CA.
2. Que su Common Name sea `dfsha-control`, es decir, que el firmante sea **el rol que
   tiene derecho a decidir**.

Lo fija `test_un_datanode_no_puede_firmar_sus_propios_tokens`.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

__all__ = [
    "BLOCK_TOKEN_HEADER",
    "SIGNER_COMMON_NAME",
    "Operation",
    "BlockGrant",
    "BlockTokenError",
    "TokenSigner",
    "verify_token",
]

#: Cabecera por la que viaja. Una sola para las tres operaciones: lo que distingue un
#: permiso de lectura de uno de escritura va DENTRO del token, firmado, no en el nombre
#: de la cabecera, que cualquiera puede cambiar.
BLOCK_TOKEN_HEADER = "X-DFSha-Block-Token"

#: El unico rol cuya firma vale. Ver el docstring del modulo: la CA firmo tres roles y
#: solo uno tiene autoridad para autorizar. `gen_certs.py` lo pone como Common Name.
SIGNER_COMMON_NAME = "dfsha-control"

Operation = Literal["read", "write", "delete"]

#: Vida por defecto. Corta porque un token es un permiso que ya no se puede retirar: no
#: hay revocacion, asi que la ventana en la que un token filtrado sirve es exactamente
#: esto. Diez minutos cubre de sobra la subida o bajada de un bloque de 64 MB y coincide
#: con el TTL de las reservas de escritura, que es el otro reloj de la misma operacion.
DEFAULT_TTL_SECONDS = 600


class BlockTokenError(Exception):
    """El token no autoriza esta peticion. Da igual el motivo: el DataNode responde 403.

    A proposito **no se distingue** hacia fuera entre «firma invalida», «caducado» y «es
    para otro bloque». Quien prueba tokens no deberia recibir pistas sobre cual de sus
    intentos estuvo mas cerca. El motivo concreto va al log del DataNode, que es donde
    hace falta para diagnosticar.
    """


@dataclass(frozen=True, slots=True)
class BlockGrant:
    """Lo que el token afirma: quien puede hacer que, sobre que bloque y hasta cuando."""

    block_id: str
    operation: Operation
    expires_at: dt.datetime
    #: Para el log del DataNode. NO se usa para decidir: la decision ya la tomo el
    #: ControlNode al firmar. Sirve para poder responder «quien bajo este bloque».
    subject: str = ""

    def _payload(self) -> bytes:
        return json.dumps(
            {
                "b": self.block_id,
                "o": self.operation,
                "e": int(self.expires_at.timestamp()),
                "s": self.subject,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64u(texto: str) -> bytes:
    return base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))


#: RSA-PSS y no PKCS#1 v1.5: es lo que se recomienda para firmas nuevas, y aqui no hay
#: ningun verificador antiguo al que haya que ser compatible.
_PADDING = padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32)


class TokenSigner:
    """Firma tokens con la clave del ControlNode. Vive solo en el ControlNode.

    Carga la clave y el certificado UNA vez al arrancar. Hacerlo por peticion leeria dos
    ficheros y reconstruiria la clave en cada bloque de cada `put`, que con un archivo de
    mil bloques son dos mil lecturas de disco para el mismo resultado.
    """

    def __init__(self, key_pem: bytes, cert_pem: bytes) -> None:
        clave = serialization.load_pem_private_key(key_pem, password=None)
        if not isinstance(clave, rsa.RSAPrivateKey):
            raise ValueError("la clave del ControlNode no es RSA")
        self._key = clave
        cert = x509.load_pem_x509_certificate(cert_pem)
        self._cert_der = cert.public_bytes(serialization.Encoding.DER)
        self._cert = cert

    @classmethod
    def from_paths(cls, key: str, cert: str) -> "TokenSigner":
        from pathlib import Path

        return cls(Path(key).read_bytes(), Path(cert).read_bytes())

    def issue(
        self,
        block_id: str,
        operation: Operation,
        *,
        now: dt.datetime,
        subject: str = "",
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> str:
        grant = BlockGrant(
            block_id=block_id,
            operation=operation,
            expires_at=now + dt.timedelta(seconds=ttl_seconds),
            subject=subject,
        )
        payload = _b64u(grant._payload())
        firma = self._key.sign(payload.encode(), _PADDING, hashes.SHA256())
        return f"{payload}.{_b64u(firma)}.{_b64u(self._cert_der)}"


@lru_cache(maxsize=8)
def _firmante_valido(ca_der: bytes, signer_der: bytes) -> bool:
    """Que este certificado lo firmo nuestra CA. En cache porque no cambia.

    Solo se cachea la comprobacion de la **cadena**, que es criptografia pura sobre dos
    bytes fijos. La **ventana de validez** se comprueba fuera, en cada peticion: si
    estuviera aqui dentro, un certificado caducado seguiria valiendo mientras la entrada
    siguiera en cache.
    """
    ca = x509.load_der_x509_certificate(ca_der)
    firmante = x509.load_der_x509_certificate(signer_der)
    try:
        ca.public_key().verify(
            firmante.signature,
            firmante.tbs_certificate_bytes,
            padding.PKCS1v15(),
            firmante.signature_hash_algorithm,
        )
    except (InvalidSignature, TypeError, ValueError):
        return False
    return True


def verify_token(
    token: str | None,
    *,
    ca_cert_pem: bytes,
    block_id: str,
    operation: Operation,
    now: dt.datetime,
) -> BlockGrant:
    """Comprueba que el token autoriza ESTA operacion sobre ESTE bloque. O lanza.

    El `block_id` y la operacion se pasan como argumento y se comparan contra lo firmado,
    en vez de devolver el grant para que el llamante mire: es la misma regla que
    `directory_for` y que la epoca del Bloque A. Una funcion que responde «¿que dice este
    token?» se puede ignorar; una que hace «falla si no autoriza esto» no.
    """
    if not token:
        raise BlockTokenError("falta el token de bloque")

    partes = token.split(".")
    if len(partes) != 3:
        raise BlockTokenError("el token no tiene las tres partes")
    payload_b64, firma_b64, cert_b64 = partes

    try:
        cert_der = _unb64u(cert_b64)
        firmante = x509.load_der_x509_certificate(cert_der)
    except Exception as exc:  # noqa: BLE001 - cualquier basura entra por aqui
        raise BlockTokenError("el certificado del firmante no se pudo leer") from exc

    ca = x509.load_pem_x509_certificate(ca_cert_pem)
    if not _firmante_valido(ca.public_bytes(serialization.Encoding.DER), cert_der):
        raise BlockTokenError("el certificado del firmante no lo emitio nuestra CA")

    # Firmado por la CA NO BASTA: la misma CA firmo a los DataNodes y al cliente. Sin
    # esta linea, cualquier DataNode podria emitirse tokens para cualquier bloque.
    comunes = firmante.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)
    if not comunes or comunes[0].value != SIGNER_COMMON_NAME:
        raise BlockTokenError("el firmante no es el ControlNode")

    if not (firmante.not_valid_before_utc <= now <= firmante.not_valid_after_utc):
        raise BlockTokenError("el certificado del firmante esta fuera de su validez")

    try:
        firmante.public_key().verify(
            _unb64u(firma_b64), payload_b64.encode(), _PADDING, hashes.SHA256()
        )
    except (InvalidSignature, Exception) as exc:  # noqa: BLE001
        raise BlockTokenError("la firma no cuadra") from exc

    try:
        datos = json.loads(_unb64u(payload_b64))
        grant = BlockGrant(
            block_id=str(datos["b"]),
            operation=datos["o"],
            expires_at=dt.datetime.fromtimestamp(int(datos["e"]), dt.timezone.utc),
            subject=str(datos.get("s", "")),
        )
    except Exception as exc:  # noqa: BLE001
        raise BlockTokenError("el contenido del token no se pudo leer") from exc

    # Atado a SU bloque y a SU operacion. Sin lo primero, un token de lectura de un
    # bloque propio abriria cualquier otro; sin lo segundo, leer autorizaria a borrar.
    if grant.block_id != block_id:
        raise BlockTokenError("el token es para otro bloque")
    if grant.operation != operation:
        raise BlockTokenError("el token no autoriza esta operacion")
    if grant.expires_at <= now:
        raise BlockTokenError("el token ha caducado")

    return grant
