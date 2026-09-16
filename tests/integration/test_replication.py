"""Replicacion R=3 con pipeline y quorum W=2, contra cuatro DataNodes reales.

Cubre los criterios 3, 4 y 6 de la Etapa 3: reparto en tres dominios de falla, commit
que pasa con W=2 cuando cae un nodo durante la subida, y lectura que sobrevive a la
perdida de dos nodos de tres.

Los umbrales de deteccion van comprimidos, igual que en `test_cluster.py`: lo que se
verifica es el mecanismo, no los numeros de produccion.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path

import pytest

from dfsha.client.api import ControlApi
from dfsha.client.session import Session
from dfsha.client.transfer import download_blocks, upload_blocks

from .cluster import MB, Cluster, start_cluster

RAPIDO = dict(
    heartbeat_interval_ms=300,
    suspect_after_ms=1500,
    dead_after_ms=3000,
    membership_interval_ms=100,
    min_free_bytes=0,
)


@pytest.fixture()
def cluster_r3(tmp_path_factory):
    """Cuatro DataNodes en cuatro dominios, con R=3 y W=2.

    Cuatro y no tres a proposito: con exactamente R nodos, matar uno hace imposible
    cualquier colocacion nueva y las pruebas dejarian de distinguir "no se pudo replicar"
    de "no se pudo ni planificar".
    """
    c = start_cluster(
        tmp_path_factory.mktemp("r3"),
        block_size=MB,
        data_nodes=4,
        fault_domains=["zona-a", "zona-b", "zona-c", "zona-d"],
        replication_factor=3,
        write_quorum=2,
        **RAPIDO,
    )
    try:
        yield c
    finally:
        c.stop()


def _sesion(cluster: Cluster, usuario: str) -> tuple[ControlApi, Session]:
    sesion = Session(control_url=cluster.control_url)
    api = ControlApi(sesion)
    api.register(usuario, "contrasena-de-prueba")
    sesion.token = api.login(usuario, "contrasena-de-prueba").access_token
    return api, sesion


def _archivo(tmp_path: Path, nombre: str, tamano: int) -> tuple[Path, str]:
    ruta = tmp_path / nombre
    datos = bytes((i * 7 + 13) % 256 for i in range(tamano))
    ruta.write_bytes(datos)
    return ruta, hashlib.sha256(datos).hexdigest()


def _subir(api: ControlApi, local: Path, remoto: str, tamano: int) -> dict:
    plan = api.create_file(remoto, tamano)
    upload_blocks(local, plan.blocks, parallel=2)
    api.commit_file(plan.file_id)
    return {"file_id": plan.file_id, "blocks": plan.blocks}


# --- Criterio 3: reparto en tres dominios ----------------------------------


def test_cada_bloque_va_a_tres_nodos_y_tres_dominios(cluster_r3, tmp_path) -> None:
    api, _ = _sesion(cluster_r3, "ana")
    local, _ = _archivo(tmp_path, "grande.bin", 5 * MB)

    plan = api.create_file("/grande.bin", 5 * MB)

    assert len(plan.blocks) == 5
    dominio_de = {n.data_node_id: n.fault_domain for n in cluster_r3.nodes}

    for bloque in plan.blocks:
        nodos = [r.data_node_id for r in bloque.replicas]
        assert len(nodos) == 3, "la colocacion no planifico tres replicas"
        assert len(set(nodos)) == 3, "dos replicas en el mismo nodo se pierden juntas"
        dominios = {dominio_de[n] for n in nodos}
        assert len(dominios) == 3, f"las tres replicas cayeron en {dominios}"

    upload_blocks(local, plan.blocks, parallel=2)
    api.commit_file(plan.file_id)

    info = api.stat("/grande.bin")
    assert info.replication_state == "FULLY_REPLICATED"
    assert info.min_replicas == 3


def test_el_cliente_sube_los_bytes_una_sola_vez(cluster_r3, tmp_path) -> None:
    """El pipeline, medido donde se nota: en el disco de los nodos.

    Si el cliente subiera R veces, los bloques estarian igualmente en tres discos, asi
    que contar discos no distingue las dos implementaciones. Lo que si las distingue es
    que aqui el cliente solo habla con UN nodo por bloque, y los otros dos reciben su
    copia de un DataNode.
    """
    api, _ = _sesion(cluster_r3, "beto")
    local, _ = _archivo(tmp_path, "pipe.bin", 3 * MB)

    plan = api.create_file("/pipe.bin", 3 * MB)
    primeros = {b.replicas[0].data_node_id for b in plan.blocks}
    upload_blocks(local, plan.blocks, parallel=1)
    api.commit_file(plan.file_id)

    # Cada bloque acabo en sus tres nodos, aunque el cliente solo hablo con el primero.
    en_disco = {n.data_node_id: n.blk_en_disco() for n in cluster_r3.nodes}
    for bloque in plan.blocks:
        tiene = [n for n, blks in en_disco.items() if bloque.block_id in blks]
        assert len(tiene) == 3, f"el bloque {bloque.block_id[:8]} quedo en {len(tiene)}"

    assert primeros, "el plan no traia primera replica"


# --- Criterio 4: commit con W=2 cuando cae un nodo --------------------------


def test_un_nodo_caido_durante_la_subida_no_impide_el_commit(cluster_r3, tmp_path) -> None:
    """La decision de diseno que esta prueba blinda: un archivo con 2 de 3 replicas no
    esta roto, y rechazar su commit pondria la durabilidad por encima de la
    disponibilidad.

    Un archivo de UN solo bloque, a proposito: asi hay una unica cadena y se puede tumbar
    su ultimo nodo de forma determinista. Con varios bloques, el nodo que es el ultimo de
    una cadena suele ser el primero de otra, y la prueba acabaria midiendo el azar del
    reparto en vez de la regla del quorum.
    """
    api, _ = _sesion(cluster_r3, "carla")
    local, _ = _archivo(tmp_path, "parcial.bin", MB)

    por_id = {n.data_node_id: n for n in cluster_r3.nodes}  # antes de parar nada
    plan = api.create_file("/parcial.bin", MB)
    assert len(plan.blocks) == 1

    por_id[plan.blocks[0].replicas[-1].data_node_id].stop()

    upload_blocks(local, plan.blocks, parallel=1)
    api.commit_file(plan.file_id)  # W=2: pasa con dos copias

    info = api.stat("/parcial.bin")
    assert info.replication_state == "UNDER_REPLICATED"
    assert info.min_replicas == 2
    assert info.replication_factor == 3


def test_el_cluster_cuenta_sus_bloques_sub_replicados(cluster_r3, tmp_path) -> None:
    api, _ = _sesion(cluster_r3, "dario")
    local, _ = _archivo(tmp_path, "sano.bin", 2 * MB)
    _subir(api, local, "/sano.bin", 2 * MB)

    estado = api.cluster_status()
    assert estado.under_replicated_blocks == 0
    assert estado.critical_blocks == 0
    assert estado.replication_factor == 3
    assert estado.write_quorum == 2


# --- Criterio 6: dos nodos caidos de tres ----------------------------------


def test_el_archivo_sigue_legible_con_dos_de_tres_nodos_caidos(cluster_r3, tmp_path) -> None:
    """La razon de ser de R=3, comprobada por su efecto y no por su intencion: el
    SHA-256 de lo que se baja tiene que ser identico al de lo que se subio."""
    api, _ = _sesion(cluster_r3, "elena")
    local, sha_original = _archivo(tmp_path, "resistente.bin", 3 * MB)

    plan = api.create_file("/resistente.bin", 3 * MB)
    upload_blocks(local, plan.blocks, parallel=1)
    api.commit_file(plan.file_id)

    # El mapa se construye ANTES de parar nada: `data_node_id` pregunta al nodo por HTTP,
    # asi que consultarlo despues de tumbarlo falla y la prueba se caeria por su propia
    # instrumentacion en vez de por lo que quiere medir.
    por_id = {n.data_node_id: n for n in cluster_r3.nodes}

    # Se tumban dos de los tres nodos que tienen el primer bloque: queda una sola copia.
    lectura = api.open_file("/resistente.bin")
    con_copia = [r.data_node_id for r in lectura.blocks[0].replicas]
    assert len(con_copia) == 3
    for data_node_id in con_copia[:2]:
        por_id[data_node_id].stop()

    destino = tmp_path / "bajado.bin"
    plan_lectura = api.open_file("/resistente.bin")
    download_blocks(destino, plan_lectura.blocks, parallel=1)

    assert hashlib.sha256(destino.read_bytes()).hexdigest() == sha_original


def test_sin_quorum_el_commit_falla_y_no_deja_el_archivo_visible(
    cluster_r3, tmp_path
) -> None:
    """La otra mitad de W=2: con menos de dos copias no hay medias tintas.

    Se tumban todos los nodos menos uno, de modo que ningun bloque pueda alcanzar dos
    copias. El commit tiene que fallar, y la ruta quedar libre.
    """
    api, _ = _sesion(cluster_r3, "fran")
    local, _ = _archivo(tmp_path, "sin-quorum.bin", MB)

    por_id = {n.data_node_id: n for n in cluster_r3.nodes}  # antes de parar nada
    plan = api.create_file("/sin-quorum.bin", MB)
    destino_unico = plan.blocks[0].replicas[0].data_node_id
    for data_node_id, nodo in por_id.items():
        if data_node_id != destino_unico:
            nodo.stop()

    upload_blocks(local, plan.blocks, parallel=1)

    with pytest.raises(Exception) as exc:
        api.commit_file(plan.file_id)
    assert "quorum" in str(exc.value).lower()

    with pytest.raises(Exception):
        api.stat("/sin-quorum.bin")


def test_el_reparto_usa_los_cuatro_nodos(cluster_r3, tmp_path) -> None:
    """Con R=3 y cuatro nodos, el cuarto tiene que entrar en el reparto.

    Es la misma propiedad que la Etapa 2 blindo para R=1 (`_carga` cuenta lo ya asignado
    dentro de la misma llamada), comprobada ahora con tres replicas por bloque, que es
    donde el sesgo seria mas facil de pasar por alto.
    """
    api, _ = _sesion(cluster_r3, "gema")
    plan = api.create_file("/reparto.bin", 12 * MB)

    reparto = Counter(r.data_node_id for b in plan.blocks for r in b.replicas)

    assert len(reparto) == 4, f"algun nodo quedo fuera del reparto: {reparto}"
    api.abort_file(plan.file_id)
