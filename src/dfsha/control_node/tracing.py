"""Trazas de la separacion CQRS.

`metadata.command` y `metadata.query` son eventos distintos a proposito: en los logs se
tiene que ver cuanto pesa cada lado, que es el dato que justifica replicar el lado de
lectura en la Etapa 3. Si ambos emitieran el mismo evento, habria que reconstruir la
separacion a posteriori mirando nombres de operacion.
"""

from __future__ import annotations

import functools
from typing import Any, Callable, TypeVar

from dfsha.common.logging import get_logger, timed

__all__ = ["command", "query"]

F = TypeVar("F", bound=Callable[..., Any])


def _trace(event: str, operation: str) -> Callable[[F], F]:
    def decorador(func: F) -> F:
        @functools.wraps(func)
        def envoltura(*args: Any, **kwargs: Any) -> Any:
            with timed(event, logger=get_logger("control_node"), operation=operation):
                return func(*args, **kwargs)

        return envoltura  # type: ignore[return-value]

    return decorador


def command(operation: str) -> Callable[[F], F]:
    """Marca un caso de uso que muta el namespace."""
    return _trace("metadata.command", operation)


def query(operation: str) -> Callable[[F], F]:
    """Marca un caso de uso de solo lectura."""
    return _trace("metadata.query", operation)
