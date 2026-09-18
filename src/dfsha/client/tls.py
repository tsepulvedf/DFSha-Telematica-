"""Como el CLIENTE verifica a los servidores cuando hablan por HTTPS.

Un solo sitio, por el mismo motivo que `common/tls`: la verificacion de TLS es de las
cosas que **parecen puestas cuando no lo estan**, y repartirla entre el ControlNode, los
DataNodes y el GC garantiza que una de las tres se quede atras.

## Por que hace falta decirle donde esta la CA

La CA de DFSha es **propia**, no la firmo nadie en quien el sistema confie de fabrica. Asi
que `httpx` la rechaza igual que rechazaria un certificado autofirmado, y con razon. Hay
dos formas de resolverlo y solo una es aceptable:

- **Instalar la CA en el almacen del sistema.** Afecta a TODO el equipo: cualquier programa
  pasaria a confiar en certificados firmados por nosotros. Desproporcionado.
- **Decirle a este cliente, y solo a el, en quien confiar.** Es lo que se hace.

## Y por que NO se desactiva la verificacion

`verify=False` haria que todo funcionara a la primera, y es exactamente el fallo que este
proyecto lleva toda la etapa persiguiendo: **algo que aparenta estar puesto y no lo esta**.
Un TLS sin verificar cifra el canal contra un observador pasivo y no protege de nada
frente a quien pueda ponerse en medio, que es el atacante que importa. Se veria seguro —el
`https://` sale igual— y no lo seria.

Si falta la CA, esto **falla y lo dice**, con la ruta que buscó.
"""

from __future__ import annotations

import os
import ssl
from pathlib import Path

from dfsha.common.errors import TlsCaNotFoundError
from dfsha.common.tls import ca_only_context

__all__ = ["verificacion_para", "CA_ENV"]

CA_ENV = "DFSHA_TLS_CA_CERT"

#: Donde se busca la CA si no se dice nada. Es donde la deja `gen_certs.py`.
RUTAS_POR_DEFECTO = (Path("certs/ca.crt"), Path.home() / ".dfsha" / "ca.crt")


def verificacion_para(url: str) -> ssl.SSLContext | bool:
    """Lo que hay que pasarle a `httpx` como `verify=` para hablar con `url`.

    Con `http://` devuelve `True`, que httpx ignora: no hay nada que verificar. Asi
    quien llama no tiene que preguntarse por el esquema, y **el mismo codigo sirve para
    los dos casos**, que es lo que evita que el camino con TLS sea uno aparte que nadie
    ejercita.
    """
    if not url.lower().startswith("https://"):
        return True
    return ca_only_context(_ca_o_fallar())


def _ca_o_fallar() -> Path:
    del_entorno = os.environ.get(CA_ENV, "").strip()
    if del_entorno:
        if not Path(del_entorno).is_file():
            raise TlsCaNotFoundError(
                f"{CA_ENV} apunta a '{del_entorno}', que no existe"
            )
        return Path(del_entorno)

    for ruta in RUTAS_POR_DEFECTO:
        if ruta.is_file():
            return ruta

    raise TlsCaNotFoundError(
        "el servidor habla HTTPS y no se encuentra la CA con la que verificarlo. "
        f"Genera los certificados con 'python scripts/gen_certs.py' o indica la ruta "
        f"con {CA_ENV}=/ruta/a/ca.crt",
        buscado=[str(r) for r in RUTAS_POR_DEFECTO],
    )
