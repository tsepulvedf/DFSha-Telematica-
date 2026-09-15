#!/usr/bin/env python
"""Genera el codigo Python del plano de control a partir de control.proto.

El .proto se versiona; lo generado no. La razon es que el codigo de grpcio-tools depende
de la version exacta de protobuf con la que se genero: versionarlo produce conflictos de
merge ilegibles y, peor, mezclas silenciosas entre el .proto y un stub viejo. Se regenera
en el CI y en los Dockerfiles, asi que siempre corresponde al .proto del commit.

    python scripts/gen_proto.py
    python scripts/gen_proto.py --check    # falla si falta o esta desactualizado
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
PROTO_DIR = RAIZ / "src" / "dfsha" / "common" / "proto"
PROTO = PROTO_DIR / "control.proto"
DESTINO = PROTO_DIR / "gen"

GENERADOS = ("control_pb2.py", "control_pb2_grpc.py", "control_pb2.pyi")


def generar() -> int:
    DESTINO.mkdir(parents=True, exist_ok=True)
    (DESTINO / "__init__.py").write_text(
        '"""Codigo generado por scripts/gen_proto.py. No editar ni versionar."""\n',
        encoding="utf-8",
    )

    resultado = subprocess.run(
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            f"--proto_path={PROTO_DIR}",
            f"--python_out={DESTINO}",
            f"--grpc_python_out={DESTINO}",
            f"--pyi_out={DESTINO}",
            str(PROTO),
        ],
        capture_output=True,
        text=True,
    )
    if resultado.returncode != 0:
        print(resultado.stdout, file=sys.stderr)
        print(resultado.stderr, file=sys.stderr)
        return resultado.returncode

    _arreglar_import_absoluto()
    print(f"generado en {DESTINO}")
    return 0


def _arreglar_import_absoluto() -> None:
    """protoc emite `import control_pb2` en el stub de gRPC.

    Ese import solo resuelve si el directorio generado esta en sys.path, cosa que no pasa
    dentro de un paquete. Se reescribe a relativo, que es el arreglo estandar y el motivo
    por el que casi todos los proyectos que usan grpcio-tools tienen un paso como este.
    """
    stub = DESTINO / "control_pb2_grpc.py"
    if not stub.exists():
        return
    texto = stub.read_text(encoding="utf-8")
    texto = texto.replace(
        "import control_pb2 as control__pb2",
        "from . import control_pb2 as control__pb2",
    )
    stub.write_text(texto, encoding="utf-8")


def comprobar() -> int:
    faltan = [n for n in GENERADOS if not (DESTINO / n).exists()]
    if faltan:
        print(
            "falta el codigo generado: " + ", ".join(faltan) + "\n"
            "corre: python scripts/gen_proto.py",
            file=sys.stderr,
        )
        return 1

    mas_nuevo = PROTO.stat().st_mtime
    viejos = [n for n in GENERADOS if (DESTINO / n).stat().st_mtime < mas_nuevo]
    if viejos:
        print(
            "el codigo generado es mas viejo que control.proto: " + ", ".join(viejos),
            file=sys.stderr,
        )
        return 1

    print("el codigo generado esta al dia")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="No genera: comprueba que existe y no esta desactualizado.",
    )
    args = parser.parse_args(argv)
    return comprobar() if args.check else generar()


if __name__ == "__main__":
    sys.exit(main())
