"""Sonda de salud de los contenedores, en HTTP o en HTTPS segun la configuracion.

    python -m dfsha.common.healthcheck 8000

## Por que no es una linea de `httpx.get('http://...')` en el Dockerfile

Porque asi estaba, y con el TLS de cliente (C2) encendido **cada contenedor se declaraba
enfermo estando sano**: el servidor ya solo hablaba HTTPS y la sonda seguia preguntando en
HTTP. Y no se quedaba en un estado feo en `docker compose ps`: el balanceador y los
DataNodes esperan con `depends_on: service_healthy`, asi que el stack **no llegaba a
levantar**. C2 estaba implementado y probado, y no se podia encender.

La sonda decide el esquema con **la misma variable que decide el servidor**
(`DFSHA_CLIENT_TLS_CERT`), asi que no pueden discrepar. Es la regla de `client/tls.py`
aplicada a la sonda: un solo camino para los dos esquemas, porque uno aparte es uno que
nadie ejercita hasta el dia que hace falta.

**Con HTTPS verifica contra la CA del proyecto**, no con `verify=False`: una sonda que
aceptara cualquier certificado daria por sano un servidor que ningun cliente de verdad
podria usar, que es exactamente el tipo de fallo que esta etapa lleva persiguiendo.
"""

from __future__ import annotations

import os
import ssl
import sys

import httpx

from dfsha.common.tls import ca_only_context

__all__ = ["destino", "main"]


def destino(puerto: int) -> tuple[str, ssl.SSLContext | bool]:
    """La URL de salud de ESTE contenedor y como verificarla.

    Con TLS se pregunta a `localhost` y no a `127.0.0.1`: es el nombre que los
    certificados del proyecto llevan para el acceso local, y lo que se comprueba es el
    certificado que vera un cliente, con su nombre.
    """
    if os.environ.get("DFSHA_CLIENT_TLS_CERT", "").strip():
        return (
            f"https://localhost:{puerto}/health",
            ca_only_context(os.environ["DFSHA_TLS_CA_CERT"]),
        )
    return f"http://127.0.0.1:{puerto}/health", True


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    puerto = int(argv[0]) if argv else 8000
    try:
        url, verify = destino(puerto)
        return 0 if httpx.get(url, timeout=3, verify=verify).status_code == 200 else 1
    except Exception as exc:  # noqa: BLE001 - una sonda contesta 0 o 1, no revienta
        print(f"healthcheck: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
