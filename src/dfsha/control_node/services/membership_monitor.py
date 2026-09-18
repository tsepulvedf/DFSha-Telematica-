"""Evaluador periodico de pertenencia.

El estado real de un nodo se **deriva** del ultimo heartbeat cada vez que alguien lo
consulta, asi que el sistema es correcto sin este bucle. Lo que el bucle aporta es que la
transicion quede registrada aunque nadie este mirando, y que las replicas de un nodo que
acaba de morir dejen de anunciarse sin esperar a la proxima consulta.

Tres propiedades, en orden de importancia:

1. **No puede tumbar el ControlNode.** Una excepcion dentro de una pasada se registra y
   el bucle sigue. Perder una evaluacion cuesta unos segundos de retraso en un evento;
   perder el proceso entero cuesta el cluster.
2. **Su intervalo es configurable**, no una constante escondida en el codigo.
3. Termina limpio cuando se le pide: el apagado del ControlNode no se queda esperando.
"""

from __future__ import annotations

import threading
from typing import Callable

from dfsha.common.errors import StaleEpochError
from dfsha.common.logging import get_logger
from dfsha.control_node.commands.control_plane import evaluate_membership
from dfsha.control_node.domain.leadership import Fencing
from dfsha.control_node.domain.membership import MembershipThresholds
from dfsha.control_node.repositories.sql import SqlUnitOfWork

__all__ = ["MembershipMonitor"]

UowFactory = Callable[[], SqlUnitOfWork]
#: De donde sale la epoca de esta instancia. Devuelve None cuando no es lider, que con
#: tres ControlNodes es el estado normal de dos de ellos.
FencingProvider = Callable[[], "Fencing | None"]


class MembershipMonitor:
    def __init__(
        self,
        uow_factory: UowFactory,
        thresholds: MembershipThresholds,
        interval_seconds: float,
        fencing_provider: FencingProvider | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("el intervalo del evaluador debe ser positivo")

        self._uow = uow_factory
        self._thresholds = thresholds
        self._interval = interval_seconds
        #: Sin proveedor no se exige liderazgo, que es el comportamiento de la Etapa 2.
        #: Lo usan las pruebas de la maquina de estados, que no tienen nada que ver con
        #: el lease.
        self._fencing = fencing_provider
        self._parar = threading.Event()
        self._hilo: threading.Thread | None = None
        self._log = get_logger("control_node")
        #: Contadores para poder comprobar en las pruebas que el bucle sobrevive a un
        #: fallo, sin depender de leer los logs.
        self.passes = 0
        self.failures = 0
        #: Pasadas que no se hicieron por no ser lider. No son fallos.
        self.skipped = 0

    def run_once(self) -> int:
        """Una pasada. Devuelve cuantas transiciones hubo.

        Aqui si se propaga la excepcion: quien llama desde una prueba quiere verla. El
        que la silencia es el bucle.
        """
        fencing = None
        if self._fencing is not None:
            fencing = self._fencing()
            if fencing is None:
                # No somos lider. No hay nada que hacer y no ha fallado nada.
                self.skipped += 1
                return 0

        cambios = evaluate_membership(self._uow(), self._thresholds, fencing=fencing)
        return len(cambios)

    def _bucle(self) -> None:
        while not self._parar.is_set():
            try:
                self.run_once()
                self.passes += 1
            except StaleEpochError:
                # No es un fallo: es el mecanismo funcionando. Esta instancia creia ser
                # lider, la transaccion le dijo que no, y la operacion se aborto entera
                # sin escribir nada. `require_leadership` ya emitio
                # leadership.epoch_rejected con el detalle.
                self.skipped += 1
            except Exception as exc:
                # Deliberadamente `Exception` y no un tipo concreto: la razon de ser de
                # este try es que NADA que pase dentro pueda matar el hilo. Un fallo
                # transitorio de la base de datos no puede dejar al cluster sin
                # deteccion de caidas para siempre.
                self.failures += 1
                self._log.error(
                    "membership.evaluation_failed",
                    error=type(exc).__name__,
                    error_detail=str(exc),
                    consecutive_failures=self.failures,
                )
            # `wait` en vez de `sleep`: al pedir la parada se despierta al momento en
            # vez de esperar a que venza el intervalo.
            self._parar.wait(self._interval)

    def start(self) -> None:
        if self._hilo is not None:
            return
        self._parar.clear()
        self._hilo = threading.Thread(
            target=self._bucle, name="dfsha-membership", daemon=True
        )
        self._hilo.start()
        self._log.info("membership.monitor_started", interval_seconds=self._interval)

    def stop(self, timeout: float = 5.0) -> None:
        if self._hilo is None:
            return
        self._parar.set()
        self._hilo.join(timeout=timeout)
        self._hilo = None
        self._log.info(
            "membership.monitor_stopped",
            passes=self.passes,
            failures=self.failures,
            skipped=self.skipped,
        )
