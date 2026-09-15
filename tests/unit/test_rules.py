"""Reglas de dominio: rmdir no vacio, nombre duplicado, WRITING invisible, reserva
vencida que libera el nombre, y la semantica de mv."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from dfsha.common.errors import (
    AlreadyExistsError,
    BlocksNotStoredError,
    DirectoryNotEmptyError,
    InvalidPathError,
    InvalidStateError,
    NotFoundError,
    ReservationExpiredError,
)
from dfsha.control_node.domain.entities import File, FileState
from dfsha.control_node.domain.path import Path
from dfsha.control_node.domain.rules import (
    MoveKind,
    decide_move,
    ensure_can_abort,
    ensure_can_commit,
    ensure_directory_is_empty,
    ensure_move_is_legal,
    ensure_name_is_free,
    ensure_visible,
)

AHORA = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)
MB = 1024 * 1024


def hacer_archivo(
    state: FileState = FileState.COMMITTED,
    *,
    expires_at: datetime | None = None,
) -> File:
    return File(
        id="f1",
        directory_id="d1",
        name="datos.bin",
        owner_id="u1",
        size=10,
        block_size=MB,
        state=state,
        created_at=AHORA - timedelta(minutes=5),
        committed_at=AHORA if state is FileState.COMMITTED else None,
        expires_at=expires_at,
    )


class TestRmdir:
    def test_directorio_vacio_se_borra(self) -> None:
        ensure_directory_is_empty(Path.parse("/a"), entry_count=0, recursive=False)

    def test_directorio_con_contenido_falla(self) -> None:
        with pytest.raises(DirectoryNotEmptyError):
            ensure_directory_is_empty(Path.parse("/a"), entry_count=3, recursive=False)

    def test_recursive_permite_borrar_con_contenido(self) -> None:
        ensure_directory_is_empty(Path.parse("/a"), entry_count=3, recursive=True)


class TestNombreOcupado:
    def test_nombre_libre(self) -> None:
        ensure_name_is_free(Path.parse("/a/x"), None, now=AHORA)

    def test_archivo_confirmado_ocupa_el_nombre(self) -> None:
        with pytest.raises(AlreadyExistsError):
            ensure_name_is_free(Path.parse("/a/x"), hacer_archivo(), now=AHORA)

    def test_reserva_viva_ocupa_el_nombre(self) -> None:
        viva = hacer_archivo(FileState.WRITING, expires_at=AHORA + timedelta(minutes=10))
        with pytest.raises(AlreadyExistsError):
            ensure_name_is_free(Path.parse("/a/x"), viva, now=AHORA)

    def test_reserva_vencida_libera_el_nombre(self) -> None:
        vencida = hacer_archivo(FileState.WRITING, expires_at=AHORA - timedelta(seconds=1))
        ensure_name_is_free(Path.parse("/a/x"), vencida, now=AHORA)

    def test_archivo_borrado_libera_el_nombre(self) -> None:
        ensure_name_is_free(Path.parse("/a/x"), hacer_archivo(FileState.DELETED), now=AHORA)


class TestVisibilidad:
    def test_archivo_confirmado_es_visible(self) -> None:
        archivo = hacer_archivo()
        assert ensure_visible(Path.parse("/a/x"), archivo, now=AHORA) is archivo

    def test_archivo_en_writing_no_existe_para_el_resto(self) -> None:
        escribiendo = hacer_archivo(
            FileState.WRITING, expires_at=AHORA + timedelta(minutes=10)
        )
        with pytest.raises(NotFoundError):
            ensure_visible(Path.parse("/a/x"), escribiendo, now=AHORA)

    def test_archivo_borrado_no_es_visible(self) -> None:
        with pytest.raises(NotFoundError):
            ensure_visible(Path.parse("/a/x"), hacer_archivo(FileState.DELETED), now=AHORA)

    def test_inexistente(self) -> None:
        with pytest.raises(NotFoundError):
            ensure_visible(Path.parse("/a/x"), None, now=AHORA)


class TestCommit:
    def test_commit_con_todo_subido(self) -> None:
        reserva = hacer_archivo(FileState.WRITING, expires_at=AHORA + timedelta(minutes=5))
        ensure_can_commit(reserva, pending_block_ids=[], now=AHORA)

    def test_faltan_bloques(self) -> None:
        reserva = hacer_archivo(FileState.WRITING, expires_at=AHORA + timedelta(minutes=5))
        with pytest.raises(BlocksNotStoredError):
            ensure_can_commit(reserva, pending_block_ids=["b2"], now=AHORA)

    def test_reserva_vencida(self) -> None:
        vencida = hacer_archivo(FileState.WRITING, expires_at=AHORA - timedelta(seconds=1))
        with pytest.raises(ReservationExpiredError):
            ensure_can_commit(vencida, pending_block_ids=[], now=AHORA)

    def test_el_vencimiento_manda_sobre_los_bloques_que_falten(self) -> None:
        # Una reserva vencida es 410 aunque ademas falten bloques: el nombre ya pudo
        # tomarlo otro cliente, asi que reintentar el commit nunca es lo correcto.
        vencida = hacer_archivo(FileState.WRITING, expires_at=AHORA - timedelta(seconds=1))
        with pytest.raises(ReservationExpiredError):
            ensure_can_commit(vencida, pending_block_ids=["b1"], now=AHORA)

    def test_commit_dos_veces(self) -> None:
        with pytest.raises(InvalidStateError):
            ensure_can_commit(hacer_archivo(), pending_block_ids=[], now=AHORA)

    def test_commit_de_una_reserva_abortada(self) -> None:
        with pytest.raises(InvalidStateError):
            ensure_can_commit(
                hacer_archivo(FileState.DELETED), pending_block_ids=[], now=AHORA
            )


class TestAbort:
    def test_abortar_una_reserva_viva(self) -> None:
        ensure_can_abort(hacer_archivo(FileState.WRITING, expires_at=AHORA))

    def test_abortar_es_idempotente_sobre_una_reserva_muerta(self) -> None:
        ensure_can_abort(hacer_archivo(FileState.DELETED))

    def test_abort_nunca_borra_un_archivo_confirmado(self) -> None:
        with pytest.raises(InvalidStateError):
            ensure_can_abort(hacer_archivo())


class TestMv:
    def test_destino_inexistente_renombra(self) -> None:
        decision = decide_move(Path.parse("/a/x"), Path.parse("/a/y"), MoveKind.MISSING)
        assert str(decision.target) == "/a/y"

    def test_destino_directorio_mueve_dentro(self) -> None:
        decision = decide_move(Path.parse("/a/x"), Path.parse("/b"), MoveKind.DIRECTORY)
        assert str(decision.target) == "/b/x"

    def test_destino_archivo_no_se_sobrescribe(self) -> None:
        with pytest.raises(AlreadyExistsError):
            decide_move(Path.parse("/a/x"), Path.parse("/b/y"), MoveKind.FILE)

    def test_no_se_mueve_la_raiz(self) -> None:
        with pytest.raises(InvalidPathError):
            decide_move(Path.root(), Path.parse("/a"), MoveKind.MISSING)

    def test_origen_igual_a_destino(self) -> None:
        src = Path.parse("/a/x")
        decision = decide_move(src, Path.parse("/a/x"), MoveKind.MISSING)
        with pytest.raises(AlreadyExistsError):
            ensure_move_is_legal(src, decision, src_is_directory=False)

    def test_directorio_dentro_de_si_mismo(self) -> None:
        src = Path.parse("/a")
        decision = decide_move(src, Path.parse("/a/b"), MoveKind.DIRECTORY)
        with pytest.raises(InvalidPathError):
            ensure_move_is_legal(src, decision, src_is_directory=True)

    def test_movimiento_legal(self) -> None:
        src = Path.parse("/a/x")
        decision = decide_move(src, Path.parse("/b"), MoveKind.DIRECTORY)
        ensure_move_is_legal(src, decision, src_is_directory=False)
