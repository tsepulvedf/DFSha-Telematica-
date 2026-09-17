"""Cliente HTTP del ControlNode.

Traduce las respuestas a los mismos DTOs que el servidor usa para serializarlas, asi que
cualquier divergencia del contrato falla aqui, en la frontera, y no tres capas mas
adentro.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from dfsha.common.dto import (
    AppendResponse,
    LockResponse,
    LocksResponse,
    OpenResponse,
    ReadRangeResponse,
    ClusterStatusResponse,
    CommitResponse,
    CreateFileResponse,
    AclResponse,
    GroupsResponse,
    LeadershipResponse,
    LsResponse,
    SharedWithMeResponse,
    OpenFileResponse,
    StatResponse,
    TokenResponse,
)
from dfsha.common.errors import DFShaError

from .session import Session, SessionStore

__all__ = ["ControlApi", "resolve_path", "READ_LSN_HEADER", "WRITE_LSN_HEADER"]

API = "/api/v1"

#: Read-your-writes. El ControlNode devuelve el LSN del primario tras cada comando y el
#: cliente lo reenvia en sus consultas; con eso, una replica retrasada no puede negarle
#: al cliente una escritura que acaba de hacer. Ver services/read_routing.py.
WRITE_LSN_HEADER = "X-DFSha-Write-LSN"
READ_LSN_HEADER = "X-DFSha-Read-LSN"


class ControlApi:
    def __init__(
        self,
        session: Session,
        timeout: float = 60.0,
        store: SessionStore | None = None,
    ) -> None:
        self.session = session
        self.base = session.control_url.rstrip("/")
        self._timeout = timeout
        #: Si se pasa, el LSN de escritura se persiste en cuanto llega. Sin el, el
        #: mecanismo sigue siendo correcto dentro de un mismo proceso.
        self._store = store

    # --- Transporte --------------------------------------------------------

    def _request(self, method: str, url: str, autenticado: bool = True, **kwargs):
        cabeceras = dict(self.session.headers) if autenticado else {}
        if self.session.last_write_lsn:
            cabeceras[READ_LSN_HEADER] = self.session.last_write_lsn

        respuesta = httpx.request(
            method, f"{self.base}{url}", headers=cabeceras, timeout=self._timeout, **kwargs
        )
        if respuesta.status_code >= 400:
            raise _to_error(respuesta)

        self._recordar_lsn(respuesta)
        return respuesta

    def _recordar_lsn(self, respuesta: httpx.Response) -> None:
        """Guarda el LSN que devolvio el ControlNode tras un comando.

        Solo avanza: el ControlNode devuelve un LSN monotono, pero dos peticiones
        concurrentes pueden llegar desordenadas al cliente, y quedarse con el menor
        anularia la garantia para la escritura mas reciente.
        """
        nuevo = respuesta.headers.get(WRITE_LSN_HEADER)
        if not nuevo or nuevo == self.session.last_write_lsn:
            return
        anterior = _lsn_a_entero(self.session.last_write_lsn)
        if anterior is not None and (_lsn_a_entero(nuevo) or 0) < anterior:
            return

        self.session.last_write_lsn = nuevo
        if self._store is not None:
            self._store.save(self.session)

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

    # --- Permisos ----------------------------------------------------------

    def share(self, path: str, principal: str, permission: str) -> None:
        self._request(
            "POST",
            f"{API}/acl/share",
            json={"path": path, "principal": principal, "permission": permission},
        )

    def unshare(self, path: str, principal: str) -> None:
        self._request(
            "POST", f"{API}/acl/unshare", json={"path": path, "principal": principal}
        )

    def acl(self, path: str) -> AclResponse:
        return AclResponse.model_validate(
            self._request("GET", f"{API}/acl/show", params={"path": path}).json()
        )

    def shared_with_me(self) -> SharedWithMeResponse:
        return SharedWithMeResponse.model_validate(
            self._request("GET", f"{API}/acl/shared-with-me").json()
        )

    def create_group(self, name: str) -> None:
        self._request("POST", f"{API}/acl/groups", json={"name": name})

    def list_groups(self) -> GroupsResponse:
        return GroupsResponse.model_validate(
            self._request("GET", f"{API}/acl/groups").json()
        )

    def add_member(self, name: str, username: str) -> None:
        self._request(
            "POST",
            f"{API}/acl/groups/members",
            json={"name": name, "username": username},
        )

    def remove_member(self, name: str, username: str) -> None:
        self._request(
            "POST",
            f"{API}/acl/groups/members/remove",
            json={"name": name, "username": username},
        )

    def leadership(self) -> LeadershipResponse:
        return LeadershipResponse.model_validate(
            self._request("GET", f"{API}/cluster/leadership").json()
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
        self,
        path: str,
        size: int,
        block_size: int | None = None,
        cipher_overhead: int = 0,
    ) -> CreateFileResponse:
        """Reserva el plan de escritura.

        `size` son bytes CLAROS y `cipher_overhead` lo que el cifrado del cliente anade a
        cada bloque. El ControlNode los necesita separados: uno es lo que el usuario ve y
        el otro lo que hay que reservar en disco.
        """
        cuerpo: dict = {"path": path, "size": size}
        if block_size:
            cuerpo["block_size"] = block_size
        if cipher_overhead:
            cuerpo["cipher_overhead"] = cipher_overhead
        return CreateFileResponse.model_validate(
            self._request("POST", f"{API}/files/create", json=cuerpo).json()
        )

    def commit_file(
        self, file_id: str, wrapped_key: str = "", key_algo: str = ""
    ) -> CommitResponse:
        return CommitResponse.model_validate(
            self._request(
                "POST",
                f"{API}/files/{file_id}/commit",
                json={"wrapped_key": wrapped_key, "key_algo": key_algo},
            ).json()
        )

    def abort_file(self, file_id: str) -> None:
        self._request("POST", f"{API}/files/{file_id}/abort")

    def open_file(self, path: str) -> OpenFileResponse:
        return OpenFileResponse.model_validate(
            self._request("GET", f"{API}/files/open", params={"path": path}).json()
        )


    # --- RF3: open / read / write / lock -----------------------------------

    def open_handle(
        self, path: str, mode: str = "read", lock: bool = False, holder: str = ""
    ) -> OpenResponse:
        """Abre un archivo y, si `lock`, lo bloquea en la MISMA transaccion."""
        return OpenResponse.model_validate(
            self._request(
                "POST",
                f"{API}/fs/open",
                json={"path": path, "mode": mode, "lock": lock, "holder": holder},
            ).json()
        )

    def lock(self, path: str, mode: str = "exclusive", holder: str = "") -> LockResponse:
        """Toma el lock, o lo renueva si ya era de este titular."""
        return LockResponse.model_validate(
            self._request(
                "POST",
                f"{API}/fs/lock",
                json={"path": path, "mode": mode, "holder": holder},
            ).json()
        )

    def unlock(self, path: str, holder: str = "") -> None:
        self._request("POST", f"{API}/fs/unlock", json={"path": path, "holder": holder})

    def locks(self, path: str) -> LocksResponse:
        return LocksResponse.model_validate(
            self._request("GET", f"{API}/fs/locks", params={"path": path}).json()
        )

    def read_range(
        self, path: str, offset: int = 0, length: int | None = None
    ) -> ReadRangeResponse:
        params: dict = {"path": path, "offset": offset}
        if length is not None:
            params["length"] = length
        return ReadRangeResponse.model_validate(
            self._request("GET", f"{API}/files/read", params=params).json()
        )

    def commit_append(
        self,
        file_id: str,
        block_ids: list[str],
        new_size: int,
        *,
        replaces: str = "",
        lock_holder: str = "",
        lock_epoch: int = 0,
    ) -> CommitResponse:
        return CommitResponse.model_validate(
            self._request(
                "POST",
                f"{API}/files/{file_id}/append/commit",
                json={
                    "block_ids": block_ids,
                    "new_size": new_size,
                    "replaces": replaces,
                    "lock_holder": lock_holder,
                    "lock_epoch": lock_epoch,
                },
            ).json()
        )

    def append(
        self,
        file_id: str,
        size: int,
        *,
        cipher_overhead: int = 0,
        lock_holder: str = "",
        lock_epoch: int = 0,
    ) -> AppendResponse:
        return AppendResponse.model_validate(
            self._request(
                "POST",
                f"{API}/files/{file_id}/append",
                json={
                    "size": size,
                    "cipher_overhead": cipher_overhead,
                    "lock_holder": lock_holder,
                    "lock_epoch": lock_epoch,
                },
            ).json()
        )


def _to_error(respuesta: httpx.Response) -> DFShaError:
    """Reconstruye el error del servidor conservando su `code` y sus `details`.

    El status HTTP se pierde en el camino porque el codigo de dominio es mas informativo:
    `reservation_expired` dice mas que un 410 suelto.

    **Los `details` se conservan**, y no es un adorno: es donde viajan los datos con los
    que el cliente puede hacer algo. Un `file_locked` trae `holder` y
    `retry_after_seconds`, que es exactamente lo que hace falta para decidir si esperar;
    descartarlos dejaba «esta bloqueado» a secas, que no permite decidir nada.
    """
    detalles: dict = {}
    try:
        cuerpo = respuesta.json()
        mensaje = cuerpo.get("message", respuesta.text)
        codigo = cuerpo.get("code", "http_error")
        detalles = cuerpo.get("details") or {}
    except ValueError:
        mensaje = respuesta.text or f"error HTTP {respuesta.status_code}"
        codigo = "http_error"

    error = DFShaError(mensaje, status=respuesta.status_code, **detalles)
    error.code = codigo  # type: ignore[misc]
    return error


def _lsn_a_entero(lsn: str | None) -> int | None:
    """'16/B374D848' -> entero comparable. `None` si no es un LSN."""
    if not lsn:
        return None
    alto, _, bajo = lsn.partition("/")
    if not bajo:
        return None
    try:
        return (int(alto, 16) << 32) + int(bajo, 16)
    except ValueError:
        return None


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
