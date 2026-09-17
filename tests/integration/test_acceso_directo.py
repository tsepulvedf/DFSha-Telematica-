"""Las ACLs alcanzan a los BYTES, no solo al metadato.

Este fichero existe porque hasta el token de bloque las ACLs del Bloque C eran ciertas a
medias, y la mitad que faltaba era la que importa.

El ControlNode resuelve permisos sobre **rutas**. Pero los bytes van directos entre
cliente y DataNode —decision 4 de la seccion 1, la que hace que anadir nodos sume ancho de
banda— y el DataNode **no sabe nada de rutas ni de usuarios**: almacena por `block_id`.
Asi que el unico sitio donde se comprobaba el permiso no estaba en el camino de los datos,
y `GET /blocks/{id}` servia a cualquiera.

«Un UUID no se adivina» no es una respuesta. Un `block_id` aparece en todo plan que el
ControlNode haya entregado alguna vez, en los logs, y en el metadato de un archivo que se
dejo de compartir: el dia que a alguien se le retira el acceso, los ids que ya vio siguen
siendo validos para siempre. Seguridad por desconocimiento del identificador es justo lo
que un sistema con ACLs explicitas no deberia necesitar.

Lo que se prueba aqui es el escenario completo: Beto **conoce** el `block_id` de un
archivo de Ana y aun asi no puede tocarlo.
"""

from __future__ import annotations

import hashlib

import httpx
import pytest

from dfsha.client.api import ControlApi
from dfsha.client.session import Session
from dfsha.common.blocktoken import BLOCK_TOKEN_HEADER

from .cluster import MB, Cluster, start_cluster


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    c = start_cluster(tmp_path_factory.mktemp("acceso"), block_size=MB, data_nodes=1)
    try:
        yield c
    finally:
        c.stop()


def _usuario(cluster: Cluster, nombre: str) -> ControlApi:
    sesion = Session(control_url=cluster.control_url)
    api = ControlApi(sesion)
    api.register(nombre, "contrasena-de-prueba")
    sesion.token = api.login(nombre, "contrasena-de-prueba").access_token
    sesion.username = nombre
    return api


@pytest.fixture()
def ana(cluster, request) -> ControlApi:
    return _usuario(cluster, f"ana-{request.node.name[:22]}")


@pytest.fixture()
def beto(cluster, request) -> ControlApi:
    return _usuario(cluster, f"beto-{request.node.name[:22]}")


DATOS = b"contenido privado de Ana" * 8


@pytest.fixture()
def nombre_de():
    """El nombre de usuario con el que se registro una sesion."""
    return lambda api: api.session.username


def _subir_privado(api: ControlApi, ruta: str):
    """Sube un archivo y devuelve su unico bloque, con el token que Ana recibio."""
    plan = api.create_file(ruta, len(DATOS))
    bloque = plan.blocks[0]
    respuesta = httpx.put(
        f"{bloque.replicas[0].base_url}/api/v1/blocks/{bloque.block_id}",
        content=DATOS,
        headers={
            "X-DFSha-Checksum": hashlib.sha256(DATOS).hexdigest(),
            BLOCK_TOKEN_HEADER: bloque.token,
        },
        timeout=30,
    )
    assert respuesta.status_code == 201
    api.commit_file(plan.file_id)
    return bloque


# --- El caso bueno primero. Si este falla, los demas no prueban nada --------


def test_el_dueno_SI_puede_bajar_su_bloque(ana, tmp_path) -> None:
    """La mitad que se olvida. Un DataNode que rechazara todo pasaria las pruebas de
    rechazo y el sistema estaria roto."""
    _subir_privado(ana, "/propio.bin")
    bloque = ana.open_file("/propio.bin").blocks[0]

    respuesta = httpx.get(
        f"{bloque.replicas[0].base_url}/api/v1/blocks/{bloque.block_id}",
        headers={BLOCK_TOKEN_HEADER: bloque.token},
        timeout=30,
    )

    assert respuesta.status_code == 200
    assert respuesta.content == DATOS


# --- El agujero que el token cierra ----------------------------------------


def test_un_extrano_con_el_block_id_NO_puede_bajar_el_bloque(ana, beto) -> None:
    """El escenario completo, y el motivo de todo este fichero.

    Beto conoce el `block_id` —da igual como: un log, un plan viejo, un archivo que se
    dejo de compartir— y va directo al DataNode saltandose el ControlNode entero. Antes
    del token, esto devolvia los bytes.
    """
    bloque = _subir_privado(ana, "/secreto.bin")

    # Que no pueda ni abrirlo por la via normal es lo que ya hacia la ACL.
    with pytest.raises(Exception) as sin_permiso:
        beto.open_file("/secreto.bin")
    assert getattr(sin_permiso.value, "code", "") in ("not_found", "forbidden")

    # Y esto es lo nuevo: por la via directa tampoco.
    respuesta = httpx.get(
        f"{bloque.replicas[0].base_url}/api/v1/blocks/{bloque.block_id}", timeout=30
    )

    assert respuesta.status_code == 403
    assert DATOS not in respuesta.content


