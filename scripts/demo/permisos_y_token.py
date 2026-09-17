"""Demostracion de las ACLs y de que alcanzan a los BYTES, no solo al metadato.

    python scripts/demo/permisos_y_token.py

Ana comparte un directorio con Beto, Beto lo ve y lo usa, y **un tercero no puede leer el
bloque ni yendo directo al DataNode con el `block_id` en la mano**.

## Por que hace falta ensenar las dos mitades

Un sistema que responde 403 a todo el mundo pasaria la mitad negativa de esta
demostracion con nota. Y un sistema sin ninguna ACL pasaria la positiva. Solo las dos
juntas dicen algo:

| Comprobacion | Que descarta |
|---|---|
| Ana **si** lee su bloque con su token | que el DataNode rechace a todo el mundo |
| Beto **si** lee lo que Ana le compartio | que compartir sea una fila en una tabla sin efecto |
| Carla **no** lee el bloque de Ana, con el id | que las ACLs solo cubran el metadato |
| El token de Carla **no** abre el bloque de Ana | que baste con tener UN token valido |

La tercera fila es la que el token de bloque hizo posible. Antes, el ControlNode resolvia
permisos sobre rutas y el DataNode servia `GET /blocks/{id}` a cualquiera: **el permiso se
comprobaba en un sitio por el que los datos no pasan**.
"""

from __future__ import annotations

import hashlib
import sys

import httpx

from _comun import CONTROL_URL, TRABAJO, Demo, exigir_docker

from dfsha.client.api import ControlApi
from dfsha.client.session import Session
from dfsha.common.blocktoken import BLOCK_TOKEN_HEADER

CLAVE = "contrasena-de-la-demo"
SECRETO = b"informe confidencial de Ana, no deberia leerlo un tercero\n" * 4


def usuario(nombre: str) -> ControlApi:
    """Registra (o reusa) un usuario y devuelve su cliente."""
    sesion = Session(control_url=CONTROL_URL)
    api = ControlApi(sesion)
    try:
        api.register(nombre, CLAVE)
    except Exception:  # noqa: BLE001 - ya existia de una pasada anterior
        pass
    sesion.token = api.login(nombre, CLAVE).access_token
    sesion.username = nombre
    return api


def subir(api: ControlApi, ruta: str, datos: bytes):
    """Sube un archivo de un bloque y devuelve el bloque del plan."""
    plan = api.create_file(ruta, len(datos))
    bloque = plan.blocks[0]
    httpx.put(
        f"{bloque.replicas[0].base_url}/api/v1/blocks/{bloque.block_id}",
        content=datos,
        headers={
            "X-DFSha-Checksum": hashlib.sha256(datos).hexdigest(),
            BLOCK_TOKEN_HEADER: bloque.token,
        },
        timeout=60,
    )
    api.commit_file(plan.file_id)
    return bloque


def pedir_bloque(bloque, token: str | None) -> httpx.Response:
    return httpx.get(
        f"{bloque.replicas[0].base_url}/api/v1/blocks/{bloque.block_id}",
        headers=({BLOCK_TOKEN_HEADER: token} if token else {}),
        timeout=60,
    )


