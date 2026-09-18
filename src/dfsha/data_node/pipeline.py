"""Pipeline de escritura en cadena entre DataNodes.

Con R=3, la alternativa ingenua es que el cliente suba el mismo bloque tres veces. Con
instancias `t3.micro` y un enlace domestico eso triplica el tiempo de un `put` y el
ancho de banda de subida del cliente, que es justo el recurso mas escaso. En cadena, el
cliente sube **una vez**: manda el bloque a la primera replica, esa lo reenvia a la
segunda y la segunda a la tercera.

## Por que hay un buffer, y por que no es una chapuza

El enunciado pide dos cosas que parecen reñirse:

- «cada nodo verifica el checksum **antes** de reenviar»
- «DN1 lo reenvia a DN2 **mientras** lo escribe»

No se puede verificar un SHA-256 hasta haber visto el ultimo byte, asi que reenviar en
streaming puro y verificar antes de reenviar son incompatibles. La lectura que satisface
las dos es la que se implementa aqui:

1. Se recibe el bloque entero y se comprueba su checksum. Nada sale de este nodo sin
   estar verificado, de modo que **la corrupcion no se propaga por la cadena**.
2. Escritura en disco y reenvio al siguiente ocurren **a la vez**, en paralelo: el
   reenvio no espera al `fsync`.

El coste es un bloque en memoria por subida concurrente, que ya era el comportamiento del
DataNode desde la Etapa 1 (`routers.put_block` materializaba el cuerpo en una lista) y
que este modulo no empeora. Con el default de 64 MB y cuatro subidas a la vez son 256 MB,
que en `t3.micro` es mucho: quien lo despliegue ahi deberia bajar `DFSHA_BLOCK_SIZE`.

## Quien cuenta las replicas de verdad

La cabecera `X-DFSha-Replicas-Acked` que sale de aqui es **informativa**: sirve para el
log `replication.pipeline` y para que el cliente sepa que esperar. La autoridad sobre
cuantas replicas hay es el ControlNode, porque cada nodo de la cadena le notifica su
bloque por `/internal/v1/blocks/{id}/stored` antes de responder. El quorum del `commit`
se decide alli, no aqui.

## Un fallo aguas abajo NO tumba la subida

Si este nodo escribio bien pero el siguiente falla, se responde 201 igualmente con un
`acked` menor. Fallar la peticion convertiria W=3 en el minimo de hecho, que es lo
contrario de lo que se decidio: un archivo con 2 de 3 replicas no esta roto, todavia
tolera perder un nodo. El hueco lo recoge la cola de re-replicacion.
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass

import httpx

from dfsha.common.blocktoken import BLOCK_TOKEN_HEADER
from dfsha.common.logging import get_logger

__all__ = [
    "PIPELINE_HEADER",
    "ACKED_HEADER",
    "CHECKSUM_HEADER",
    "MAX_HOPS",
    "PipelineResult",
    "parse_pipeline",
    "forward",
]

#: Resto de la cadena, separado por comas: las URL base de los DataNodes que faltan.
PIPELINE_HEADER = "X-DFSha-Pipeline"
#: Cuantas copias quedaron confirmadas contando desde este nodo hacia abajo.
ACKED_HEADER = "X-DFSha-Replicas-Acked"
CHECKSUM_HEADER = "X-DFSha-Checksum"

#: Tope de saltos. El token de bloque autoriza a escribir ESE bloque, no a fijar la
#: longitud de la cadena: quien tenga un token valido podria mandar una cadena de mil
#: nodos y convertir un PUT en una tormenta de trafico entre DataNodes. Son dos cosas
#: distintas —autorizacion y limite de recursos— y el token solo cubre la primera. R=3
#: necesita 2 saltos; 4 deja margen sin dejar la puerta abierta.
MAX_HOPS = 4


@dataclass(frozen=True, slots=True)
class PipelineResult:
    """Lo que consiguio el reenvio, visto desde este nodo."""

    #: Replicas confirmadas aguas abajo. No incluye este nodo.
    downstream_acked: int
    #: Cuantos nodos quedaban en la cadena.
    hops: int
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def parse_pipeline(cabecera: str | None) -> list[str]:
    """Trocea la cabecera en la lista de destinos que faltan.

    Descarta vacios y recorta a `MAX_HOPS`. No valida que sean URL alcanzables: eso lo
    dira el intento de reenvio, y con mejor mensaje.
    """
    if not cabecera:
        return []
    destinos = [u.strip().rstrip("/") for u in cabecera.split(",")]
    return [u for u in destinos if u][:MAX_HOPS]


def forward(
    block_id: str,
    datos: bytes,
    checksum: str,
    cadena: list[str],
    timeout: float = 120.0,
    token: str = "",
    verify: ssl.SSLContext | bool = True,
) -> PipelineResult:
    """Manda el bloque al siguiente de la cadena con el resto de la cadena detras.

    Se llama **despues** de verificar el checksum: lo que sale de este nodo ya esta
    comprobado, asi que un bloque corrupto no puede propagarse.

    Nunca lanza. Un fallo aguas abajo es una replica de menos, no un error de la subida:
    quien llama responde 201 igual y el hueco lo recoge la re-replicacion.

    **El token de bloque se reenvia tal cual**, no se genera uno nuevo. No podria: la
    clave que firma vive solo en el ControlNode, y esa es justamente la propiedad que
    hace que un DataNode comprometido no pueda autorizarse nada. El token que el cliente
    presento vale para ESTE bloque, con `write`, hasta su expiracion, asi que sirve igual
    en cada salto de la cadena: los tres nodos estan escribiendo el mismo bloque por
    orden del mismo ControlNode.
    """
    log = get_logger("data_node")

    if not cadena:
        return PipelineResult(downstream_acked=0, hops=0)

    siguiente, resto = cadena[0], cadena[1:]
    cabeceras = {
        CHECKSUM_HEADER: checksum,
        "Content-Type": "application/octet-stream",
    }
    if resto:
        cabeceras[PIPELINE_HEADER] = ",".join(resto)
    if token:
        cabeceras[BLOCK_TOKEN_HEADER] = token

    try:
        respuesta = httpx.put(
            f"{siguiente}/api/v1/blocks/{block_id}",
            content=datos,
            headers=cabeceras,
            timeout=timeout,
            # La CA del proyecto: con C2 el vecino habla https y su certificado no lo
            # conoce el almacen del sistema. Ver `peer_verify` en main.py.
            verify=verify,
        )
    except httpx.HTTPError as exc:
        log.warning(
            "replication.forward_failed",
            block_id=block_id,
            next_hop=siguiente,
            remaining=len(cadena),
            error=type(exc).__name__,
            error_detail=str(exc),
        )
        return PipelineResult(
            downstream_acked=0, hops=len(cadena), error=type(exc).__name__
        )

    if respuesta.status_code == httpx.codes.CONFLICT:
        # El bloque ya estaba ahi. Los bloques son inmutables, asi que el que hay es el
        # mismo: cuenta como replica. Pasa en un reintento del cliente tras un fallo
        # parcial, y tratarlo como error convertiria un reintento inocuo en una subida
        # que nunca alcanza el quorum.
        log.info(
            "replication.forward_already_present",
            block_id=block_id,
            next_hop=siguiente,
            detail="el siguiente nodo ya tenia el bloque; cuenta como replica",
        )
        return PipelineResult(downstream_acked=1, hops=len(cadena))

    if respuesta.status_code >= 400:
        log.warning(
            "replication.forward_rejected",
            block_id=block_id,
            next_hop=siguiente,
            status=respuesta.status_code,
            remaining=len(cadena),
        )
        return PipelineResult(
            downstream_acked=0, hops=len(cadena), error=f"http_{respuesta.status_code}"
        )

    # El siguiente nodo informa de cuantas replicas quedaron de el hacia abajo. Se suma
    # la suya. Si no manda la cabecera (version antigua), se cuenta solo la suya.
    aguas_abajo = _leer_acked(respuesta.headers.get(ACKED_HEADER))
    return PipelineResult(downstream_acked=aguas_abajo, hops=len(cadena))


def _leer_acked(valor: str | None) -> int:
    """El `acked` que devolvio el siguiente nodo. Ante cualquier cosa rara, 1.

    Un 1 es lo que sabemos con certeza: respondio 201, o sea que el bloque esta en su
    disco. Contar de menos hace que el cliente vea menos replicas de las que hay, lo que
    como mucho provoca un reintento; contar de mas le haria creer que alcanzo el quorum
    cuando no.
    """
    if not valor:
        return 1
    try:
        return max(1, int(valor))
    except ValueError:
        return 1
