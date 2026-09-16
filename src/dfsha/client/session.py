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
    #: LSN del WAL de PostgreSQL de la ultima escritura de ESTE cliente.
    #:
    #: Se guarda en disco y no en memoria porque cada invocacion de `dfsha` es un
    #: proceso nuevo: sin persistirlo, un `mkdir /a` seguido de un `ls /` en la linea
    #: siguiente no tendria forma de saber que hay una escritura que la replica quiza
    #: no ha reproducido, que es justo el caso que esto existe para cubrir.
    last_write_lsn: str | None = None
    #: De donde salio (o saldria) esta sesion. Solo sirve para poder decirlo en los
    #: mensajes de error: saber que fichero se miro ahorra la mitad del diagnostico
    #: cuando la sesion no persiste, por ejemplo dentro de un contenedor.
    session_path: str | None = None

    @property
    def is_authenticated(self) -> bool:
        return bool(self.token)

    @property
    def headers(self) -> dict[str, str]:
        if not self.token:
            donde = f" (sesion buscada en {self.session_path})" if self.session_path else ""
            raise AuthenticationError(
                f"no has iniciado sesion; usa 'dfsha login'{donde}"
            )
        return {"Authorization": f"Bearer {self.token}"}


class SessionStore:
    def __init__(self, directory: str | Path | None = None) -> None:
        self.directory = Path(directory or os.environ.get("DFSHA_HOME") or DEFAULT_SESSION_DIR)
        self.path = self.directory / SESSION_FILE

    def load(self, control_url: str) -> Session:
        if not self.path.exists():
            return Session(control_url=control_url, session_path=str(self.path))
        try:
            datos = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # Una sesion corrupta no debe dejar la CLI inutilizable: se empieza de cero.
            return Session(control_url=control_url, session_path=str(self.path))
        except OSError as exc:
            # Hay sesion guardada pero no se puede leer, casi siempre por permisos. Antes
            # esto se trataba como "no hay sesion", y el sintoma era un desconcertante
            # "no has iniciado sesion" justo despues de un login que dijo que fue bien.
            raise AuthenticationError(
                f"hay una sesion en {self.path} pero no se puede leer: {exc.strerror or exc}"
            ) from exc

        return Session(
            control_url=datos.get("control_url", control_url),
            token=datos.get("token"),
            username=datos.get("username"),
            cwd=datos.get("cwd", "/"),
            last_write_lsn=datos.get("last_write_lsn"),
            session_path=str(self.path),
        )

    def save(self, session: Session) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(
                    {
                        "control_url": session.control_url,
                        "token": session.token,
                        "username": session.username,
                        "cwd": session.cwd,
                        "last_write_lsn": session.last_write_lsn,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            # Falla ruidosamente y diciendo donde. Un login que no consigue guardar el
            # token y aun asi dice "sesion iniciada" deja al siguiente comando quejandose
            # de que no hay sesion, sin ninguna pista de por que.
            raise AuthenticationError(
                f"no se pudo guardar la sesion en {self.path}: {exc.strerror or exc}"
            ) from exc
        # El archivo lleva un token de acceso: solo su dueno debe poder leerlo. En
        # Windows chmod no hace gran cosa, pero en Linux y macOS, que es donde se
        # evaluara, evita dejar credenciales legibles por todo el sistema.
        try:
            self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)
