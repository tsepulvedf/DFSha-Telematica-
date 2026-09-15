"""Estado vivo del DataNode: lo que mide el heartbeat y no esta en el disco.

`used_bytes` y `block_count` salen de recorrer el disco, porque tienen que sobrevivir a
un reinicio. Estos otros no: cuantas escrituras y lecturas hay en curso ahora mismo, y
cuantos bytes se escribieron en el ultimo minuto, solo tienen sentido para el proceso que
esta corriendo. Son la senal de carga instantanea que la politica de colocacion usa para
desempatar entre nodos igual de llenos.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import Iterator

__all__ = ["LoadTracker"]

VENTANA_SEGUNDOS = 60.0


class LoadTracker:
    def __init__(self, ventana_segundos: float = VENTANA_SEGUNDOS) -> None:
        self._ventana = ventana_segundos
        self._lock = threading.Lock()
        self._writes = 0
        self._reads = 0
        self._escrituras: deque[tuple[float, int]] = deque()

    @property
    def writes_in_flight(self) -> int:
        with self._lock:
            return self._writes

    @property
    def reads_in_flight(self) -> int:
        with self._lock:
            return self._reads

    def bytes_written_60s(self, ahora: float | None = None) -> int:
        """Bytes escritos en la ventana. Descarta lo viejo al consultar, que es cuando
        hace falta: asi no hace falta un hilo limpiando la cola."""
        corte = (ahora or time.monotonic()) - self._ventana
        with self._lock:
            while self._escrituras and self._escrituras[0][0] < corte:
                self._escrituras.popleft()
            return sum(tamano for _, tamano in self._escrituras)

    @contextmanager
    def write(self) -> Iterator["_WriteScope"]:
        """Cuenta una escritura en curso. El tamano se registra al terminar bien.

        `try/finally` para que un bloque que falla a mitad no deje el contador subido
        para siempre: un `writes_in_flight` que solo crece sacaria al nodo de la
        colocacion de forma permanente.
        """
        scope = _WriteScope()
        with self._lock:
            self._writes += 1
        try:
            yield scope
        finally:
            with self._lock:
                self._writes -= 1
                if scope.size:
                    self._escrituras.append((time.monotonic(), scope.size))

    @contextmanager
    def read(self) -> Iterator[None]:
        with self._lock:
            self._reads += 1
        try:
            yield
        finally:
            with self._lock:
                self._reads -= 1


class _WriteScope:
    __slots__ = ("size",)

    def __init__(self) -> None:
        self.size = 0
