"""Colaboracion entre usuarios: los criterios 7 y 8 de la Etapa 3.

El 7, palabra por palabra: *Ana comparte `/compartido` con el grupo `equipo` con `write`,
Beto sube un archivo, Ana lo descarga, Ana baja el permiso a `read`, Beto ya no puede
subir.*

El 8: *`/compartido-conmigo` lista lo compartido y no contamina el arbol propio de Beto.*

Se hace de punta a punta contra el ControlNode real, con el cliente real, porque lo que
se quiere comprobar es que el permiso se aplica en **todos** los caminos —namespace,
subida, descarga— y no solo en el que se recordo proteger.
"""

from __future__ import annotations

import hashlib
from pathlib import Path as FsPath

import pytest

from dfsha.client.api import ControlApi
from dfsha.client.session import Session
from dfsha.client.transfer import download_blocks, upload_blocks

from .cluster import MB, Cluster, start_cluster


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    c = start_cluster(tmp_path_factory.mktemp("acl"), block_size=MB, data_nodes=1)
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
    return _usuario(cluster, f"ana-{request.node.name[:24]}")


@pytest.fixture()
def beto(cluster, request) -> ControlApi:
    return _usuario(cluster, f"beto-{request.node.name[:24]}")


def _archivo(tmp_path: FsPath, nombre: str, tamano: int = 2048) -> tuple[FsPath, str]:
    ruta = tmp_path / nombre
    datos = bytes((i * 13 + 7) % 256 for i in range(tamano))
    ruta.write_bytes(datos)
    return ruta, hashlib.sha256(datos).hexdigest()


def _subir(api: ControlApi, local: FsPath, remoto: str) -> None:
    tamano = local.stat().st_size
    plan = api.create_file(remoto, tamano)
    upload_blocks(local, plan.blocks, parallel=1)
    api.commit_file(plan.file_id)


def _bajar(api: ControlApi, remoto: str, destino: FsPath) -> str:
    plan = api.open_file(remoto)
    download_blocks(destino, plan.blocks, parallel=1)
    return hashlib.sha256(destino.read_bytes()).hexdigest()


def _nombre(api: ControlApi) -> str:
    return api.session.username or ""


# --- Aislamiento: lo que ya valia en las etapas anteriores ------------------


def test_sin_compartir_nada_beto_no_ve_el_arbol_de_ana(ana, beto) -> None:
    """El punto de partida. Si esto se rompiera, lo demas no significaria nada."""
    ana.mkdir("/privado")

    with pytest.raises(Exception) as exc:
        beto.ls("/privado")

    # 404 y no 403: decir «no tienes permiso» confirmaria que la ruta existe, y eso
    # convierte el arbol ajeno en algo enumerable a base de probar nombres.
    assert getattr(exc.value, "code", "") == "not_found"


# --- Criterio 7: el ciclo completo de colaboracion -------------------------


def test_criterio_7_colaboracion_por_grupo_y_revocacion(
    ana, beto, tmp_path: FsPath
) -> None:
    """El escenario del enunciado, entero y en orden."""
    ana.mkdir("/compartido")
    ana.create_group("equipo")
    ana.add_member("equipo", _nombre(beto))
    ana.share("/compartido", "equipo", "write")

    # Beto lo ve en su directorio virtual, con el permiso y el grupo por el que le llega.
    compartido = beto.shared_with_me()
    assert len(compartido.entries) == 1
    entrada = compartido.entries[0]
    assert entrada.owner == _nombre(ana)
    assert entrada.name == "compartido"
    assert entrada.permission == "WRITE"
    assert entrada.via_group == "equipo"

    ruta = entrada.path  # /compartido-conmigo/<ana>/compartido

    # Beto SUBE un archivo al directorio de Ana.
    local, sha = _archivo(tmp_path, "de-beto.bin")
    _subir(beto, local, f"{ruta}/de-beto.bin")

    # Y Ana lo descarga desde SU propia ruta: es su directorio, al fin y al cabo.
    destino = tmp_path / "bajado.bin"
    assert _bajar(ana, "/compartido/de-beto.bin", destino) == sha

    # Ana baja el permiso a lectura.
    ana.share("/compartido", "equipo", "read")

    # Beto sigue leyendo...
    assert [e.name for e in beto.ls(ruta).entries] == ["de-beto.bin"]
    assert _bajar(beto, f"{ruta}/de-beto.bin", tmp_path / "beto-baja.bin") == sha

    # ...pero ya no puede subir.
    otro, _ = _archivo(tmp_path, "otro.bin")
    with pytest.raises(Exception) as exc:
        _subir(beto, otro, f"{ruta}/otro.bin")
    assert getattr(exc.value, "code", "") == "not_authorized"


def test_bajar_el_permiso_no_acumula_concesiones(ana, beto) -> None:
    """La razon de que `grant` ACTUALICE en vez de insertar.

    Si se acumularan dos filas, el permiso efectivo —que es el maximo— seguiria siendo el
    alto, y bajar un permiso no bajaria nada. Se comprueba por su efecto: el permiso
    efectivo que ve Beto es el ultimo concedido, no el mayor de los dos.
    """
    ana.mkdir("/doc")
    ana.share("/doc", _nombre(beto), "admin")
    ana.share("/doc", _nombre(beto), "read")

    vista = beto.acl(f"/compartido-conmigo/{_nombre(ana)}/doc")
    assert vista.effective == "READ"


