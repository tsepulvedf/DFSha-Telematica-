"""Divergencia entre lo que dice el metadato y lo que hay en el disco de un DataNode.

El ControlNode **nunca borra datos** por una divergencia. Solo la anota:

- Bloque reportado que el metadato no conoce -> huerfano, queda para el GC, que es quien
  borra y solo cuando alguien lo ejecuta a mano.
- Bloque esperado que el nodo no reporta -> la replica pasa a MISSING y se registra.

La asimetria es deliberada. Un bloque de mas cuesta disco; un bloque de menos cuesta
datos. Ante la duda, el sistema se queda con el bloque y avisa a un humano.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

__all__ = ["DivergenceReport", "compare"]


@dataclass(frozen=True, slots=True)
class DivergenceReport:
    unknown_block_ids: tuple[str, ...]
    missing_block_ids: tuple[str, ...]
    confirmed_block_ids: tuple[str, ...]

    @property
    def unknown_count(self) -> int:
        return len(self.unknown_block_ids)

    @property
    def missing_count(self) -> int:
        return len(self.missing_block_ids)

    @property
    def has_divergence(self) -> bool:
        return bool(self.unknown_block_ids or self.missing_block_ids)


def compare(
    expected: Iterable[str], reported: Iterable[str], is_full: bool
) -> DivergenceReport:
    """Compara lo esperado en ese nodo con lo que el nodo dice tener.

    `is_full` no es un detalle: en un report **incremental** el nodo solo manda lo que
    cambio desde el anterior, asi que la ausencia de un bloque no significa nada. Marcar
    MISSING con un incremental daria por perdido el disco entero en cada latido. Solo un
    report completo autoriza a concluir que falta algo.
    """
    esperados = set(expected)
    reportados = set(reported)

    desconocidos = tuple(sorted(reportados - esperados))
    confirmados = tuple(sorted(reportados & esperados))
    faltantes = tuple(sorted(esperados - reportados)) if is_full else ()

    return DivergenceReport(
        unknown_block_ids=desconocidos,
        missing_block_ids=faltantes,
        confirmed_block_ids=confirmados,
    )
