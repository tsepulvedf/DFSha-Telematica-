"""Todo el codigo del repositorio compila, lo importe una prueba o no.

## Por que existe

**El punto de entrada del ControlNode y el del DataNode no compilaban, con 529 pruebas en
verde.** Un f-string sin cerrar en `__main__.py`, que es lo PRIMERO que ejecuta el
contenedor. Los tres ControlNodes entraron en bucle de reinicio al levantar el stack, y la
suite no tenia forma de saberlo: **ninguna prueba importaba esos modulos**. Las pruebas
arrancan la app llamando a `create_app()` directamente; el camino que usa Docker
—`python -m dfsha.control_node`— no lo recorria nadie.

Es la forma mas literal del patron de la Etapa 3: **el codigo que menos se ejercita es
justo el que decide si el servicio existe**.

Medido al escribir esto, antes del arreglo:

- de 82 modulos en `src/`, **2 no los cargaba ninguna prueba**: exactamente los dos
  `__main__.py`
- de 10 scripts en `scripts/`, **8 no los tocaba ninguna prueba**, incluidos los cuatro
  guiones de demostracion que se iban a grabar

## Que comprueba, y que NO

Dos niveles, y el segundo es el que cierra el hueco para siempre:

1. **Los dos `__main__` se IMPORTAN.** Mas fuerte que compilar: tambien atrapa un nombre
   mal escrito en un `import` o una referencia rota a nivel de modulo. Es seguro porque
   `main()` esta detras de `if __name__ == "__main__"`, asi que importar no arranca nada.
2. **Todo `.py` de `src/` y `scripts/` se COMPILA**, lo importe alguien o no. No los
   ejecuta —varios scripts tocan Docker o la red— pero un error de sintaxis ya no puede
   llegar hasta un contenedor ni hasta una grabacion.

Lo que no comprueba es que **funcionen**: un script que compila puede fallar al correr.
Eso sigue siendo cosa de las pruebas de cada uno y de la validacion en Docker. Esto es el
suelo, no el techo: garantiza que nada llega roto por algo que Python detecta sin
ejecutarlo.

## Por que se recorre el arbol y no una lista

Una lista de ficheros envejeceria exactamente como envejecio la cobertura: el modulo que
alguien anada dentro de seis meses no estaria en ella, y **no fallaria nada**. Recorriendo
el arbol, un fichero nuevo entra solo.
"""

from __future__ import annotations

import importlib
import py_compile
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[2]

#: Codigo generado desde el .proto: no se versiona y lo regenera el build.
EXCLUIDOS = ("proto/gen",)


def _ficheros() -> list[Path]:
    encontrados = []
    for carpeta in ("src", "scripts"):
        for f in sorted((RAIZ / carpeta).rglob("*.py")):
            ruta = f.relative_to(RAIZ).as_posix()
            if "__pycache__" in ruta or any(x in ruta for x in EXCLUIDOS):
                continue
            encontrados.append(f)
    return encontrados


FICHEROS = _ficheros()


def test_se_encuentran_ficheros_que_compilar() -> None:
    """El control positivo de esta misma prueba.

    Si el recorrido del arbol se rompiera —una ruta mal calculada, un patron que no casa—
    la prueba de abajo se parametrizaria con CERO ficheros y pasaria en verde sin
    comprobar nada. Es exactamente el fallo que este fichero existe para cazar, aplicado a
    si mismo.
    """
    rutas = {f.relative_to(RAIZ).as_posix() for f in FICHEROS}

    assert len(FICHEROS) > 80
    assert "src/dfsha/control_node/__main__.py" in rutas
    assert "src/dfsha/data_node/__main__.py" in rutas
    assert "scripts/demo/cifrado_en_reposo.py" in rutas


@pytest.mark.parametrize(
    "fichero", FICHEROS, ids=[f.relative_to(RAIZ).as_posix() for f in FICHEROS]
)
def test_compila(fichero: Path, tmp_path: Path) -> None:
    # doraise para que un error salga como excepcion y no como un aviso por stderr que
    # nadie lee. cfile a tmp_path para no ensuciar el arbol con .pyc.
    py_compile.compile(str(fichero), cfile=str(tmp_path / "x.pyc"), doraise=True)


@pytest.mark.parametrize("modulo", ["dfsha.control_node.__main__", "dfsha.data_node.__main__"])
def test_el_punto_de_entrada_se_importa(modulo: str) -> None:
    """Importar es mas que compilar: tambien atrapa un `import` que apunta a un nombre que
    no existe. Y es seguro, porque `main()` va detras de `if __name__ == "__main__"`.

    Se comprueba ademas que `main` exista y sea llamable: es lo que ejecuta
    `python -m`, y un refactor que la renombrara dejaria el contenedor arrancando un
    modulo que no hace nada.
    """
    mod = importlib.import_module(modulo)

    assert callable(getattr(mod, "main", None))


# --- Lo que destapo ejecutar el punto de entrada de verdad ------------------


def test_una_variable_VACIA_cuenta_como_no_puesta(monkeypatch, tmp_path) -> None:
    """El fallo que aparecio al arrancar el DataNode por `python -m`, y no compilando.

    `deploy/.env.datanode.example` recomienda dejar `DFSHA_DATANODE_CAPACITY_BYTES=`
    VACIA («se deduce del disco»), y el compose de AWS pasa el `.env` tal cual. Llegaba
    como la cadena "" a un campo `int | None` y **el DataNode no arrancaba siguiendo
    nuestras propias instrucciones**. En local no se veia porque `docker-compose.yml`
    le pone un default.

    Se comprueba con la variable del caso real y con otra numerica, porque el arreglo es
    general y conviene que la prueba lo diga.
    """
    from dfsha.data_node.config import DataNodeSettings

    from tests.certs import material

    m = material("data")
    monkeypatch.setenv("DFSHA_TLS_CA_CERT", str(m.ca_cert))
    monkeypatch.setenv("DFSHA_TLS_CERT", str(m.cert))
    monkeypatch.setenv("DFSHA_TLS_KEY", str(m.key))
    monkeypatch.setenv("DFSHA_DATANODE_CAPACITY_BYTES", "")
    monkeypatch.setenv("DFSHA_ORDER_WORKERS", "")

    ajustes = DataNodeSettings(_env_file=None, data_dir=str(tmp_path))

    assert ajustes.datanode_capacity_bytes is None  # vacia = se deduce del disco
    assert ajustes.order_workers == 2  # vacia = el default, no un error
