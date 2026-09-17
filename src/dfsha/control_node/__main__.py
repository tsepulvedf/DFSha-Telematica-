"""Punto de entrada del ControlNode, con TLS de cliente opcional.

    python -m dfsha.control_node

Va por aqui y no por un `CMD` de uvicorn con banderas fijas porque el TLS de cliente es
**opcional** y la decision se toma con la configuracion. Ver `common/serve.py`.
"""

from __future__ import annotations

import sys

from dfsha.common.serve import ClientTls, run
from dfsha.control_node.config import load_settings_or_exit


def main() -> int:
    ajustes = load_settings_or_exit()
    try:
        tls = ClientTls.from_settings(ajustes.client_tls_cert, ajustes.client_tls_key)
    except ValueError as error:
        print(f"ControlNode: configuracion de TLS de cliente invalida.
  - {error}", file=sys.stderr)
        return 2

    run("dfsha.control_node.main:create_app", host="0.0.0.0", port=8000, tls=tls)
    return 0


if __name__ == "__main__":
    sys.exit(main())
