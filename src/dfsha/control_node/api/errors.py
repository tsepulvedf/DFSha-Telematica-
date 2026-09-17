"""Traduccion de errores de dominio a HTTP.

Esta tabla es el unico sitio del ControlNode donde un error de dominio se convierte en un
status code. El dominio no sabe que existe HTTP, asi que cuando la Etapa 3 mueva el plano
interno a gRPC bastara con anadir otra tabla al lado de esta.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from dfsha.common.errors import (
    AlreadyExistsError,
    AuthenticationError,
    AuthorizationError,
    BlockAlreadyExistsError,
    BlockNotFoundError,
    BlocksNotStoredError,
    ChecksumMismatchError,
    DFShaError,
    DirectoryNotEmptyError,
    FileLockedError,
    InvalidPathError,
    InvalidStateError,
    IsADirectoryError_,
    NoDataNodeAvailableError,
    NotADirectoryError_,
    NotFoundError,
    ReservationExpiredError,
    StaleLockError,
    StorageError,
)
from dfsha.common.logging import get_logger
from dfsha.control_node.domain.partition import InvalidBlockSizeError

__all__ = ["STATUS_BY_ERROR", "status_for", "install_error_handlers"]

STATUS_BY_ERROR: dict[type[DFShaError], int] = {
    InvalidPathError: status.HTTP_400_BAD_REQUEST,
    InvalidBlockSizeError: status.HTTP_400_BAD_REQUEST,
    NotADirectoryError_: status.HTTP_400_BAD_REQUEST,
    IsADirectoryError_: status.HTTP_400_BAD_REQUEST,
    AuthenticationError: status.HTTP_401_UNAUTHORIZED,
    # 404 y no 403: decir "existe pero no es tuyo" filtraria la forma del arbol de otro
    # usuario. Para quien no es el dueno, el recurso simplemente no existe.
    AuthorizationError: status.HTTP_404_NOT_FOUND,
    NotFoundError: status.HTTP_404_NOT_FOUND,
    BlockNotFoundError: status.HTTP_404_NOT_FOUND,
    AlreadyExistsError: status.HTTP_409_CONFLICT,
    DirectoryNotEmptyError: status.HTTP_409_CONFLICT,
    BlocksNotStoredError: status.HTTP_409_CONFLICT,
    InvalidStateError: status.HTTP_409_CONFLICT,
    BlockAlreadyExistsError: status.HTTP_409_CONFLICT,
    # 409 y no 423 (Locked): 423 es de WebDAV y muchos clientes HTTP no lo tratan como
    # reintentable. El cuerpo lleva `holder` y `retry_after_seconds`, que es la
    # informacion con la que de verdad se decide si esperar.
    FileLockedError: status.HTTP_409_CONFLICT,
    StaleLockError: status.HTTP_409_CONFLICT,
    ReservationExpiredError: status.HTTP_410_GONE,
    # 422 literal: starlette renombro la constante entre versiones y el nombre
    # viejo emite un DeprecationWarning.
    ChecksumMismatchError: 422,
    NoDataNodeAvailableError: status.HTTP_503_SERVICE_UNAVAILABLE,
    StorageError: status.HTTP_500_INTERNAL_SERVER_ERROR,
}


def status_for(error: DFShaError) -> int:
    """Busca por la clase exacta y luego por sus ancestros.

    Un error de dominio nuevo sin entrada en la tabla acaba en 500, que es lo correcto:
    significa que alguien lo lanza sin haber decidido como se ve desde fuera.
    """
    for clase in type(error).__mro__:
        if clase in STATUS_BY_ERROR:
            return STATUS_BY_ERROR[clase]
    return status.HTTP_500_INTERNAL_SERVER_ERROR


def install_error_handlers(app: FastAPI) -> None:
    log = get_logger("control_node")

    @app.exception_handler(DFShaError)
    async def _dfsha_error(request: Request, exc: DFShaError) -> JSONResponse:
        codigo = status_for(exc)
        if codigo >= 500:
            log.error(
                "api.error",
                path=request.url.path,
                status=codigo,
                code=exc.code,
                message=exc.message,
            )
        return JSONResponse(status_code=codigo, content=exc.to_dict())
