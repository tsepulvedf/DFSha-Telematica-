"""API del DataNode: bloques opacos y estado del disco."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Annotated

from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from dfsha.common.blocktoken import (
    BLOCK_TOKEN_HEADER,
    BlockTokenError,
    Operation,
    verify_token,
)
from dfsha.common.checksum import Sha256Accumulator, checksum_matches
from dfsha.common.dto import HealthResponse
from dfsha.common.errors import ChecksumMismatchError, StorageError
from dfsha.common.logging import get_logger, timed

from ..pipeline import ACKED_HEADER, forward, parse_pipeline

__all__ = ["blocks_router", "health_router", "CHECKSUM_HEADER"]

CHECKSUM_HEADER = "X-DFSha-Checksum"

blocks_router = APIRouter(prefix="/api/v1", tags=["bloques"])
health_router = APIRouter(tags=["operacion"])


def _autorizar(request: Request, block_id: str, operacion: Operation, token: str | None):
    """Exige un token de bloque valido para esta operacion. O responde 403.

    **Esta funcion es lo unico que hace que las ACLs del Bloque C sean ciertas.** El
    ControlNode resuelve permisos sobre rutas, pero los bytes van directos entre cliente y
    DataNode (decision 4 de la seccion 1), asi que sin esto el permiso se comprueba en un
    sitio por el que los datos no pasan: bastaria pedir `GET /blocks/{id}` para saltarselo.

    Se pasa el `block_id` y la operacion como ARGUMENTOS, igual que el minimo de
    `directory_for` y que la epoca del Bloque A: la funcion no responde «que dice este
    token», hace «falla si no autoriza esto». Un token de lectura de un bloque propio no
    abre otro bloque, y ninguno de lectura autoriza a borrar.

    Sin CA configurada no se exige nada, que es el mismo interruptor que apaga la firma en
    el ControlNode. Las dos mitades se encienden juntas o no se encienden.
    """
    ca = getattr(request.app.state, "block_token_ca", None)
    if ca is None:
        return None

    try:
        return verify_token(
            token,
            ca_cert_pem=ca,
            block_id=block_id,
            operation=operacion,
            now=datetime.now(timezone.utc),
        )
    except BlockTokenError as error:
        # El motivo concreto va al LOG y no a la respuesta: quien prueba tokens no debe
        # recibir pistas sobre cual de sus intentos estuvo mas cerca.
        get_logger("data_node").warning(
            "block.token_rejected",
            block_id=block_id,
            operation=operacion,
            reason=str(error),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="no autorizado para este bloque",
        ) from error


@blocks_router.put("/blocks/{block_id}", status_code=status.HTTP_201_CREATED)
async def put_block(
    block_id: str,
    request: Request,
    x_dfsha_checksum: Annotated[str | None, Header()] = None,
    x_dfsha_pipeline: Annotated[str | None, Header()] = None,
    x_dfsha_block_token: Annotated[str | None, Header()] = None,
) -> Response:
    """Recibe un bloque, lo persiste si el checksum cuadra y lo reenvia al siguiente.

    `X-DFSha-Pipeline` trae el RESTO de la cadena: las URL de los DataNodes que faltan.
    Con R=3, el cliente sube a la primera replica con las otras dos en esa cabecera, y
    cada nodo reenvia a quien le sigue quitandose de la lista. El cliente sube los bytes
    una sola vez.

    Orden deliberado: **verificar, luego escribir y reenviar en paralelo**. Verificar
    antes de reenviar impide que un bloque corrupto se propague por la cadena; hacer el
    reenvio a la vez que el `fsync` evita que la cadena sea la suma de las latencias de
    disco de tres nodos. Ver `pipeline.py` para por que esto obliga a un buffer.

    ## Por que todo el trabajo bloqueante sale del bucle de eventos

    Esto no es higiene, es correccion, y costo un interbloqueo descubrirlo. Escribir en
    disco y esperar al reenvio son operaciones sincronas; hacerlas dentro de un `async
    def` deja el bucle de eventos de este nodo parado y el nodo **deja de aceptar
    peticiones mientras tanto**. Con dos subidas concurrentes eso se convierte en una
    espera circular:

        bloque A: cliente -> DN1 -> DN2 -> DN3
        bloque B: cliente -> DN2 -> DN1 -> DN4

    DN1 se queda esperando a que DN2 le conteste, DN2 esperando a que le conteste DN1, y
    ninguno de los dos puede atender al otro porque su bucle esta ocupado esperando.
    Ambas subidas mueren por timeout. Sacarlo a un hilo del pool deja el bucle libre
    para atender el reenvio del vecino mientras este nodo escribe.
    """
    # ANTES de leer el cuerpo: sin esto, un no autorizado podria hacer que el nodo se
    # tragara 64 MB por la red antes de rechazarlo. Autorizar primero convierte un abuso
    # de ancho de banda en una respuesta corta.
    _autorizar(request, block_id, "write", x_dfsha_block_token)

    if not x_dfsha_checksum:
        raise ChecksumMismatchError(f"falta la cabecera {CHECKSUM_HEADER}", block_id=block_id)

    # El cuerpo llega como un generador asincrono y el almacenamiento escribe de forma
    # sincrona, asi que se materializa por trozos en una lista de bloques de memoria
    # acotada por el propio tamano de bloque acordado.
    trozos: list[bytes] = []
    async for chunk in request.stream():
        if chunk:
            trozos.append(chunk)

    cadena = parse_pipeline(x_dfsha_pipeline)

    meta, acked = await run_in_threadpool(
        _escribir_y_reenviar,
        request.app,
        block_id,
        trozos,
        x_dfsha_checksum,
        cadena,
        x_dfsha_block_token or "",
    )

    return Response(
        status_code=status.HTTP_201_CREATED,
        headers={
            CHECKSUM_HEADER: meta.checksum_sha256,
            ACKED_HEADER: str(acked),
        },
    )


def _escribir_y_reenviar(
    app, block_id: str, trozos: list[bytes], checksum: str, cadena, token: str = ""
):
    """Todo lo bloqueante del PUT, fuera del bucle de eventos. Ver `put_block`."""
    storage = app.state.storage
    control = app.state.control
    data_node_id = app.state.data_node_id
    log = get_logger("data_node")

    # Verificacion antes de nada: si el bloque llego corrupto, no se escribe NI se
    # reenvia. Propagarlo por la cadena convertiria un error de red en tres copias malas.
    if cadena:
        acumulador = Sha256Accumulator()
        for chunk in trozos:
            acumulador.update(chunk)
        if not checksum_matches(checksum, acumulador.hexdigest):
            raise ChecksumMismatchError(
                "el checksum no coincide con los bytes recibidos; no se reenvia",
                block_id=block_id,
                expected=checksum,
                actual=acumulador.hexdigest,
            )

    resultado = None
    with timed("block.write", logger=log, block_id=block_id, data_node_id=data_node_id) as t:
        # El contador de escrituras en vuelo alimenta el heartbeat, y con el la politica
        # de colocacion: es lo que desempata entre dos nodos igual de llenos.
        with app.state.load.write() as escritura:
            if cadena:
                # A la vez, no en secuencia: el reenvio no espera al fsync local.
                with ThreadPoolExecutor(max_workers=1) as bomba:
                    envio = bomba.submit(
                        forward, block_id, b"".join(trozos), checksum, cadena, token=token
                    )
                    meta = storage.write(block_id, iter(trozos), checksum)
                    resultado = envio.result()
            else:
                meta = storage.write(block_id, iter(trozos), checksum)
            escritura.size = meta.size
        t.bind(size_bytes=meta.size)
        # Se lee aqui dentro: `duration_ms` es un cronometro vivo, y leerlo fuera del
        # `with` incluiria el coste de emitir el propio log de block.write.
        duracion_ms = t.duration_ms

    #: Este nodo mas lo que confirmo la cadena. Informativo: la autoridad sobre cuantas
    #: replicas hay es el ControlNode, que recibe un aviso de cada nodo por separado.
    acked = 1 + (resultado.downstream_acked if resultado else 0)

    if cadena:
        log.info(
            "replication.pipeline",
            block_id=block_id,
            size_bytes=meta.size,
            nodes=1 + len(cadena),
            acked=acked,
            duration_ms=duracion_ms,
            ok=resultado.ok if resultado else True,
            error=resultado.error if resultado else "",
        )

    # Antes del 201: cuando el cliente vea su bloque subido, el ControlNode ya lo sabra.
    try:
        control.notify_stored(block_id, data_node_id, meta.size, meta.checksum_sha256)
    except Exception as exc:
        # Los bytes estan en disco pero el metadato no lo refleja. Se deshace la escritura
        # para que el cliente pueda reintentar con el mismo block_id: dejarla convertiria
        # el bloque en inmutable e irreintentable, y el commit fallaria para siempre.
        storage.delete(block_id)
        log.error("block.notify_failed", block_id=block_id, error=type(exc).__name__)
        raise StorageError(
            "el bloque se escribio pero el ControlNode no pudo confirmarlo; reintenta",
            block_id=block_id,
        ) from exc

    # El bloque ya esta confirmado: entra en el proximo report incremental. Anotarlo
    # antes del notify_stored haria que un fallo en la notificacion, que deshace la
    # escritura, dejara anunciado un bloque que ya no existe.
    app.state.changes.block_added(block_id)

    return meta, acked


@blocks_router.get("/blocks/{block_id}")
def get_block(
    block_id: str,
    request: Request,
    x_dfsha_block_token: Annotated[str | None, Header()] = None,
) -> StreamingResponse:
    _autorizar(request, block_id, "read", x_dfsha_block_token)
    storage = request.app.state.storage
    load = request.app.state.load
    meta = storage.read_meta(block_id)

    def cuerpo():
        # El contador se mantiene durante todo el envio, no solo mientras se abre el
        # fichero: una descarga lenta ocupa el nodo hasta que termina.
        with load.read():
            yield from storage.read(block_id)

    return StreamingResponse(
        cuerpo(),
        media_type="application/octet-stream",
        headers={
            CHECKSUM_HEADER: meta.checksum_sha256,
            "Content-Length": str(meta.size),
        },
    )


@blocks_router.delete("/blocks/{block_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_block(
    block_id: str,
    request: Request,
    x_dfsha_block_token: Annotated[str | None, Header()] = None,
) -> Response:
    """Borra un bloque. Idempotente: borrar lo que ya no esta tambien es 204, para que el
    GC pueda reintentar una pasada a medias sin que falle entera.

    La idempotencia va DESPUES de la autorizacion: un bloque que no existe tambien exige
    token. Si no, el endpoint seria un oraculo —204 si nunca existio, 403 si existe— que
    permite enumerar que bloques tiene este nodo sin autorizacion ninguna.
    """
    _autorizar(request, block_id, "delete", x_dfsha_block_token)
    if request.app.state.storage.delete(block_id):
        # Solo se anuncia como quitado lo que estaba: si no, un reintento del GC haria
        # que el ControlNode marcase MISSING una replica que ya habia olvidado.
        request.app.state.changes.block_removed(block_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@health_router.get("/health")
def health(request: Request) -> HealthResponse:
    """Estado real del disco.

    En la Etapa 2 este endpoint se convierte en el heartbeat y la politica de colocacion
    depende de estos numeros, asi que se calculan recorriendo el disco, no con contadores
    en memoria.
    """
    stats = request.app.state.storage.stats()
    return HealthResponse(
        status="ok",
        used_bytes=stats.used_bytes,
        capacity_bytes=request.app.state.capacity_bytes,
        block_count=stats.block_count,
        disk_free_bytes=stats.disk_free_bytes,
        data_node_id=request.app.state.data_node_id,
        fault_domain=request.app.state.fault_domain,
        boot_id=request.app.state.boot_id,
    )