def main() -> int:
    demo = Demo(
        "ACLs con grupos, y permisos que alcanzan a los bytes",
        "el DataNode no conoce rutas ni usuarios: el permiso viaja FIRMADO",
    )
    exigir_docker(demo)
    TRABAJO.mkdir(exist_ok=True)

    sufijo = "demo"
    ana = usuario(f"ana-{sufijo}")
    beto = usuario(f"beto-{sufijo}")
    carla = usuario(f"carla-{sufijo}")
    demo.nota("tres usuarios: Ana (duena), Beto (invitado), Carla (ajena)")

    # --- 1. Ana sube algo suyo ---------------------------------------------
    demo.titulo("Ana crea un directorio y sube un archivo")
    try:
        ana.mkdir("/proyecto", parents=True)
    except Exception:  # noqa: BLE001
        pass
    bloque_de_ana = subir(ana, "/proyecto/secreto.txt", SECRETO)
    demo.ok(f"subido, bloque {bloque_de_ana.block_id[:8]}...")

    # --- 2. CONTROL POSITIVO: Ana si puede leerlo --------------------------
    demo.titulo("CONTROL POSITIVO: Ana lee su propio bloque")
    demo.nota(
        "sin esto, un DataNode que rechazara a TODO el mundo pasaria el resto de la "
        "demostracion con nota"
    )
    propia = ana.open_file("/proyecto/secreto.txt").blocks[0]
    r = pedir_bloque(propia, propia.token)
    demo.afirmar(
        r.status_code == 200 and r.content == SECRETO,
        f"HTTP {r.status_code}, y los bytes son los suyos",
        f"HTTP {r.status_code}: la duena NO puede leer su propio bloque",
    )

    # --- 3. Sin compartir, Beto no ve nada ---------------------------------
    demo.titulo("Beto todavia no tiene acceso")
    try:
        beto.open_file("/proyecto/secreto.txt")
        demo.mal("Beto pudo abrir un archivo que no le han compartido")
    except Exception as exc:  # noqa: BLE001
        demo.ok(f"el ControlNode lo niega: {getattr(exc, 'code', type(exc).__name__)}")
    demo.nota(
        "responde 404 y no 403 a proposito: decir «existe pero no es tuyo» convierte "
        "el arbol de otro usuario en algo enumerable probando nombres"
    )

    # --- 4. Ana comparte con un grupo --------------------------------------
    demo.titulo("Ana crea un grupo, mete a Beto, y comparte con el grupo")
    try:
        ana.create_group("equipo")
        ana.add_member("equipo", beto.session.username)
    except Exception:  # noqa: BLE001
        pass
    ana.share("/proyecto", "equipo", "read")
    demo.ok("concedido READ sobre /proyecto al grupo 'equipo'")

    # --- 5. CONTROL POSITIVO: compartir SI da acceso a los bytes -----------
    demo.titulo("CONTROL POSITIVO: Beto lee los bytes, no solo el nombre")
    demo.nota(
        "si solo viera el archivo en `ls` pero no pudiera bajarlo, compartir seria "
        "una fila en una tabla sin efecto sobre los datos"
    )
    ruta_beto = f"/compartido-conmigo/{ana.session.username}/proyecto/secreto.txt"
    de_beto = beto.open_file(ruta_beto).blocks[0]
    r = pedir_bloque(de_beto, de_beto.token)
    demo.afirmar(
        r.status_code == 200 and r.content == SECRETO,
        "Beto baja el contenido con el token que le dio el ControlNode",
        f"HTTP {r.status_code}: compartir no llego hasta los bytes",
    )

    # --- 6. EL AGUJERO QUE EL TOKEN CIERRA ---------------------------------
    demo.titulo("Carla conoce el block_id y va DIRECTA al DataNode")
    demo.nota(
        "se salta el ControlNode entero. Antes del token de bloque, esto devolvia "
        "los bytes: el permiso se comprobaba en un sitio por el que los datos no pasan"
    )
    print(f"    {bloque_de_ana.replicas[0].base_url}/api/v1/blocks/{bloque_de_ana.block_id}")
    r = pedir_bloque(bloque_de_ana, None)
    demo.afirmar(
        r.status_code == 403 and SECRETO not in r.content,
        f"HTTP {r.status_code}: sin token no hay bytes",
        f"HTTP {r.status_code}: el bloque se sirvio SIN autorizacion",
    )

    # --- 7. Y con un token suyo, tampoco -----------------------------------
    demo.titulo("Carla lo intenta con un token legitimo... de SU propio archivo")
    demo.nota(
        "es la version fina del ataque: el token lo firmo el ControlNode y es de "
        "lectura. Lo que lo rechaza es que el block_id va FIRMADO dentro"
    )
    subir(carla, "/suyo.txt", b"cosas de Carla\n")
    token_de_carla = carla.open_file("/suyo.txt").blocks[0].token
    r = pedir_bloque(bloque_de_ana, token_de_carla)
    demo.afirmar(
        r.status_code == 403,
        f"HTTP {r.status_code}: un token vale para SU bloque y para ninguno mas",
        f"HTTP {r.status_code}: un token valido abrio un bloque ajeno",
    )

    # --- 8. Revocar ---------------------------------------------------------
    demo.titulo("Ana retira el permiso")
    ana.unshare("/proyecto", "equipo")
    try:
        beto.open_file(ruta_beto)
        demo.mal("Beto sigue pudiendo abrir el archivo tras la revocacion")
    except Exception as exc:  # noqa: BLE001
        demo.ok(f"Beto deja de poder abrirlo: {getattr(exc, 'code', 'denegado')}")
    demo.nota(
        "esto corta la EMISION de tokens nuevos. Un token ya emitido sigue valiendo "
        "hasta que caduque (10 min): no hay revocacion, y esta escrito como limite "
        "conocido en CLAUDE.md"
    )

    ana.rm("/proyecto/secreto.txt")
    carla.rm("/suyo.txt")
    return demo.terminar(
        """
        El permiso lo resuelve el ControlNode sobre rutas.
        Los bytes viajan directos al DataNode, que no sabe de rutas ni de usuarios.
        El token firmado es lo que hace que la decision llegue hasta donde estan los datos.
        """
    )


if __name__ == "__main__":
    sys.exit(main())
