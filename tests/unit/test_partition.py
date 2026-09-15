"""Particion en bloques: numero de bloques y tamano del ultimo, con sus bordes."""

from __future__ import annotations

import pytest

from dfsha.control_node.domain.partition import (
    InvalidBlockSizeError,
    block_count_for,
    plan_blocks,
)

MB = 1024 * 1024


class TestConteo:
    @pytest.mark.parametrize(
        ("size", "block_size", "esperado"),
        [
            (0, MB, 0),            # archivo vacio: cero bloques, no un bloque vacio
            (1, MB, 1),
            (MB - 1, MB, 1),       # menor que un bloque
            (MB, MB, 1),           # multiplo exacto: no sobra un bloque de cero bytes
            (MB + 1, MB, 2),
            (50 * MB, MB, 50),     # el caso del round-trip de la seccion 12
            (3 * MB - 1, MB, 3),
        ],
    )
    def test_block_count_for(self, size: int, block_size: int, esperado: int) -> None:
        assert block_count_for(size, block_size) == esperado
        assert len(plan_blocks(size, block_size)) == esperado


class TestReparto:
    def test_archivo_vacio_no_tiene_bloques(self) -> None:
        assert plan_blocks(0, MB) == []

    def test_un_solo_byte(self) -> None:
        specs = plan_blocks(1, MB)
        assert len(specs) == 1
        assert (specs[0].index, specs[0].size, specs[0].offset) == (0, 1, 0)

    def test_menor_que_el_bloque(self) -> None:
        specs = plan_blocks(1234, MB)
        assert [s.size for s in specs] == [1234]

    def test_multiplo_exacto_todos_llenos(self) -> None:
        specs = plan_blocks(4 * MB, MB)
        assert [s.size for s in specs] == [MB] * 4
        assert [s.offset for s in specs] == [0, MB, 2 * MB, 3 * MB]

    def test_ultimo_bloque_lleva_el_resto(self) -> None:
        specs = plan_blocks(2 * MB + 7, MB)
        assert [s.size for s in specs] == [MB, MB, 7]

    def test_los_bloques_cubren_el_archivo_sin_huecos(self) -> None:
        size = 10 * MB + 12345
        specs = plan_blocks(size, MB)

        assert [s.index for s in specs] == list(range(len(specs)))
        assert specs[0].offset == 0
        assert specs[-1].end == size
        assert sum(s.size for s in specs) == size
        for anterior, siguiente in zip(specs, specs[1:]):
            assert anterior.end == siguiente.offset

    def test_bloque_mas_grande_que_el_archivo(self) -> None:
        specs = plan_blocks(10, 64 * MB)
        assert [s.size for s in specs] == [10]


class TestValidacion:
    @pytest.mark.parametrize("block_size", [0, -1])
    def test_block_size_no_positivo(self, block_size: int) -> None:
        with pytest.raises(InvalidBlockSizeError):
            plan_blocks(10, block_size)

    def test_size_negativo(self) -> None:
        with pytest.raises(InvalidBlockSizeError):
            plan_blocks(-1, MB)
