#!/usr/bin/env python3
"""Genera la CA de DFSha y un certificado por rol.

    python scripts/gen_certs.py                  # a ./certs
    python scripts/gen_certs.py --out /etc/dfsha --hosts control-node,lb,10.0.1.7

Sustituye a `DFSHA_INTERNAL_SECRET`. Un secreto compartido por cabecera protege contra
quien no lo conoce, pero no dice **quien** esta al otro lado: cualquiera que lo tenga es
todos a la vez. Con mTLS cada rol presenta su propio certificado, el otro extremo lo
valida contra la CA, y revocar a uno no obliga a rotar el secreto de todos.

## Lo que genera

    certs/
      ca.crt        la CA. La tienen TODOS para validar a los demas
      ca.key        la clave de la CA. NO se copia a ningun servicio
      control.crt   control.key    ControlNode
      data.crt      data.key       DataNodes (uno para todos, ver abajo)
      client.crt    client.key     cliente y script del GC

**Nada de esto se versiona.** `.gitignore` cubre `*.crt`, `*.key` y `certs/`. Lo que se
versiona es este script y el procedimiento.

## Un certificado para todos los DataNodes

Un certificado por nodo seria mas correcto y permitiria revocar uno solo. No se hace, y
conviene saber por que: el `data_node_id` lo asigna el ControlNode en el registro, asi
que no existe todavia cuando habria que emitir el certificado, y usar la URL anunciada
ataria el certificado a la topologia de red. El limite reconocido es ese: **los DataNodes
se autentican como rol, no como individuos**. Quien robe el certificado de un DataNode
puede hacerse pasar por otro DataNode; lo que no puede es hacerse pasar por el
ControlNode ni por un cliente.

## Por que la CA es propia

Una CA publica no firmaria `control-node` ni `10.0.1.7`. Y el objetivo aqui no es que un
navegador confie: es que **los nodos confien entre si y solo entre si**. Una CA propia con
su clave fuera de los servicios es mas restrictiva que una publica, no menos.
"""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import sys
from pathlib import Path

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
except ImportError:  # pragma: no cover - mensaje de arranque
    print(
        "falta 'cryptography'. Instala el proyecto con sus dependencias:\n"
        '    pip install -e "."',
        file=sys.stderr,
    )
    raise SystemExit(2)

#: 10 anos para la CA y 2 para los servicios. No es un despliegue real con rotacion
#: automatica; que caduquen a mitad de la evaluacion seria un fallo absurdo.
CA_DIAS = 3650
SERVICIO_DIAS = 730

#: 2048 y no 4096: el handshake de cada conexion gRPC lo paga el t3.micro, y 2048 sigue
#: siendo el minimo recomendado. Es una decision de coste, no un descuido.
BITS = 2048

ORGANIZACION = "DFSha"


def _clave() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=BITS)