def test_revocar_deja_de_verse(ana, beto) -> None:
    ana.mkdir("/temporal")
    ana.share("/temporal", _nombre(beto), "read")
    assert len(beto.shared_with_me().entries) == 1

    ana.unshare("/temporal", _nombre(beto))

    assert beto.shared_with_me().entries == []
    with pytest.raises(Exception):
        beto.ls(f"/compartido-conmigo/{_nombre(ana)}/temporal")


def test_beto_no_puede_compartir_lo_que_le_compartieron_con_write(ana, beto) -> None:
    """Compartir exige ADMIN, no WRITE. Con WRITE se pueden meter archivos; repartir
    acceso es otra cosa, y dejarlo a quien solo puede escribir haria que una concesion
    se propagara sin que el dueno se entere."""
    ana.mkdir("/equipo-doc")
    ana.share("/equipo-doc", _nombre(beto), "write")

    with pytest.raises(Exception) as exc:
        beto.share(
            f"/compartido-conmigo/{_nombre(ana)}/equipo-doc", _nombre(ana), "read"
        )
    assert getattr(exc.value, "code", "") == "not_authorized"


def test_con_admin_si_puede_repartir(ana, beto, cluster) -> None:
    carla = _usuario(cluster, "carla-admin")
    ana.mkdir("/delegado")
    ana.share("/delegado", _nombre(beto), "admin")

    beto.share(f"/compartido-conmigo/{_nombre(ana)}/delegado", _nombre(carla), "read")

    assert [e.name for e in carla.shared_with_me().entries] == ["delegado"]


# --- Criterio 8: el arbol propio no se contamina ---------------------------


def test_criterio_8_lo_compartido_no_aparece_en_el_arbol_propio(ana, beto) -> None:
    """Lo ajeno esta en `/compartido-conmigo`, y en ningun otro sitio."""
    ana.mkdir("/informes")
    ana.share("/informes", _nombre(beto), "read")
    beto.mkdir("/mis-cosas")

    # La raiz de Beto solo tiene lo suyo.
    assert [e.name for e in beto.ls("/").entries] == ["mis-cosas"]

    # Y lo compartido esta en su sitio, agrupado por quien lo comparte.
    assert [e.name for e in beto.ls("/compartido-conmigo").entries] == [_nombre(ana)]
    assert [
        e.name for e in beto.ls(f"/compartido-conmigo/{_nombre(ana)}").entries
    ] == ["informes"]


def test_dos_personas_pueden_compartir_directorios_con_el_mismo_nombre(
    ana, beto, cluster, tmp_path: FsPath
) -> None:
    """La razon de que el usuario vaya delante en la ruta virtual.

    Sin ese nivel, el segundo `informes` taparia al primero o habria que renombrarlo.
    """
    carla = _usuario(cluster, "carla-colision")
    ana.mkdir("/informes")
    carla.mkdir("/informes")
    ana.share("/informes", _nombre(beto), "read")
    carla.share("/informes", _nombre(beto), "read")

    duenos = sorted(e.name for e in beto.ls("/compartido-conmigo").entries)
    assert duenos == sorted([_nombre(ana), _nombre(carla)])

    # Y las dos rutas son distintas y navegables.
    for dueno in duenos:
        assert beto.ls(f"/compartido-conmigo/{dueno}/informes").entries == []


# --- Herencia y origen del permiso -----------------------------------------


def test_el_permiso_se_hereda_hacia_abajo(ana, beto) -> None:
    ana.mkdir("/proyecto")
    ana.mkdir("/proyecto/docs")
    ana.mkdir("/proyecto/docs/2026")
    ana.share("/proyecto", _nombre(beto), "read")

    ruta = f"/compartido-conmigo/{_nombre(ana)}/proyecto/docs/2026"
    vista = beto.acl(ruta)

    assert vista.effective == "READ"
    assert vista.source == "user"
    # Y dice de donde lo hereda, que es lo que hace auditable un permiso.
    assert vista.inherited_from == "/proyecto"


def test_una_regla_mas_cercana_puede_conceder_menos(ana, beto) -> None:
    """La propiedad que da al modelo su unica forma de acotar el alcance sin
    denegaciones. Ana comparte todo `/area` con WRITE pero `/area/privado` solo con
    READ."""
    ana.mkdir("/area")
    ana.mkdir("/area/privado")
    ana.share("/area", _nombre(beto), "write")
    ana.share("/area/privado", _nombre(beto), "read")

    base = f"/compartido-conmigo/{_nombre(ana)}/area"
    assert beto.acl(base).effective == "WRITE"
    assert beto.acl(f"{base}/privado").effective == "READ"


def test_el_dueno_ve_quien_tiene_acceso(ana, beto) -> None:
    """`acl` responde dos preguntas distintas: que puedo hacer yo, y quien mas puede."""
    ana.mkdir("/auditado")
    ana.share("/auditado", _nombre(beto), "write")

    vista = ana.acl("/auditado")

    assert vista.effective == "ADMIN"
    assert vista.source == "owner"
    assert len(vista.grants) == 1
    assert vista.grants[0].principal == _nombre(beto)
    assert vista.grants[0].permission == "WRITE"
    assert vista.grants[0].principal_type == "USER"
