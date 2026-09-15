"""Cliente del DataNode hacia el plano interno del ControlNode.

Dos llamadas: registrarse al arrancar y avisar de cada bloque almacenado. En la Etapa 3
esto pasa a gRPC con mTLS; hoy es HTTP con un secreto compartido por cabecera.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

from dfsha.common.errors import StorageError
from dfsha.common.logging import get_logger

__all__ = ["ControlClient", "NodeIdentity"]

INTERNAL_SECRET_HEADER = "X-DFSha-Internal-Secret"
IDENTITY_FILE = "node.json"


class NodeIdentity:
    """Guarda el `data_node_id` en disco, junto a los bloques.

    Reiniciar el contenedor no puede producir un nodo nuevo: las replicas ya registradas
    apuntan al id viejo y quedarian ilocalizables. El ControlNode tambien deduplica por
    `base_url`, asi que esto son dos cinturones para el mismo pantalon: el fichero cubre
    el caso de que la URL anunciada cambie entre reinicios.
    """

    def __init__(self, data_dir: str | Path) -> None:
        self.path = Path(data_dir) / IDENTITY_FILE

    def read(self) -> str | None:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8")).get("data_node_id")
        except (json.JSONDecodeError, OSError):
            return None

    def write(self, data_node_id: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporal = self.path.with_suffix(".json.tmp")
        temporal.write_text(json.dumps({"data_node_id": data_node_id}), encoding="utf-8")
        temporal.replace(self.path)


class ControlClient:
    def __init__(self, control_url: str, internal_secret: str, timeout: float = 10.0) -> None:
        self.control_url = control_url.rstrip("/")
        self._headers = {INTERNAL_SECRET_HEADER: internal_secret}
        self._timeout = timeout
        self._log = get_logger("data_node")

    def register(
        self,
        base_url: str,
        capacity_bytes: int,
        max_attempts: int = 30,
        retry_seconds: float = 2.0,
    ) -> str:
        """Se registra reintentando: en docker compose el DataNode suele arrancar antes
        de que el ControlNode acepte conexiones, y eso no es un error."""
        ultimo: Exception | None = None

        for intento in range(1, max_attempts + 1):
            try:
                respuesta = httpx.post(
                    f"{self.control_url}/internal/v1/datanodes/register",
                    json={"base_url": base_url, "capacity_bytes": capacity_bytes},
                    headers=self._headers,
                    timeout=self._timeout,
                )
                respuesta.raise_for_status()
                data_node_id = respuesta.json()["data_node_id"]
                self._log.info(
                    "datanode.registered",
                    data_node_id=data_node_id,
                    base_url=base_url,
                    capacity_bytes=capacity_bytes,
                    attempts=intento,
                )
                return data_node_id
            except Exception as exc:  # httpx.HTTPError y errores de respuesta
                ultimo = exc
                self._log.warning(
                    "datanode.register_retry",
                    attempt=intento,
                    max_attempts=max_attempts,
                    error=type(exc).__name__,
                )
                if intento < max_attempts:
                    time.sleep(retry_seconds)

        raise StorageError(
            f"no se pudo registrar contra el ControlNode tras {max_attempts} intentos: {ultimo}"
        )

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
