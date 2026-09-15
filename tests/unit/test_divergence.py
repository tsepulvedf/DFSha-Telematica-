"""Comparacion entre el metadato y lo que un DataNode dice tener en disco."""

from __future__ import annotations

from dfsha.control_node.domain.divergence import compare


class TestReportCompleto:
    def test_todo_cuadra(self) -> None:
        r = compare(expected=["a", "b"], reported=["a", "b"], is_full=True)
        assert r.has_divergence is False
        assert r.confirmed_block_ids == ("a", "b")

    def test_bloque_de_mas_es_huerfano(self) -> None:
        r = compare(expected=["a"], reported=["a", "zombi"], is_full=True)
        assert r.unknown_block_ids == ("zombi",)
        assert r.missing_block_ids == ()

    def test_bloque_de_menos_falta(self) -> None:
        r = compare(expected=["a", "perdido"], reported=["a"], is_full=True)
        assert r.missing_block_ids == ("perdido",)
        assert r.unknown_block_ids == ()

    def test_las_dos_divergencias_a_la_vez(self) -> None:
        r = compare(expected=["a", "perdido"], reported=["a", "zombi"], is_full=True)
        assert (r.unknown_count, r.missing_count) == (1, 1)
        assert r.confirmed_block_ids == ("a",)

    def test_un_nodo_que_perdio_el_disco(self) -> None:
        r = compare(expected=["a", "b", "c"], reported=[], is_full=True)
        assert r.missing_block_ids == ("a", "b", "c")


class TestReportIncremental:
    def test_un_incremental_nunca_marca_nada_como_perdido(self) -> None:
        # El nodo solo manda lo que cambio; la ausencia de un bloque no significa nada.
        # Si esto marcara MISSING, cada latido daria por perdido el disco entero.
        r = compare(expected=["a", "b", "c"], reported=["a"], is_full=False)
        assert r.missing_block_ids == ()
        assert r.confirmed_block_ids == ("a",)

    def test_un_incremental_si_detecta_huerfanos(self) -> None:
        # Un bloque que el nodo anuncia y el metadato no conoce es igual de sospechoso
        # venga en un incremental o en un completo.
        r = compare(expected=["a"], reported=["zombi"], is_full=False)
        assert r.unknown_block_ids == ("zombi",)


class TestBordes:
    def test_nada_por_ninguna_parte(self) -> None:
        r = compare(expected=[], reported=[], is_full=True)
        assert r.has_divergence is False

    def test_repetidos_no_cuentan_dos_veces(self) -> None:
        r = compare(expected=["a"], reported=["a", "a"], is_full=True)
        assert r.confirmed_block_ids == ("a",)
        assert r.has_divergence is False

    def test_el_resultado_es_determinista(self) -> None:
        # Ordenado, para que los logs y las respuestas no bailen entre ejecuciones.
        r = compare(expected=["z", "y"], reported=["b", "a"], is_full=True)
        assert r.unknown_block_ids == ("a", "b")
        assert r.missing_block_ids == ("y", "z")
