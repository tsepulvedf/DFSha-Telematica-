"""CA de pruebas, generada una sola vez por sesion de pytest.

Generar un par de claves RSA cuesta unos cientos de milisegundos, y aqui hacen falta
cuatro. Multiplicado por cada fixture de cluster serian varios segundos de suite gastados
en criptografia que no se esta probando, asi que se genera una vez y se reutiliza.

**Son certificados de prueba, en un directorio temporal, y se van con la sesion.** No
tienen nada que ver con los de un despliegue: los de verdad los genera
`scripts/gen_certs.py`, que es el mismo codigo, con los nombres del despliegue.
"""

from __future__ import annotations

import functools
import tempfile
from pathlib import Path

from dfsha.common.tls import TlsMaterial

__all__ = ["ca_de_pruebas", "material", "material_sin_firmar"]


@functools.lru_cache(maxsize=1)
def ca_de_pruebas() -> Path:
    """Directorio con la CA y los tres certificados de rol. Una vez por sesion."""
    import sys

    raiz = Path(__file__).resolve().parents[1]
    if str(raiz / "scripts") not in sys.path:
        sys.path.insert(0, str(raiz / "scripts"))

    from gen_certs import main as generar

    destino = Path(tempfile.mkdtemp(prefix="dfsha-certs-"))
    codigo = generar(["--out", str(destino)])
    assert codigo == 0, "no se pudieron generar los certificados de prueba"
    return destino


def material(rol: str = "control") -> TlsMaterial:
    """Material TLS de un rol: `control`, `data` o `client`."""
    directorio = ca_de_pruebas()
    return TlsMaterial(
        ca_cert=directorio / "ca.crt",
        cert=directorio / f"{rol}.crt",
        key=directorio / f"{rol}.key",
    )


@functools.lru_cache(maxsize=1)
def material_sin_firmar() -> TlsMaterial:
    """Una CA DISTINTA, con su propio certificado de cliente.

    Es lo que hace util a la prueba de mTLS: un certificado bien formado pero firmado por
    otra autoridad tiene que ser rechazado igual que si no hubiera ninguno. Probar solo
    "sin certificado" dejaria sin comprobar que la CA se valida de verdad.
    """
    import sys

    raiz = Path(__file__).resolve().parents[1]
    if str(raiz / "scripts") not in sys.path:
        sys.path.insert(0, str(raiz / "scripts"))

    from gen_certs import main as generar

    destino = Path(tempfile.mkdtemp(prefix="dfsha-certs-intrusa-"))
    generar(["--out", str(destino)])
    return TlsMaterial(
        # Se presenta la CA BUENA para validar al servidor (si no, el fallo seria por
        # no reconocerlo a el, no por su rechazo a nosotros), pero el certificado de
        # cliente es de la otra.
        ca_cert=ca_de_pruebas() / "ca.crt",
        cert=destino / "client.crt",
        key=destino / "client.key",
    )
