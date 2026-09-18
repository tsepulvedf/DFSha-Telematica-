"""Servicio que sostiene el lease de liderazgo.

Un hilo que renueva cada `DFSHA_LEASE_RENEW_MS`. Misma disciplina que el evaluador de
pertenencia de la Etapa 2, y por la misma razon: **no puede tumbar el ControlNode**. Una
excepcion dentro de una pasada se registra y el bucle sigue. Perder una renovacion cuesta
que el lease pase a otra instancia, que es una situacion prevista y ordenada; perder el
proceso cuesta un ControlNode.

## Lo que este objeto NO es

No es la autoridad sobre quien manda. `current_fencing()` devuelve la **ultima vista
local** del lease, que puede estar obsoleta precisamente cuando mas importa: si este
proceso lleva ocho segundos congelado, esta propiedad dira alegremente que sigue siendo
lider. Eso es deliberado y no hay forma de arreglarlo aqui: para saber la verdad hay que
mirar la base, y para que esa verdad no se quede vieja entre la mirada y la escritura hay
que mirarla dentro de la misma transaccion. De eso se encarga `require_leadership`.

La division es: este servicio **propone** una epoca; la transaccion de la operacion la
**verifica**.
"""

from __future__ import annotations

import threading
import uuid
from typing import Callable

from dfsha.common.logging import get_logger
from dfsha.control_node.commands.leadership import (
    acquire_or_renew,
    release_leadership,
)
from dfsha.control_node.domain.entities import utcnow
from dfsha.control_node.domain.leadership import Fencing, Lease, LeaseTimings
from dfsha.control_node.repositories.sql import SqlUnitOfWork

__all__ = ["LeadershipService"]

UowFactory = Callable[[], SqlUnitOfWork]


class LeadershipService:
    def __init__(
        self,
        uow_factory: UowFactory,
        timings: LeaseTimings,
        instance_id: str | None = None,
        clock=utcnow,
    ) -> None:
        self._uow = uow_factory
        self._timings = timings
        #: Identidad de ESTE proceso. Nueva en cada arranque a proposito: un ControlNode
        #: que se reinicia es, a efectos del lease, una instancia distinta, y no debe
        #: poder renovar el lease que dejo a medias antes de caerse.
        self.instance_id = instance_id or str(uuid.uuid4())
        self._clock = clock

        self._lease: Lease | None = None
        self._candado = threading.Lock()
        self._parar = threading.Event()
        self._hilo: threading.Thread | None = None
        self._log = get_logger("control_node")
        #: Contadores para que las pruebas comprueben que el bucle sobrevive a un fallo
        #: sin tener que leer los logs.
        self.passes = 0
        self.failures = 0

    # --- Vista local -------------------------------------------------------

    @property
    def is_leader(self) -> bool:
        """Ultima vista local. Vale para pintar un endpoint, NO para autorizar una
        escritura: ver la nota de la cabecera del modulo."""
        with self._candado:
            lease = self._lease
        return lease is not None and lease.held_by(self.instance_id, self._clock())

    def current_fencing(self) -> Fencing | None:
        """La epoca que esta instancia cree tener. La transaccion la verificara."""
        with self._candado:
            lease = self._lease
        if lease is None or lease.leader_id != self.instance_id:
            return None
        return lease.fencing

    # --- Bucle -------------------------------------------------------------

    def run_once(self) -> Lease | None:
        """Una pasada. Aqui si se propaga la excepcion: quien llama desde una prueba
        quiere verla. El que la silencia es el bucle."""
        antes = self.current_fencing()
        lease = acquire_or_renew(self._uow(), self.instance_id, self._timings)

        with self._candado:
            self._lease = lease

        if antes is not None and lease is None:
            # Se perdio el liderazgo sin haberlo soltado: alguien mas lo tomo mientras
            # esta instancia no renovaba a tiempo.
            self._log.warning(
                "leadership.lost",
                leader_id=self.instance_id,
                epoch=antes.epoch,
                duration_ms=0.0,
                detail="el lease vencio o lo tomo otra instancia",
            )
        return lease

    def tick(self) -> Lease | None:
        """Una pasada del bucle, con su red de seguridad.

        Separada de `_bucle` para que las pruebas puedan ejercitar exactamente lo que
        corre el hilo, incluido el manejo del fallo, sin arrancar ningun hilo.
        """
        try:
            return self.run_once()
        except Exception as exc:
            # Deliberadamente `Exception`: nada de lo que pase aqui puede matar el hilo.
            # Un fallo transitorio de la base cuesta, como mucho, que el lease pase a
            # otra instancia, que es exactamente lo que deberia pasar. Matar el hilo
            # dejaria a esta instancia sin poder volver a ser lider nunca.
            self.failures += 1
            with self._candado:
                self._lease = None
            self._log.error(
                "leadership.renew_failed",
                leader_id=self.instance_id,
                error=type(exc).__name__,
                error_detail=str(exc),
                consecutive_failures=self.failures,
            )
            return None

    def _bucle(self) -> None:
        while not self._parar.is_set():
            fallos_antes = self.failures
            self.tick()
            # Una pasada cuenta si no fallo, sea cual sea el resultado: no ser lider es
            # una pasada perfectamente correcta, y con tres instancias es lo normal.
            if self.failures == fallos_antes:
                self.passes += 1
            self._parar.wait(self._timings.renew_every.total_seconds())

    def start(self) -> None:
        if self._hilo is not None:
            return
        self._parar.clear()
        self._hilo = threading.Thread(
            target=self._bucle, name="dfsha-leadership", daemon=True
        )
        self._hilo.start()
        self._log.info(
            "leadership.monitor_started",
            leader_id=self.instance_id,
            ttl_s=self._timings.ttl.total_seconds(),
            renew_every_s=self._timings.renew_every.total_seconds(),
        )

    def stop(self, timeout: float = 5.0) -> None:
        if self._hilo is None:
            return
        self._parar.set()
        self._hilo.join(timeout=timeout)
        self._hilo = None

        # Soltarlo al apagar no hace falta para ser correcto (el lease vence solo), pero
        # evita que un reinicio ordenado deje al cluster sin lider un TTL entero.
        fencing = self.current_fencing()
        if fencing is not None:
            try:
                release_leadership(self._uow(), fencing)
                self._log.info(
                    "leadership.released", leader_id=self.instance_id, epoch=fencing.epoch
                )
            except Exception as exc:  # pragma: no cover - apagado con la base ya caida
                self._log.warning(
                    "leadership.release_failed", error=type(exc).__name__, error_detail=str(exc)
                )
        with self._candado:
            self._lease = None
