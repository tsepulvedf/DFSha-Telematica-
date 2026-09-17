"""Configuracion del DataNode."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["DataNodeSettings", "load_settings_or_exit"]

MIN_SECRET_LENGTH = 16


class DataNodeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DFSHA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    data_dir: str = "/var/lib/dfsha"
    #: Plano INTERNO del ControlNode, no el de cliente. Variable propia y no reutilizar
    #: DFSHA_CONTROL_URL porque desde la Etapa 3 son endpoints de verdad distintos:
    #: otro puerto, otro esquema y otra forma de autenticarse (certificado en vez de
    #: token). Llamarlos igual invitaria a apuntar el DataNode al puerto de cliente y
    #: descubrirlo con un 404 en la primera subida.
    control_internal_url: str = "https://localhost:8443"

    #: Con que URL se anuncia el DataNode. Tiene que ser la alcanzable POR EL CLIENTE:
    #: el ControlNode se limita a repetirsela, porque los bytes van directos. Una sola
    #: direccion por nodo, fijada por el despliegue; ver la decision en CLAUDE.md.
    datanode_advertise_url: str = "http://localhost:8001"
    #: Con que direccion se anuncia a OTROS DATANODES. Vacia = la misma del cliente.
    #:
    #: Existe desde la Etapa 3 porque aparecio un hecho nuevo: hasta la Etapa 2 solo el
    #: cliente hablaba con los DataNodes, y ahora los DataNodes hablan entre si (pipeline
    #: de escritura y re-replicacion). Los dos grupos pueden estar en redes distintas: en
    #: compose el cliente esta fuera (localhost:800N) y los vecinos dentro
    #: (data-node-N:8001), y una sola direccion no puede ser correcta para los dos.
    #:
    #: Esto NO contradice la decision de la Etapa 2, que fue que el ControlNode no
    #: infiriera la direccion segun el origen de la peticion. Aqui no hay inferencia: las
    #: dos direcciones son estaticas y su destinatario se sabe por la ESTRUCTURA del
    #: mensaje, no por quien llama.
    datanode_peer_url: str = ""

    #: Cadena opaca que agrupa nodos que pueden caerse juntos. En local son etiquetas
    #: (local-1..local-4); en AWS, zonas de disponibilidad. El ControlNode solo compara
    #: igualdad, asi que el mismo codigo sirve para simular y para ser real.
    datanode_fault_domain: str = "local-1"
    #: Donde escucha el plano de control. gRPC, no REST.
    control_grpc_url: str = "localhost:9000"

    #: Vacio = se deduce del espacio libre del disco al arrancar.
    datanode_capacity_bytes: int | None = None

    #: Copias simultaneas que este nodo acepta ejecutar. Acotado a proposito: sin tope,
    #: veinte ordenes a la vez saturarian el disco del nodo que precisamente acaba de
    #: ofrecerse como destino porque estaba menos cargado.
    order_workers: int = Field(default=2, gt=0)

    # --- mTLS (Etapa 3, Bloque C) ------------------------------------------
    # Sustituyen a DFSHA_INTERNAL_SECRET. Sin default: un DataNode que hablara con el
    # plano de control sin autenticarse seria justo el agujero que esto cierra.
    tls_ca_cert: str
    tls_cert: str
    tls_key: str
    #: Certificado con el que este servicio se presenta ante el CLIENTE (C2). Vacias =
    #: HTTP plano, que es el default y el modo de desarrollo. Van LAS DOS o NINGUNA.
    #:
    #: Separadas de DFSHA_TLS_CERT a proposito: aquel identifica al servicio DENTRO del
    #: cluster y sus nombres son internos; este lleva el nombre por el que llega el
    #: usuario, que en un despliegue real es publico. Que sean dos variables permite usar
    #: el mismo fichero en desarrollo sin que el diseno lo de por supuesto.
    client_tls_cert: str = ""
    client_tls_key: str = ""

    log_level: str = "INFO"
    register_retry_seconds: float = Field(default=2.0, gt=0)
    register_max_attempts: int = Field(default=30, gt=0)

    @field_validator("tls_ca_cert", "tls_cert", "tls_key")
    @classmethod
    def _fichero_existe(cls, value: str) -> str:
        ruta = Path(value.strip())
        if not value.strip():
            raise ValueError("es obligatorio; generalos con python scripts/gen_certs.py")
        if not ruta.is_file():
            raise ValueError(
                f"no existe el fichero '{ruta}'; generalos con "
                "python scripts/gen_certs.py"
            )
        return str(ruta)

    def resolved_capacity_bytes(self) -> int:
        """Capacidad anunciada.

        Sin valor explicito se usa lo que queda libre en el disco ahora mismo, mas lo que
        este DataNode ya tiene ocupado. Anunciar solo el libre haria que la capacidad
        encogiera sola segun se escriben bloques, y la Etapa 2 leeria eso como un nodo que
        se esta quedando sin sitio cuando en realidad solo esta trabajando.
        """
        if self.datanode_capacity_bytes:
            return self.datanode_capacity_bytes

        ruta = Path(self.data_dir)
        ruta.mkdir(parents=True, exist_ok=True)
        uso = shutil.disk_usage(ruta)
        return uso.free


def load_settings_or_exit() -> DataNodeSettings:
    try:
        return DataNodeSettings()
    except ValidationError as exc:
        print("DataNode: configuracion invalida.", file=sys.stderr)
        for error in exc.errors():
            variable = "DFSHA_" + str(error["loc"][0]).upper()
            motivo = error["msg"].removeprefix("Value error, ")
            print(f"  - {variable}: {motivo}", file=sys.stderr)
        print(
            "\nCopia .env.example a .env y completa los valores que faltan.",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
