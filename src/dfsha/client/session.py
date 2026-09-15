"""Sesion del cliente: token y directorio de trabajo, en `~/.dfsha/session.json`.

El cwd es del lado del cliente a proposito. El ControlNode es stateless: no guarda
sesiones, no sabe donde esta cada usuario, y cada peticion lleva su ruta ya absoluta. Eso
es lo que permitira poner varios ControlNode detras de un balanceador en la Etapa 3 sin
sesiones pegajosas.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from dfsha.common.errors import AuthenticationError

__all__ = ["Session", "SessionStore", "DEFAULT_SESSION_DIR"]

DEFAULT_SESSION_DIR = Path.home() / ".dfsha"
SESSION_FILE = "session.json"


@dataclass
class Session:
    control_url: str
    token: str | None = None
    username: str | None = None
    cwd: str = "/"

    @property
    def is_authenticated(self) -> bool:
        return bool(self.token)

    @property
    def headers(self) -> dict[str, str]:
        if not self.token:
            raise AuthenticationError("no has iniciado sesion; usa 'dfsha login'")
        return {"Authorization": f"Bearer {self.token}"}


class SessionStore:
    def __init__(self, directory: str | Path | None = None) -> None:
        self.directory = Path(directory or os.environ.get("DFSHA_HOME") or DEFAULT_SESSION_DIR)
        self.path = self.directory / SESSION_FILE

    def load(self, control_url: str) -> Session:
        if not self.path.exists():
            return Session(control_url=control_url)
        try:
            datos = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Una sesion corrupta no debe dejar la CLI inutilizable: se empieza de cero.
            return Session(control_url=control_url)

        return Session(
            control_url=datos.get("control_url", control_url),
            token=datos.get("token"),
            username=datos.get("username"),
            cwd=datos.get("cwd", "/"),
        )

    def save(self, session: Session) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "control_url": session.control_url,
                    "token": session.token,
                    "username": session.username,
                    "cwd": session.cwd,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        # El archivo lleva un token de acceso: solo su dueno debe poder leerlo. En
        # Windows chmod no hace gran cosa, pero en Linux y macOS, que es donde se
        # evaluara, evita dejar credenciales legibles por todo el sistema.
        try:
            self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
