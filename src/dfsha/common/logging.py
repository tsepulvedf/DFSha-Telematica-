"""Logging estructurado en JSON.

Una linea JSON por evento a stdout, con `timestamp`, `level`, `service`, `event` y los
campos propios de cada evento. No es decorativo: estos son los datos de los benchmarks de
la Etapa 4, y por eso se instrumenta desde el primer commit en vez de despues.

Nada de Prometheus ni OpenTelemetry. Logs JSON y nada mas.
"""

from __future__ import annotations

import logging
import sys
import time
from contextlib import contextmanager
from typing import Any, Iterator

import structlog

__all__ = ["configure_logging", "get_logger", "timed", "Timer"]

_configured = False


def configure_logging(service: str, level: str = "INFO") -> None:
    """Configura structlog para emitir JSON por linea a stdout.

    `service` queda ligado a todos los eventos del proceso ("control-node",
    "data-node", "client", "gc"), que es lo que permite separar las trazas cuando se
    recolectan los logs de todos los contenedores juntos.
    """
    global _configured

    numeric_level = getattr(logging, level.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=numeric_level,
        force=True,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(service=service)
    _configured = True


def get_logger(name: str | None = None) -> Any:
    """Devuelve un logger structlog. Si nadie configuro aun, aplica un default seguro."""
    if not _configured:
        configure_logging(service="dfsha", level="INFO")
    return structlog.get_logger(name) if name else structlog.get_logger()


class Timer:
    """Campos acumulados de un evento cronometrado.

    Se expone dentro del `with timed(...) as t` para que el cuerpo pueda anadir campos que
    solo se conocen a mitad de la operacion (por ejemplo `block_count` o `file_id`).
    """

    def __init__(self, **fields: Any) -> None:
        self.fields: dict[str, Any] = dict(fields)
        self.start = time.perf_counter()

    def bind(self, **fields: Any) -> "Timer":
        self.fields.update(fields)
        return self

    @property
    def duration_ms(self) -> float:
        return round((time.perf_counter() - self.start) * 1000, 3)


@contextmanager
def timed(event: str, logger: Any | None = None, **fields: Any) -> Iterator[Timer]:
    """Cronometra un bloque y emite `event` con `duration_ms` y `ok`.

    Emite siempre, tambien si el bloque lanza: un fallo con su duracion es justamente lo
    que hace falta para diagnosticar. La excepcion se vuelve a propagar.
    """
    log = logger or get_logger()
    timer = Timer(**fields)
    try:
        yield timer
    except Exception as exc:
        log.error(
            event,
            duration_ms=timer.duration_ms,
            ok=False,
            error=type(exc).__name__,
            error_detail=str(exc),
            **timer.fields,
        )
        raise
    else:
        log.info(event, duration_ms=timer.duration_ms, ok=True, **timer.fields)
