"""Configuracion del ControlNode.

Todo por variables de entorno. Los secretos no tienen valor por defecto: si faltan, el
proceso no arranca. Un secreto por defecto en un repositorio publico es un hallazgo de
seguridad, y este proyecto se evalua justo en eso.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["ControlNodeSettings", "get_settings", "load_settings_or_exit"]

#: Longitud minima de un secreto. No evita que alguien use "aaaa...", pero si que se
#: cuele un "cambiame" o el valor vacio de .env.example.
MIN_SECRET_LENGTH = 16


class ControlNodeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DFSHA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    block_size: int = Field(default=64 * 1024 * 1024, gt=0)
    db_url: str = "sqlite:///./dfsha.db"
    #: Replica de lectura para el lado de consulta de CQRS. Vacia = todo al primario,
    #: que es un modo soportado: el sistema se comporta como en las etapas anteriores.
    #: Esa propiedad es deliberada, porque convierte "prescindir de la replica" en
    #: borrar una variable de entorno en vez de deshacer codigo.
    db_replica_url: str = ""

    # Sin default a proposito: es obligatorio.
    jwt_secret: str

    # --- mTLS del plano interno (Etapa 3, Bloque C) ------------------------
    #
    # SUSTITUYEN a DFSHA_INTERNAL_SECRET, que ya no existe. Un secreto compartido
    # protege contra quien no lo conoce, pero no dice QUIEN esta al otro lado:
    # cualquiera que lo tenga es todos a la vez. Con mTLS cada rol presenta su propio
    # certificado y revocar a uno no obliga a rotar el de todos.
    #
    # Sin default, igual que el secreto de JWT: un plano interno que arranca sin
    # autenticacion porque se olvido una variable es peor que uno que no arranca.
    tls_ca_cert: str
    tls_cert: str
    tls_key: str
    #: Puerto del plano interno. Va SEPARADO del de cliente a proposito: uvicorn no
    #: expone el certificado del cliente a la aplicacion, asi que no se puede exigir
    #: certificado para unas rutas y no para otras dentro del mismo puerto. Con un
    #: puerto propio, la exigencia la hace el propio TLS: quien no presente un
    #: certificado firmado por la CA no llega ni a enviar la peticion.
    internal_port: int = Field(default=8443, gt=0, lt=65536)

    #: Certificado con el que este servicio se presenta ante el CLIENTE (C2). Vacias =
    #: HTTP plano, que es el default y el modo de desarrollo. Van LAS DOS o NINGUNA.
    #:
    #: Separadas de DFSHA_TLS_CERT a proposito: aquel identifica al servicio DENTRO del
    #: cluster y sus nombres son internos; este lleva el nombre por el que llega el
    #: usuario, que en un despliegue real es publico. Que sean dos variables permite usar
    #: el mismo fichero en desarrollo sin que el diseno lo de por supuesto.
    client_tls_cert: str = ""
    client_tls_key: str = ""

    jwt_ttl_seconds: int = Field(default=3600, gt=0)
    write_ttl_seconds: int = Field(default=600, gt=0)
    log_level: str = "INFO"

    # --- Plano de control (Etapa 2) ---------------------------------------
    grpc_port: int = Field(default=9000, gt=0, lt=65536)
    heartbeat_interval_ms: int = Field(default=3000, gt=0)
    full_report_every_n: int = Field(default=20, gt=0)
    #: Umbrales agresivos a proposito: con un latido cada 3 s, 10 s son tres perdidos y
    #: 30 s son diez. Se eligieron para que la transicion quepa en la demostracion del
    #: hito; en produccion serian mucho mas largos.
    suspect_after_ms: int = Field(default=10_000, gt=0)
    dead_after_ms: int = Field(default=30_000, gt=0)
    #: Cada cuanto corre el evaluador de pertenencia. Configurable a proposito: es lo que
    #: marca cuanto tarda en registrarse una transicion cuando nadie esta consultando.
    membership_interval_ms: int = Field(default=1000, gt=0)
    #: Hilos del servidor gRPC. Cada stream de heartbeat ocupa uno mientras esta abierto,
    #: asi que tiene que sobrar sitio respecto al numero de DataNodes.
    grpc_max_workers: int = Field(default=16, gt=0)

    # --- Liderazgo (Etapa 3) ------------------------------------------------
    #: Cuanto vale un lease sin renovar. Si el lider se calla mas de esto, otra
    #: instancia puede tomarlo subiendo la epoca.
    lease_ttl_ms: int = Field(default=6000, gt=0)
    #: Vida de un lock de archivo (RF3). Dos ordenes de magnitud mas que el lease de
    #: liderazgo, y por un motivo: ahi el relevo tiene que ser rapido porque un cluster sin
    #: lider no se repara; aqui lo que espera es una PERSONA que tiene el archivo abierto,
    #: y arrebatarselo a los seis segundos porque tardo en teclear seria inutilizable.
    #: El precio de que sea largo es que un cliente que muere deja el archivo bloqueado
    #: ese tiempo.
    file_lock_ttl_ms: int = Field(default=120_000, gt=0)
    #: Cada cuanto renueva el lider. Tiene que ser bastante menor que el TTL: entre los
    #: dos valores cabe el margen para una pausa, un pico de latencia de la base o un
    #: reintento, sin que el liderazgo cambie de manos por nada.
    lease_renew_ms: int = Field(default=2000, gt=0)

    # --- Colocacion --------------------------------------------------------
    #: Etapa 3: sube de 1 a 3. La politica de colocacion soporta R>1 y esta probada
    #: para ello desde la Etapa 2; lo unico que cambia aqui es el default.
    replication_factor: int = Field(default=3, gt=0)
    #: Replicas confirmadas que exige el `commit`. Con W=2 y R=3, el archivo queda
    #: legible con dos copias y la tercera se completa despues: un archivo con 2 de 3 no
    #: esta roto, todavia tolera perder un nodo. Rechazar el commit por eso pondria la
    #: durabilidad por encima de la disponibilidad, que es la eleccion contraria a la que
    #: hacen estos sistemas.
    write_quorum: int = Field(default=2, gt=0)
    placement_d: int = Field(default=3, gt=0)
    #: Margen de seguridad: un nodo necesita block_size + esto para ser candidato.
    min_free_bytes: int = Field(default=128 * 1024 * 1024, ge=0)

    # --- Re-replicacion (Etapa 3) ------------------------------------------
    #: Cuanto se espera desde que un nodo entra en DEAD antes de copiar sus bloques.
    #: Reiniciar un contenedor tarda segundos; copiar su disco entero por un reinicio es
    #: el error clasico, y ademas se encadena: la copia satura la red, otro nodo deja de
    #: latir a tiempo, y se dispara otra copia. Para el video se baja a 30 s.
    rereplication_grace_ms: int = Field(default=300_000, ge=0)
    #: Copias simultaneas como maximo hacia el mismo DataNode destino. Sin tope, la
    #: recuperacion se concentra en el nodo mas vacio y lo tumba por saturacion.
    rereplication_max_per_node: int = Field(default=2, gt=0)
    #: Cada cuanto corre el planificador. Mucho mas lento que el evaluador de pertenencia
    #: a proposito: con una gracia de 5 minutos, escanear cada segundo seria recorrer el
    #: metadato 300 veces para no hacer nada.
    rereplication_interval_ms: int = Field(default=5_000, gt=0)
    #: Copias despachadas como maximo en una pasada, para que el cluster respire entre
    #: tandas en vez de vaciar la cola de golpe.
    rereplication_max_per_pass: int = Field(default=8, gt=0)

    @model_validator(mode="after")
    def _quorum_alcanzable(self) -> "ControlNodeSettings":
        if self.write_quorum > self.replication_factor:
            raise ValueError(
                f"DFSHA_WRITE_QUORUM ({self.write_quorum}) no puede ser mayor que "
                f"DFSHA_REPLICATION_FACTOR ({self.replication_factor}): ningun commit "
                "podria alcanzar un quorum que la colocacion no llega a planificar, y "
                "todas las subidas fallarian con 409 sin explicar por que"
            )
        return self

    @model_validator(mode="after")
    def _lease_coherente(self) -> "ControlNodeSettings":
        if self.lease_renew_ms >= self.lease_ttl_ms:
            raise ValueError(
                "DFSHA_LEASE_RENEW_MS debe ser menor que DFSHA_LEASE_TTL_MS; si no, el "
                "lider pierde el lease antes de tener ocasion de renovarlo y el "
                "liderazgo cambiaria de manos continuamente sin que haya fallado nada"
            )
        return self

    @model_validator(mode="after")
    def _umbrales_coherentes(self) -> "ControlNodeSettings":
        if self.suspect_after_ms >= self.dead_after_ms:
            raise ValueError(
                "DFSHA_SUSPECT_AFTER_MS debe ser menor que DFSHA_DEAD_AFTER_MS; si no, "
                "el estado SUSPECT no existiria y un hipo de red costaria dar por "
                "perdidas las replicas de un nodo que sigue vivo"
            )
        return self

    @field_validator("tls_ca_cert", "tls_cert", "tls_key")
    @classmethod
    def _fichero_existe(cls, value: str, info) -> str:
        """Que el fichero este, y decirlo al arrancar si no.

        Un certificado que falta se manifestaria como un handshake fallido en la primera
        conexion de un DataNode, que es un sintoma varias capas por debajo de la causa.
        """
        ruta = Path(value.strip())
        if not value.strip():
            raise ValueError("es obligatorio; generalos con python scripts/gen_certs.py")
        if not ruta.is_file():
            raise ValueError(
                f"no existe el fichero '{ruta}'; generalos con "
                "python scripts/gen_certs.py"
            )
        return str(ruta)

    @field_validator("jwt_secret")
    @classmethod
    def _secreto_con_cuerpo(cls, value: str) -> str:
        if len(value.strip()) < MIN_SECRET_LENGTH:
            raise ValueError(
                f"debe tener al menos {MIN_SECRET_LENGTH} caracteres; genera uno con "
                "python -c \"import secrets; print(secrets.token_urlsafe(48))\""
            )
        return value


def load_settings_or_exit() -> ControlNodeSettings:
    """Carga la configuracion o termina el proceso con un mensaje util.

    Sin esto, faltar `DFSHA_JWT_SECRET` produce un volcado de pydantic en medio del
    arranque de Uvicorn que no dice que hacer.
    """
    try:
        return ControlNodeSettings()
    except ValidationError as exc:
        print("ControlNode: configuracion invalida.", file=sys.stderr)
        for error in exc.errors():
            variable = "DFSHA_" + str(error["loc"][0]).upper()
            motivo = error["msg"].removeprefix("Value error, ")
            print(f"  - {variable}: {motivo}", file=sys.stderr)
        print(
            "\nCopia .env.example a .env y completa los valores que faltan.",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc


@lru_cache(maxsize=1)
def get_settings() -> ControlNodeSettings:
    return load_settings_or_exit()
