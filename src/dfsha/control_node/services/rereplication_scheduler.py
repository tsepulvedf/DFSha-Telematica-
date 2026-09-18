"""Bucle que detecta huecos de replicacion y despacha las copias.

**La tercera y ultima tarea en background del ControlNode.** La Etapa 2 fijo que solo
hubiera dos (el heartbeat del DataNode y el evaluador de pertenencia) y que anadir una
tuviera que justificarse. Esta se justifica porque no hay ningun otro momento natural en
el que mirar: un hueco de replicacion no lo provoca ninguna peticion, lo provoca que algo
**deje** de pasar, y nadie va a preguntar por el.

Va aparte del evaluador de pertenencia, aunque los dos exijan liderazgo y corran en
bucle, por dos razones concretas:

1. **Cadencias muy distintas.** El evaluador mira cada segundo porque detectar una caida
   rapido es su trabajo. Aqui la espera de gracia es de cinco minutos: escanear cada
   segundo seria recorrer el metadato entero 300 veces para no hacer nada.
2. **Fallan distinto.** Una excepcion escaneando la replicacion no debe impedir que se
   siga detectando que un nodo se cayo, que es informacion mas urgente.

Misma disciplina que los otros dos bucles, y por el mismo motivo: **no puede tumbar el
ControlNode**. Captura `Exception`, registra y sigue. Un `StaleEpochError` no cuenta como
fallo: es el mecanismo del Bloque A funcionando.
"""

from __future__ import annotations

import threading
from typing import Callable

from dfsha.common.errors import NotLeaderError, StaleEpochError
from dfsha.common.logging import get_logger
from dfsha.control_node.commands.rereplication import (
    RereplicationPolicy,
    dispatch,
    scan_and_enqueue,
)
from dfsha.control_node.domain.leadership import Fencing
from dfsha.control_node.domain.membership import MembershipThresholds
from dfsha.control_node.repositories.sql import SqlUnitOfWork

__all__ = ["RereplicationScheduler"]

UowFactory = Callable[[], SqlUnitOfWork]
FencingProvider = Callable[[], "Fencing | None"]


class RereplicationScheduler:
    def __init__(
        self,
        uow_factory: UowFactory,
        policy: RereplicationPolicy,
        thresholds: MembershipThresholds,
        interval_seconds: float,
        fencing_provider: FencingProvider | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("el intervalo del planificador debe ser positivo")

        self._uow = uow_factory
        self._policy = policy
        self._thresholds = thresholds
        self._interval = interval_seconds
        self._fencing = fencing_provider

        self._parar = threading.Event()
        self._hilo: threading.Thread | None = None
        self._log = get_logger("control_node")
        self.passes = 0
        self.failures = 0
        #: Pasadas que no se hicieron por no ser lider. No son fallos: con tres
        #: instancias es el estado normal de dos de ellas.
        self.skipped = 0
        self.enqueued = 0
        self.dispatched = 0

    def run_once(self) -> tuple[int, int]:
        """Una pasada: escanear y despachar. Devuelve (encolados, despachados).

        Aqui si se propaga la excepcion: quien llama desde una prueba quiere verla. El
        que la silencia es el bucle.
        """
        fencing = None
        if self._fencing is not None:
            fencing = self._fencing()
            if fencing is None:
                self.skipped += 1
                return (0, 0)
        else:
            # Sin proveedor no hay liderazgo que exigir. Lo usan las pruebas que
            # ejercitan el planificador sin montar un lease.
            fencing = Fencing(leader_id="", epoch=0)

        encolados = scan_and_enqueue(
            self._uow(), fencing, self._policy, self._thresholds
        )
        asignaciones = dispatch(self._uow(), fencing, self._policy, self._thresholds)

        self.enqueued += encolados
        self.dispatched += len(asignaciones)
        return (encolados, len(asignaciones))

    def tick(self) -> None:
        """Una pasada con su red de seguridad. Es lo que corre el hilo."""
        try:
            self.run_once()
            self.passes += 1
        except (StaleEpochError, NotLeaderError):
            # No es un fallo: es el Bloque A funcionando. Esta instancia creia ser lider,
            # la transaccion le dijo que no, y no escribio nada. `require_leadership` ya
            # emitio leadership.epoch_rejected con el detalle.
            self.skipped += 1
        except Exception as exc:
            # Deliberadamente `Exception`. Que la re-replicacion se pare unos segundos
            # cuesta que una copia tarde mas; que el proceso muera cuesta un ControlNode
            # entero, y encima justo cuando el cluster ya esta recuperandose de algo.
            self.failures += 1
            self._log.error(
                "rereplication.pass_failed",
                error=type(exc).__name__,
                error_detail=str(exc),
                consecutive_failures=self.failures,
            )

    def _bucle(self) -> None:
        while not self._parar.is_set():
            self.tick()
            # `wait` en vez de `sleep`: al pedir la parada se despierta al momento.
            self._parar.wait(self._interval)

    def start(self) -> None:
        if self._hilo is not None:
            return
        self._parar.clear()
        self._hilo = threading.Thread(
            target=self._bucle, name="dfsha-rereplication", daemon=True
        )
        self._hilo.start()
        self._log.info(
            "rereplication.scheduler_started",
            interval_seconds=self._interval,
            grace_seconds=self._policy.grace.total_seconds(),
            max_per_node=self._policy.max_per_node,
            replication_factor=self._policy.replication_factor,
        )

    def stop(self, timeout: float = 5.0) -> None:
        if self._hilo is None:
            return
        self._parar.set()
        self._hilo.join(timeout=timeout)
        self._hilo = None
        self._log.info(
            "rereplication.scheduler_stopped",
            passes=self.passes,
            failures=self.failures,
            skipped=self.skipped,
            enqueued=self.enqueued,
            dispatched=self.dispatched,
        )
