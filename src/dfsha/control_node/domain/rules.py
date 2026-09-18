"""Reglas del namespace.

Funciones puras que deciden y lanzan. Los casos de uso hacen las consultas, traen el
estado y llaman aqui; los routers no invocan nada de este modulo. La razon de tenerlas
separadas es poder probar cada regla sin base de datos ni HTTP de por medio.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from dfsha.common.errors import (
    AlreadyExistsError,
    BlocksNotStoredError,
    DirectoryNotEmptyError,
    InvalidPathError,
    InvalidStateError,
    NotFoundError,
    ReservationExpiredError,
)

from .entities import File, FileState, utcnow
from .path import Path

__all__ = [
    "MoveKind",
    "MoveDecision",
    "ensure_directory_is_empty",
    "ensure_name_is_free",
    "ensure_no_live_reservation",
    "ensure_visible",
    "ensure_can_commit",
    "ensure_can_abort",
    "decide_move",
    "ensure_move_is_legal",
]


def ensure_directory_is_empty(path: Path, entry_count: int, recursive: bool) -> None:
    """`rmdir` falla si el directorio no esta vacio, salvo `--recursive`."""
    if entry_count > 0 and not recursive:
        raise DirectoryNotEmptyError(
            "el directorio no esta vacio; usa --recursive para borrarlo con su contenido",
            path=str(path),
            entries=entry_count,
        )


def ensure_name_is_free(path: Path, existing: File | None, now: datetime | None = None) -> None:
    """Rechaza el nombre solo si hay algo vivo ocupandolo.

    Una reserva vencida no lo ocupa. Quien llama debe marcarla DELETED antes de seguir:
    ver `holds_name` en `File`.
    """
    if existing is not None and existing.holds_name(now or utcnow()):
        raise AlreadyExistsError("ya existe un archivo en esa ruta", path=str(path))


def ensure_no_live_reservation(
    path: Path, existing: File | None, now: datetime | None = None
) -> None:
    """Lo que puede impedir un `create`, que no es lo mismo que un nombre ocupado.

    Un archivo COMMITTED no estorba: escribir sobre una ruta existente es copy-on-write,
    se suben bloques nuevos y el viejo pasa a DELETED en el commit. Lo que si se rechaza
    es una reserva WRITING todavia viva, porque hay otro cliente subiendo a ese nombre
    ahora mismo y el segundo commit dejaria sus bloques huerfanos sin que nadie se entere.

    Una reserva vencida no estorba: quien la pidio ya no esta.
    """
    if existing is None:
        return
    if existing.state is not FileState.WRITING:
        return
    if not existing.is_expired(now or utcnow()):
        raise AlreadyExistsError(
            "hay una escritura en curso sobre esa ruta",
            path=str(path),
            file_id=existing.id,
        )


def ensure_visible(path: Path, file: File | None, now: datetime | None = None) -> File:
    """Un archivo en WRITING no aparece en `ls` ni puede abrirse: para el resto del
    mundo, todavia no existe."""
    if file is None or not file.is_visible(now or utcnow()):
        raise NotFoundError("no existe el archivo", path=str(path))
    return file


def ensure_can_commit(
    file: File,
    pending_block_ids: list[str],
    now: datetime | None = None,
    quorum: int = 1,
) -> None:
    """Orden deliberado: primero el vencimiento, luego los bloques.

    Una reserva vencida es 410 aunque todos los bloques esten arriba, porque el nombre ya
    pudo ser tomado por otro cliente y el commit produciria dos archivos vivos en la misma
    ruta.

    `pending_block_ids` son los bloques que **no alcanzan el quorum** de escritura, no
    los que no tienen ninguna copia. Con W=2 y R=3, un bloque con una sola replica cuenta
    como pendiente y uno con dos no, aunque le falte la tercera: un archivo con 2 de 3
    copias no esta roto, todavia tolera perder un nodo, y la tercera la completa la
    re-replicacion. `quorum` solo entra en el mensaje de error, para que un 409 diga que
    se esperaba en vez de dejar al cliente adivinando.
    """
    now = now or utcnow()

    if file.state is FileState.COMMITTED:
        raise InvalidStateError("el archivo ya fue confirmado", file_id=file.id)
    if file.state is FileState.DELETED:
        raise InvalidStateError("la reserva fue abortada o borrada", file_id=file.id)
    if file.is_expired(now):
        raise ReservationExpiredError(
            "la reserva de escritura vencio; vuelve a crear el archivo",
            file_id=file.id,
            expires_at=file.expires_at.isoformat() if file.expires_at else None,
        )
    if pending_block_ids:
        raise BlocksNotStoredError(
            f"hay bloques que no alcanzan el quorum de escritura (W={quorum})",
            file_id=file.id,
            missing=pending_block_ids[:20],
            missing_count=len(pending_block_ids),
            quorum=quorum,
        )


def ensure_can_abort(file: File) -> None:
    """Abortar es idempotente sobre una reserva ya muerta, pero nunca borra un archivo
    confirmado: para eso esta `rm`."""
    if file.state is FileState.COMMITTED:
        raise InvalidStateError(
            "el archivo ya fue confirmado; usa rm para borrarlo", file_id=file.id
        )


# --- mv --------------------------------------------------------------------


class MoveKind(StrEnum):
    """Que hay en el destino de un `mv`."""

    MISSING = "MISSING"
    DIRECTORY = "DIRECTORY"
    FILE = "FILE"


@dataclass(frozen=True, slots=True)
class MoveDecision:
    """A donde acaba la entrada movida: directorio destino y nombre final."""

    parent: Path
    name: str

    @property
    def target(self) -> Path:
        return self.parent.child(self.name)


def decide_move(src: Path, dst: Path, dst_kind: MoveKind) -> MoveDecision:
    """Resuelve la semantica de `mv`, que depende de que haya en el destino.

    - destino es un directorio existente: se mueve dentro, conservando el nombre.
    - destino no existe: se renombra (su padre debe existir, nunca se crea implicitamente).
    - destino es un archivo existente: 409. Sobrescribir en silencio pierde datos, y el
      modelo WORM no tiene manera de recuperarlos.
    """
    if src.is_root:
        raise InvalidPathError("no se puede mover la raiz")
    if dst.is_root:
        raise AlreadyExistsError("la raiz ya existe", path=str(dst))

    if dst_kind is MoveKind.DIRECTORY:
        return MoveDecision(parent=dst, name=src.name)
    if dst_kind is MoveKind.FILE:
        raise AlreadyExistsError(
            "ya existe un archivo en el destino; borralo primero", path=str(dst)
        )
    return MoveDecision(parent=dst.parent, name=dst.name)


def ensure_move_is_legal(src: Path, decision: MoveDecision, src_is_directory: bool) -> None:
    """Rechaza los movimientos que dejarian el arbol inconsistente."""
    target = decision.target

    if target == src:
        raise AlreadyExistsError("el origen y el destino son la misma ruta", path=str(src))

    if src_is_directory and src.is_ancestor_of(target):
        raise InvalidPathError(
            "no se puede mover un directorio dentro de si mismo",
            src=str(src),
            dst=str(target),
        )
