"""Cliente REST del DataNode hacia el plano interno del ControlNode.

Desde la Etapa 2 aqui queda **una sola** llamada: avisar de que un bloque quedo
almacenado. El registro, el heartbeat y el block report se fueron a gRPC (ver
`heartbeat.py`).

Que esta se quede en REST es deliberado y esta razonado en CLAUDE.md: el DataNode la
hace de forma sincrona antes de responder 201 al cliente, de modo que cuando el cliente
ve su bloque subido, el ControlNode ya lo sabe. Si viajara en el report incremental del
heartbeat, un `commit` inmediato podria adelantarse hasta 3 s a la noticia y fallar con
409 por una carrera. La latencia del commit no puede quedar atada al periodo del latido.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from dfsha.common.logging import get_logger

__all__ = ["ControlClient", "NodeIdentity", "Identity"]

INTERNAL_SECRET_HEADER = "X-DFSha-Internal-Secret"
IDENTITY_FILE = "node.json"


@dataclass(frozen=True, slots=True)
class Identity:
    """Quien es este nodo y con que disco arranco."""

    data_node_id: str
    boot_id: str


class NodeIdentity:
    """Guarda `data_node_id` y `boot_id` en disco, junto a los bloques.

    Que el fichero viva en el MISMO volumen que los bloques no es casualidad, es el
    mecanismo:

    - Reiniciar el contenedor conserva el fichero, asi que el nodo vuelve con el mismo
      `data_node_id` (sus replicas siguen siendo localizables) y el mismo `boot_id` (el
      ControlNode sabe que su disco sigue intacto).
    - Perder el volumen pierde el fichero, asi que el nodo arranca con un `boot_id`
      nuevo. El ControlNode lo detecta y da sus replicas por perdidas, que es la verdad:
      los bytes se fueron con el volumen.

    Un `boot_id` guardado fuera del volumen de datos mentiria en ese segundo caso.
    """

    def __init__(self, data_dir: str | Path) -> None:
        self.path = Path(data_dir) / IDENTITY_FILE

    def read(self) -> Identity | None:
        if not self.path.exists():
            return None
        try:
            datos = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

        boot_id = datos.get("boot_id")
        if not boot_id:
            return None  # fichero de la Etapa 1: se trata como arranque nuevo
        return Identity(data_node_id=datos.get("data_node_id", ""), boot_id=boot_id)

    def load_or_create(self) -> Identity:
        """Identidad de este arranque.

        Sin fichero, `boot_id` nuevo: es un disco vacio, aunque el ControlNode pueda
        reconocer al nodo por su URL anunciada.
        """
        existente = self.read()
        if existente is not None:
            return existente
        return Identity(data_node_id="", boot_id=str(uuid.uuid4()))

    def write(self, identity: Identity) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporal = self.path.with_suffix(".json.tmp")
        temporal.write_text(
            json.dumps(
                {"data_node_id": identity.data_node_id, "boot_id": identity.boot_id}
            ),
            encoding="utf-8",
        )
        temporal.replace(self.path)


class ControlClient:
    def __init__(self, control_url: str, internal_secret: str, timeout: float = 10.0) -> None:
        self.control_url = control_url.rstrip("/")
        self._headers = {INTERNAL_SECRET_HEADER: internal_secret}
        self._timeout = timeout
        self._log = get_logger("data_node")

    def notify_stored(
        self, block_id: str, data_node_id: str, size: int, checksum_sha256: str
    ) -> None:
        """Avisa de que el bloque esta en disco.

        Se llama ANTES de responder 201 al cliente. Asi, cuando el cliente ve su bloque
        subido, el ControlNode ya lo sabe: sin ese orden, un commit inmediato podria
        encontrar el bloque todavia en PENDING y fallar con 409 por una carrera.
        """
        respuesta = httpx.post(
            f"{self.control_url}/internal/v1/blocks/{block_id}/stored",
            json={
                "data_node_id": data_node_id,
                "size": size,
                "checksum_sha256": checksum_sha256,
            },
            headers=self._headers,
            timeout=self._timeout,
        )
        respuesta.raise_for_status()