def test_un_extrano_NO_puede_borrar_el_bloque(ana, beto) -> None:
    """Peor que leer. Sin token, `DELETE /blocks/{id}` destruia datos ajenos, y ademas de
    forma silenciosa: el ControlNode se enteraria por un report como si fuera un disco
    que fallo."""
    bloque = _subir_privado(ana, "/borrable.bin")

    respuesta = httpx.delete(
        f"{bloque.replicas[0].base_url}/api/v1/blocks/{bloque.block_id}", timeout=30
    )

    assert respuesta.status_code == 403
    # Y sigue ahi, que es la comprobacion que de verdad importa.
    vivo = ana.open_file("/borrable.bin").blocks[0]
    assert (
        httpx.get(
            f"{vivo.replicas[0].base_url}/api/v1/blocks/{vivo.block_id}",
            headers={BLOCK_TOKEN_HEADER: vivo.token},
            timeout=30,
        ).content
        == DATOS
    )


def test_el_token_de_Beto_no_abre_el_bloque_de_Ana(ana, beto) -> None:
    """La version fina del ataque, y la que justifica que el `block_id` vaya FIRMADO
    dentro del token y no solo en la URL.

    Beto tiene un token legitimo: es de su propio archivo. Si el DataNode se limitara a
    comprobar «este token lo firmo el ControlNode», bastaria con presentarlo pidiendo otro
    bloque.
    """
    ajeno = _subir_privado(ana, "/de-ana.bin")
    propio = _subir_privado(beto, "/de-beto.bin")

    respuesta = httpx.get(
        f"{ajeno.replicas[0].base_url}/api/v1/blocks/{ajeno.block_id}",
        headers={BLOCK_TOKEN_HEADER: propio.token},  # valido, pero para OTRO bloque
        timeout=30,
    )

    assert respuesta.status_code == 403


def test_un_token_de_lectura_no_sirve_para_borrar(ana) -> None:
    """Escalada de operacion. Ana puede leer su bloque; eso no la autoriza a borrarlo por
    la via directa, que es la que se salta el borrado logico y el GC."""
    _subir_privado(ana, "/solo-lectura.bin")
    bloque = ana.open_file("/solo-lectura.bin").blocks[0]

    respuesta = httpx.delete(
        f"{bloque.replicas[0].base_url}/api/v1/blocks/{bloque.block_id}",
        headers={BLOCK_TOKEN_HEADER: bloque.token},  # token de LECTURA
        timeout=30,
    )

    assert respuesta.status_code == 403


def test_borrar_un_bloque_inexistente_tambien_exige_token(ana) -> None:
    """La idempotencia va DESPUES de la autorizacion.

    Si el 204 de «no estaba» llegara antes de comprobar el token, el endpoint seria un
    oraculo: 204 si el bloque no existe, 403 si existe. Con eso se puede enumerar el
    contenido de un disco sin autorizacion ninguna.
    """
    bloque = _subir_privado(ana, "/oraculo.bin")
    base = bloque.replicas[0].base_url

    existe = httpx.delete(f"{base}/api/v1/blocks/{bloque.block_id}", timeout=30)
    no_existe = httpx.delete(
        f"{base}/api/v1/blocks/00000000-0000-4000-8000-000000000000", timeout=30
    )

    assert existe.status_code == no_existe.status_code == 403


# --- Que el mecanismo no rompe lo que ya funcionaba ------------------------


def test_compartir_da_acceso_a_los_BYTES_y_no_solo_al_nombre(
    ana, beto, nombre_de
) -> None:
    """La otra direccion, y la que evita que esto sea un candado sin llave.

    Cuando el permiso SI existe, `open_file` emite el token y Beto baja los bytes. Es lo
    que convierte a `share` en algo mas que una fila en una tabla: el permiso viaja hasta
    el DataNode, que es donde estan los datos.
    """
    ana.mkdir("/publico", parents=True)
    _subir_privado(ana, "/publico/compartido.bin")
    ana.share("/publico", nombre_de(beto), "read")

    ruta = f"/compartido-conmigo/{nombre_de(ana)}/publico/compartido.bin"
    bloque = beto.open_file(ruta).blocks[0]

    assert bloque.token, "el plan de un usuario autorizado tiene que traer token"
    respuesta = httpx.get(
        f"{bloque.replicas[0].base_url}/api/v1/blocks/{bloque.block_id}",
        headers={BLOCK_TOKEN_HEADER: bloque.token},
        timeout=30,
    )

    assert respuesta.status_code == 200
    assert respuesta.content == DATOS


def test_al_retirar_el_permiso_deja_de_emitirse_token(ana, beto, nombre_de) -> None:
    """El limite honesto del mecanismo, escrito como prueba.

    Retirar el acceso corta la EMISION de tokens nuevos, no invalida los ya emitidos: no
    hay revocacion. Un token que Beto guardara sigue sirviendo hasta que caduque, y por
    eso la vida por defecto es corta. Es el compromiso clasico de una credencial sin
    estado, y conviene que este fijado como comportamiento esperado y no descubierto en
    la sustentacion.
    """
    ana.mkdir("/temporal", parents=True)
    _subir_privado(ana, "/temporal/dato.bin")
    ana.share("/temporal", nombre_de(beto), "read")
    ruta = f"/compartido-conmigo/{nombre_de(ana)}/temporal/dato.bin"
    assert beto.open_file(ruta).blocks[0].token

    ana.unshare("/temporal", nombre_de(beto))

    with pytest.raises(Exception):
        beto.open_file(ruta)