def _escribir_clave(clave: rsa.RSAPrivateKey, destino: Path) -> None:
    destino.write_bytes(
        clave.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    # Solo su dueno. En Windows chmod hace poco, pero en Linux y macOS —que es donde se
    # despliega— evita dejar una clave privada legible por todo el sistema.
    try:
        destino.chmod(0o600)
    except OSError:  # pragma: no cover - sistemas sin permisos POSIX
        pass


def _escribir_cert(cert: x509.Certificate, destino: Path) -> None:
    destino.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def crear_ca(out: Path) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    clave = _clave()
    nombre = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, ORGANIZACION),
            x509.NameAttribute(NameOID.COMMON_NAME, "DFSha Root CA"),
        ]
    )
    ahora = dt.datetime.now(dt.timezone.utc)

    cert = (
        x509.CertificateBuilder()
        .subject_name(nombre)
        .issuer_name(nombre)  # autofirmada
        .public_key(clave.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(ahora - dt.timedelta(minutes=5))  # margen de reloj
        .not_valid_after(ahora + dt.timedelta(days=CA_DIAS))
        # `path_length=0`: esta CA firma certificados de servicio y NADA MAS. Sin esto,
        # un certificado filtrado podria firmar otros y emitir identidades nuevas.
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(clave, hashes.SHA256())
    )

    _escribir_cert(cert, out / "ca.crt")
    _escribir_clave(clave, out / "ca.key")
    return cert, clave


def emitir(
    out: Path,
    nombre_fichero: str,
    common_name: str,
    hosts: list[str],
    ca_cert: x509.Certificate,
    ca_key: rsa.RSAPrivateKey,
) -> None:
    """Emite un certificado de servicio firmado por la CA.

    Lleva `serverAuth` **y** `clientAuth` a proposito: en este sistema los mismos
    procesos actuan de las dos formas. El ControlNode es servidor del DataNode por gRPC y
    cliente de nadie; el DataNode es servidor del cliente y cliente del ControlNode; y en
    el pipeline un DataNode es cliente de otro DataNode. Separar los usos obligaria a dos
    certificados por nodo sin ganar nada, porque el conjunto de identidades es el mismo.
    """
    clave = _clave()
    ahora = dt.datetime.now(dt.timezone.utc)

    alternativos: list[x509.GeneralName] = []
    for host in hosts:
        try:
            alternativos.append(x509.IPAddress(ipaddress.ip_address(host)))
        except ValueError:
            alternativos.append(x509.DNSName(host))

    cert = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name(
                [
                    x509.NameAttribute(NameOID.ORGANIZATION_NAME, ORGANIZACION),
                    x509.NameAttribute(NameOID.COMMON_NAME, common_name),
                ]
            )
        )
        .issuer_name(ca_cert.subject)
        .public_key(clave.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(ahora - dt.timedelta(minutes=5))
        .not_valid_after(ahora + dt.timedelta(days=SERVICIO_DIAS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName(alternativos), critical=False)
        .add_extension(
            x509.ExtendedKeyUsage(
                [
                    x509.oid.ExtendedKeyUsageOID.SERVER_AUTH,
                    x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH,
                ]
            ),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=True,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )

    _escribir_cert(cert, out / f"{nombre_fichero}.crt")
    _escribir_clave(clave, out / f"{nombre_fichero}.key")


#: Nombres por los que cada rol es alcanzable. `localhost` y `127.0.0.1` para el
#: desarrollo y las pruebas; los nombres de servicio para la red de compose. En AWS hay
#: que anadir las IP privadas con `--hosts`.
HOSTS_POR_DEFECTO = {
    "control": [
        "localhost",
        "127.0.0.1",
        "control-node",
        "control-node-1",
        "control-node-2",
        "control-node-3",
        "lb",
        "migrate",
    ],
    "data": [
        "localhost",
        "127.0.0.1",
        "data-node-1",
        "data-node-2",
        "data-node-3",
        "data-node-4",
    ],
    "client": ["localhost", "127.0.0.1"],
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out", default="certs", help="Directorio destino (por defecto ./certs)."
    )
    parser.add_argument(
        "--hosts",
        default="",
        help=(
            "Nombres o IP ADICIONALES para todos los certificados, separados por comas. "
            "En AWS hay que anadir aqui las IP privadas de las instancias."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenera aunque ya existan. OJO: invalida los certificados en uso.",
    )
    args = parser.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    if (out / "ca.crt").exists() and not args.force:
        print(
            f"ya hay una CA en {out}/ca.crt.\n"
            "\n"
            "Regenerarla invalida TODOS los certificados emitidos con ella, y los\n"
            "servicios que sigan usando los viejos dejaran de poder hablar entre si.\n"
            "Si es lo que quieres, repite con --force.",
            file=sys.stderr,
        )
        return 1

    extra = [h.strip() for h in args.hosts.split(",") if h.strip()]

    ca_cert, ca_key = crear_ca(out)
    for rol, hosts in HOSTS_POR_DEFECTO.items():
        emitir(out, rol, f"dfsha-{rol}", hosts + extra, ca_cert, ca_key)

    print(f"CA y certificados en {out.resolve()}")
    print("  ca.crt        la tienen todos, para validarse entre si")
    print("  ca.key        NO se copia a ningun servicio: solo firma")
    print("  control.*     ControlNode")
    print("  data.*        DataNodes")
    print("  client.*      cliente y script del GC")
    if extra:
        print(f"  nombres extra: {', '.join(extra)}")
    print()
    print("Nada de esto se versiona: .gitignore cubre *.crt, *.key y certs/.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
