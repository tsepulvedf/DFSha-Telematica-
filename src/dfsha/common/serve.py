"""Arranque de los servidores de cara al CLIENTE, con o sin TLS.

## Por que existe este modulo y no un `CMD` de uvicorn con mas banderas

Porque el TLS de cliente es **opcional** y la decision se toma con la configuracion, no
con la linea de comandos. Un `CMD` fijo en el Dockerfile no puede decidir; uno con las
banderas puestas obligaria a que todo despliegue tuviera certificados de cliente, y
entonces el `docker compose` de desarrollo dejaria de arrancar con `curl localhost:8000`.

## TLS de cliente y mTLS interno son dos cosas distintas, a proposito

| | Plano interno | Plano de cliente |
|---|---|---|
| Quien llama | un servicio nuestro | una **persona** |
| Como se identifica | su **certificado** | su **JWT** |
| El servidor exige certificado | **si** (`CERT_REQUIRED`) | **no** |
| Que aporta TLS | cifrado **y** identidad | cifrado e identidad **del servidor** |

Darle un certificado a cada usuario seria montar una PKI para usuarios —emision,
distribucion, revocacion, rotacion— para acabar sabiendo lo mismo que ya dice el token.
Aqui TLS hace lo que hace bien: cifrar el canal y demostrar que el servidor es quien dice
ser. Quien es el usuario lo dice la capa de arriba, y eso no es un hueco.

## Que protege de verdad, dicho sin inflarlo

Los **bloques ya viajaban cifrados** desde el Bloque C: el cliente los cifra antes de que
salgan de su maquina, asi que un observador de la red nunca pudo leer su contenido. Lo que
esto anade es proteger lo que quedaba en claro:

- el **token JWT**, que iba en una cabecera y con el que cualquiera podria suplantar al
  usuario durante la vida del token;
- el **metadato**: nombres de archivos, rutas, tamanos y con quien se comparte;
- los **tokens de bloque**, que autorizan a bajarse un bloque concreto.

Es una pieza necesaria y no la que sostiene la confidencialidad del contenido. Conviene
decirlo en ese orden.
"""

from __future__ import annotations

import ssl
from pathlib import Path

__all__ = ["run", "ClientTls"]


class ClientTls:
    """El certificado con el que un servicio se presenta ante el CLIENTE.

    Distinto del de `TlsMaterial` a proposito: ese identifica al servicio dentro del
    cluster y sus nombres son internos (`control-node-1`, IP privadas). El de cara al
    cliente tiene que llevar el nombre por el que el **usuario** llega, que en un
    despliegue real es publico y no tiene por que parecerse.

    Que sean dos variables distintas es lo que permite que en desarrollo se use el mismo
    certificado para las dos cosas sin que el diseno lo dé por supuesto.
    """

    def __init__(self, cert: str, key: str) -> None:
        self.cert = Path(cert)
        self.key = Path(key)
        for ruta in (self.cert, self.key):
            if not ruta.is_file():
                raise ValueError(
                    f"no existe el fichero '{ruta}'; generalos con "
                    "python scripts/gen_certs.py"
                )

    @classmethod
    def from_settings(cls, cert: str, key: str) -> "ClientTls | None":
        """`None` si no hay TLS de cliente configurado, que es el default.

        **Las dos variables o ninguna.** Configurar solo una es casi siempre un error de
        copia y pega, y arrancar en HTTP ignorandolo en silencio dejaria a alguien
        convencido de que su trafico va cifrado. Falla al arrancar y lo dice.
        """
        if not cert and not key:
            return None
        if not (cert and key):
            raise ValueError(
                "DFSHA_CLIENT_TLS_CERT y DFSHA_CLIENT_TLS_KEY van juntas: configurar "
                "solo una arrancaria en HTTP sin avisar"
            )
        return cls(cert, key)

    def uvicorn_kwargs(self) -> dict:
        return {
            "ssl_certfile": str(self.cert),
            "ssl_keyfile": str(self.key),
            # CERT_NONE, y es LA diferencia con el plano interno: al cliente NO se le pide
            # certificado, porque su identidad es el JWT. Ver la tabla del modulo.
            "ssl_cert_reqs": ssl.CERT_NONE,
        }


def run(factory: str, host: str, port: int, tls: ClientTls | None) -> None:
    """Levanta uvicorn sobre `factory`, en HTTPS si hay material y en HTTP si no."""
    import uvicorn

    uvicorn.run(
        factory,
        factory=True,
        host=host,
        port=port,
        **(tls.uvicorn_kwargs() if tls else {}),
    )
