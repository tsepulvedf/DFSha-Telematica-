"""Jerarquia de errores de dominio.

Deliberadamente sin ningun codigo HTTP: el dominio no sabe que existe HTTP. La traduccion
a status codes vive en la capa `api/`, que es la que habla ese protocolo. Cuando la
Etapa 3 mueva el trafico interno a gRPC, se anade otra tabla de traduccion y el dominio no
se toca.

Cada error lleva un `code` estable, legible por maquina, que si viaja al cliente en el
cuerpo de la respuesta.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "DFShaError",
    "InvalidPathError",
    "NotFoundError",
    "AlreadyExistsError",
    "DirectoryNotEmptyError",
    "NotADirectoryError_",
    "IsADirectoryError_",
    "AuthenticationError",
    "AuthorizationError",
    "ReservationExpiredError",
    "BlocksNotStoredError",
    "InvalidStateError",
    "BlockAlreadyExistsError",
    "BlockNotFoundError",
    "ChecksumMismatchError",
    "NoDataNodeAvailableError",
    "StorageError",
    "NotLeaderError",
    "StaleEpochError",
]


class DFShaError(Exception):
    """Raiz de todos los errores propios de DFSha."""

    code = "dfsha_error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


# --- Namespace -------------------------------------------------------------


class InvalidPathError(DFShaError):
    """La ruta no pasa la normalizacion: `..`, nombre vacio, caracteres de control..."""

    code = "invalid_path"


class NotFoundError(DFShaError):
    """No existe el directorio o archivo pedido."""

    code = "not_found"


class AlreadyExistsError(DFShaError):
    """Ya hay una entrada viva con ese nombre en el directorio."""

    code = "already_exists"


class DirectoryNotEmptyError(DFShaError):
    """`rmdir` sobre un directorio con contenido y sin `--recursive`."""

    code = "directory_not_empty"


class NotADirectoryError_(DFShaError):
    """Se esperaba un directorio y la ruta apunta a un archivo."""

    code = "not_a_directory"


class IsADirectoryError_(DFShaError):
    """Se esperaba un archivo y la ruta apunta a un directorio."""

    code = "is_a_directory"


# --- Identidad -------------------------------------------------------------


class AuthenticationError(DFShaError):
    """Credenciales invalidas, token ausente, vencido o mal firmado."""

    code = "authentication_failed"


class AuthorizationError(DFShaError):
    """El principal esta autenticado pero el recurso no es suyo.

    Ojo con como la usa la capa `api`: revelar "existe pero no es tuyo" filtra la
    estructura del arbol de otro usuario. Para recursos de otro dueno se responde como si
    no existieran.
    """

    code = "not_authorized"


# --- Escritura de archivos -------------------------------------------------


class ReservationExpiredError(DFShaError):
    """La reserva de escritura vencio: su `expires_at` quedo en el pasado."""

    code = "reservation_expired"


class BlocksNotStoredError(DFShaError):
    """Se pidio commit y algun bloque del plan no esta en estado STORED."""

    code = "blocks_not_stored"


class InvalidStateError(DFShaError):
    """Transicion de estado ilegal, p. ej. commit sobre un archivo ya COMMITTED."""

    code = "invalid_state"


# --- Bloques y almacenamiento ---------------------------------------------


class BlockAlreadyExistsError(DFShaError):
    """Reescribir un `block_id` existente. Los bloques son inmutables (WORM)."""

    code = "block_already_exists"


class BlockNotFoundError(DFShaError):
    code = "block_not_found"


class ChecksumMismatchError(DFShaError):
    """Los bytes recibidos no corresponden al SHA-256 anunciado."""

    code = "checksum_mismatch"


class NoDataNodeAvailableError(DFShaError):
    """Ningun DataNode vivo con espacio para colocar el bloque."""

    code = "no_datanode_available"


# --- Liderazgo (Etapa 3) ---------------------------------------------------


class NotLeaderError(DFShaError):
    """Esta instancia no sostiene el lease y la operacion lo exige.

    No es un fallo: con tres ControlNodes, dos de ellos no son lideres en todo momento.
    """

    code = "not_leader"


class StaleEpochError(DFShaError):
    """La epoca con la que se pidio la operacion ya no es la vigente.

    Este es el error que atrapa al lider congelado: una pausa larga del recolector de
    basura o una particion de red dejan a una instancia creyendo que sigue mandando
    mientras otra ya tomo el lease. Se comprueba DENTRO de la misma transaccion que la
    operacion, porque comprobarlo antes deja una ventana en la que el lease puede
    cambiar entre la comprobacion y la escritura.
    """

    code = "stale_epoch"


class FileLockedError(DFShaError):
    """Otro cliente sostiene un lock incompatible sobre el archivo.

    Lleva `holder` y `retry_after_seconds` para que el cliente pueda decidir si esperar:
    «el archivo esta bloqueado» sin decir por quien ni hasta cuando es un mensaje con el
    que no se puede hacer nada.
    """

    code = "file_locked"


class StaleLockError(DFShaError):
    """El lock con el que se pidio la operacion ya no es el vigente.

    El equivalente del RF3 a `StaleEpochError`, y atrapa al mismo tipo de fallo: un
    cliente que se congelo con el lock tomado, vio vencer su lease sin enterarse, y
    despierta escribiendo encima de quien lo tomo despues. Se comprueba DENTRO de la
    misma transaccion que la escritura, por el mismo motivo.
    """

    code = "stale_lock"


class StorageError(DFShaError):
    """Fallo de E/S en el DataNode."""

    code = "storage_error"
