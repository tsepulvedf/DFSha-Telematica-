"""Cliente HTTP del ControlNode.

Traduce las respuestas a los mismos DTOs que el servidor usa para serializarlas, asi que
cualquier divergencia del contrato falla aqui, en la frontera, y no tres capas mas
adentro.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from dfsha.common.dto import (
    ClusterStatusResponse,
    CommitResponse,
    CreateFileResponse,
    LsResponse,
    OpenFileResponse,
    StatResponse,
    TokenResponse,
)
from dfsha.common.errors import DFShaError

from .session import Session

__all__ = ["ControlApi", "resolve_path"]

API = "/api/v1"


class ControlApi:
    def __init__(self, session: Session, timeout: float = 60.0) -> None:
        self.session = session
        self.base = session.control_url.rstrip("/")
        self._timeout = timeout

    # --- Transporte --------------------------------------------------------

    def _request(self, method: str, url: str, autenticado: bool = True, **kwargs):
        cabeceras = self.session.headers if autenticado else {}
        respuesta = httpx.request(
            method, f"{self.base}{url}", headers=cabeceras, timeout=self._timeout, **kwargs
        )
        if respuesta.status_code >= 400:
            raise _to_error(respuesta)
        return respuesta

    # --- Autenticacion -----------------------------------------------------

    def register(self, username: str, password: str) -> None:
        self._request(
            "POST",
            f"{API}/auth/register",
            autenticado=False,
            json={"username": username, "password": password},
        )

    def login(self, username: str, password: str) -> TokenResponse:
        respuesta = self._request(
            "POST",
            f"{API}/auth/login",
            autenticado=False,
            json={"username": username, "password": password},
        )
        return TokenResponse.model_validate(respuesta.json())

    # --- Cluster -----------------------------------------------------------

    def cluster_status(self) -> ClusterStatusResponse:
        return ClusterStatusResponse.model_validate(
            self._request("GET", f"{API}/cluster/status").json()
        )

    # --- Namespace ---------------------------------------------------------

    def ls(self, path: str) -> LsResponse:
        return LsResponse.model_validate(
            self._request("GET", f"{API}/fs/ls", params={"path": path}).json()
        )

    def stat(self, path: str) -> StatResponse:
        return StatResponse.model_validate(
            self._request("GET", f"{API}/fs/stat", params={"path": path}).json()
        )

    def mkdir(self, path: str, parents: bool = False) -> None:
        self._request("POST", f"{API}/fs/mkdir", json={"path": path, "parents": parents})

    def rmdir(self, path: str, recursive: bool = False) -> None:
        self._request(
            "DELETE", f"{API}/fs/rmdir", params={"path": path, "recursive": recursive}
        )

    def rm(self, path: str) -> None:
        self._request("DELETE", f"{API}/fs/rm", params={"path": path})

    def mv(self, src: str, dst: str) -> None:
        self._request("POST", f"{API}/fs/mv", json={"src": src, "dst": dst})

    # --- Transferencia -----------------------------------------------------

    def create_file(
        self, path: str, size: int, block_size: int | None = None
    ) -> CreateFileResponse:
        cuerpo: dict = {"path": path, "size": size}
        if block_size:
            cuerpo["block_size"] = block_size
        return CreateFileResponse.model_validate(
            self._request("POST", f"{API}/files/create", json=cuerpo).json()
        )

    def commit_file(self, file_id: str) -> CommitResponse:
        return CommitResponse.model_validate(
            self._request("POST", f"{API}/files/{file_id}/commit").json()
        )

    def abort_file(self, file_id: str) -> None:
        self._request("POST", f"{API}/files/{file_id}/abort")

    def open_file(self, path: str) -> OpenFileResponse:
        return OpenFileResponse.model_validate(
            self._request("GET", f"{API}/files/open", params={"path": path}).json()
        )


def _to_error(respuesta: httpx.Response) -> DFShaError:
    """Reconstruye el error del servidor conservando su `code`.

    El status HTTP se pierde en el camino porque el codigo de dominio es mas informativo:
    `reservation_expired` dice mas que un 410 suelto.
    """
    try:
        cuerpo = respuesta.json()
        mensaje = cuerpo.get("message", respuesta.text)
        codigo = cuerpo.get("code", "http_error")
    except ValueError:
        mensaje = respuesta.text or f"error HTTP {respuesta.status_code}"
        codigo = "http_error"

    error = DFShaError(mensaje, status=respuesta.status_code)
    error.code = codigo  # type: ignore[misc]
    return error


def resolve_path(session: Session, path: str | None) -> str:
    """Resuelve una ruta del usuario contra el cwd del cliente.

    Se resuelve aqui, antes de salir a la red: el ControlNode solo acepta rutas
    absolutas, porque no guarda sesion ni sabe donde esta cada cliente.
    """
    if path is None or path == "":
        return session.cwd
    if path.startswith("/"):
        return path
    base = session.cwd.rstrip("/")
    return f"{base}/{path}"


def local_name(remote_path: str) -> str:
    return Path(remote_path).name or "descarga"
