"""Criterios de aceptacion de la Etapa 2, con cuatro DataNodes reales.

Los umbrales de deteccion van comprimidos (SUSPECT a 1,5 s y DEAD a 3 s, con latidos
cada 300 ms) para que la suite no tarde minutos. Los valores de produccion —10 s y 30 s—
son los de `.env.example` y el compose, y su aritmetica esta cubierta por las unitarias
de `test_membership.py`, que no dependen del reloj real. Lo que estas pruebas verifican
es el mecanismo de punta a punta, no los numeros concretos.
"""

from __future__ import annotations

import hashlib
import time
from collections import Counter
from pathlib import Path

import pytest

from dfsha.client.api import ControlApi
from dfsha.client.session import Session
from dfsha.client.transfer import download_blocks, upload_blocks

from .cluster import MB, Cluster, start_cluster

# Deteccion comprimida: ver el docstring del modulo.
RAPIDO = dict(
    heartbeat_interval_ms=300,
    suspect_after_ms=1500,
    dead_after_ms=3000,
    membership_interval_ms=100,
    min_free_bytes=0,
)


@pytest.fixture(scope="module")
def cuatro_nodos(tmp_path_factory):
    """Cuatro DataNodes en cuatro dominios, como el compose local."""
    c = start_cluster(
        tmp_path_factory.mktemp("cuatro"),
        block_size=MB,
        data_nodes=4,
        fault_domains=["local-1", "local-2", "local-3", "local-4"],
        **RAPIDO,
    )
    try:
        yield c
    finally:
        c.stop()


def sesion(cluster: Cluster, username: str) -> ControlApi:
    s = Session(control_url=cluster.control_url)
    api = ControlApi(s)
    api.register(username, "contrasena-larga")
    s.token = api.login(username, "contrasena-larga").access_token
    return api


def generar(path: Path, size: int, semilla: int = 1) -> str:
    import random

    rng = random.Random(semilla)
    digest = hashlib.sha256()
    with open(path, "wb") as fh:
        restante = size
        while restante > 0:
            trozo = rng.randbytes(min(MB, restante))
            digest.update(trozo)
            fh.write(trozo)
            restante -= len(trozo)
    return digest.hexdigest()


def sha256_de(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while trozo := fh.read(MB):
            digest.update(trozo)
    return digest.hexdigest()


def subir(api: ControlApi, local: Path, remoto: str):
    plan = api.create_file(remoto, local.stat().st_size)
    upload_blocks(local, plan.blocks, parallel=4)
    api.commit_file(plan.file_id)
    return plan


def reparto(plan) -> Counter:
    """Cuantos bloques fueron a cada DataNode, segun el plan que dio el ControlNode."""
    return Counter(b.replicas[0].data_node_id for b in plan.blocks)


def esperar_estado(cluster: Cluster, token: str, node_id: str, estado: str, timeout: float):
    limite = time.time() + timeout
    visto = None
    while time.time() < limite:
        for n in cluster.cluster_status(token)["nodes"]:
            if n["data_node_id"] == node_id:
                visto = n["state"]
                if visto == estado:
                    return n
        time.sleep(0.1)
    raise AssertionError(f"el nodo no llego a {estado} en {timeout}s (visto: {visto})")


# --- 1 ---------------------------------------------------------------------


class TestRegistroDeCuatroNodos:
    def test_los_cuatro_aparecen_vivos_con_su_dominio(self, cuatro_nodos: Cluster) -> None:
        api = sesion(cuatro_nodos, "ana-registro")
        estado = api.cluster_status()

        assert len(estado.nodes) == 4
        assert {n.state for n in estado.nodes} == {"ALIVE"}
        assert {n.fault_domain for n in estado.nodes} == {
            "local-1",
            "local-2",
            "local-3",
            "local-4",
        }
        # Cada uno se anuncia con su propia URL, que es la que el cliente usara.
        assert len({n.advertise_url for n in estado.nodes}) == 4
        for nodo in estado.nodes:
            assert nodo.seconds_since_heartbeat is not None
            assert nodo.disk_free_bytes > 0


# --- 2 y 8 -----------------------------------------------------------------


class TestRepartoDeBloques:
    def test_50_bloques_repartidos_entre_los_cuatro(
        self, cuatro_nodos: Cluster, tmp_path: Path
    ) -> None:
        api = sesion(cuatro_nodos, "ana-reparto")
        origen = tmp_path / "grande.bin"
        sha = generar(origen, 50 * MB, semilla=50)

        plan = subir(api, origen, "/grande.bin")
        distribucion = reparto(plan)

        assert len(plan.blocks) == 50
        assert len(distribucion) == 4, f"algun nodo se quedo sin bloques: {distribucion}"
        mayor = max(distribucion.values())
        assert mayor <= 30, f"un nodo se lleva {mayor}/50 bloques: {distribucion}"

        # Y el round-trip de la Etapa 1 sigue funcionando con los bloques repartidos
        # entre cuatro nodos (criterio 8).
        destino = tmp_path / "bajado.bin"
        lectura = api.open_file("/grande.bin")
        download_blocks(destino, lectura.blocks, parallel=4)
        assert sha256_de(destino) == sha

    def test_los_bloques_estan_de_verdad_en_los_discos_que_dice_el_plan(
        self, cuatro_nodos: Cluster, tmp_path: Path
    ) -> None:
        api = sesion(cuatro_nodos, "ana-discos")
        origen = tmp_path / "comprobar.bin"
        generar(origen, 8 * MB, semilla=8)

        plan = subir(api, origen, "/comprobar.bin")
        por_nodo: dict[str, set[str]] = {}
        for bloque in plan.blocks:
            por_nodo.setdefault(bloque.replicas[0].data_node_id, set()).add(bloque.block_id)

        for nodo in cuatro_nodos.nodes:
            esperados = por_nodo.get(nodo.data_node_id, set())
            if esperados:
                assert esperados <= nodo.blk_en_disco()


# --- 3 ---------------------------------------------------------------------


class TestNodoCasiLleno:
    def test_un_nodo_con_menos_capacidad_recibe_bastante_menos(
        self, tmp_path_factory, tmp_path: Path
    ) -> None:
        """La capacidad del cuarto nodo es una fraccion de la de los demas, asi que su
        ocupacion relativa sube mucho mas rapido con cada bloque."""
        cluster = start_cluster(
            tmp_path_factory.mktemp("lleno"),
            block_size=MB,
            data_nodes=4,
            capacities=[512 * MB, 512 * MB, 512 * MB, 40 * MB],
            **RAPIDO,
        )
        try:
            api = sesion(cluster, "ana-lleno")
            pequeno = cluster.nodes[3].data_node_id

            origen = tmp_path / "carga.bin"
            generar(origen, 40 * MB, semilla=40)
            plan = subir(api, origen, "/carga.bin")
            distribucion = reparto(plan)

            recibidos = distribucion.get(pequeno, 0)
            media_resto = (len(plan.blocks) - recibidos) / 3
            assert recibidos < media_resto, (
                f"el nodo pequeno recibio {recibidos} y la media del resto es "
                f"{media_resto:.1f}: {distribucion}"
            )
        finally:
            cluster.stop()


# --- 4, 5 y 6 --------------------------------------------------------------


class TestCaidaYReincorporacion:
    def test_ciclo_completo_de_caida(self, tmp_path_factory, tmp_path: Path) -> None:
        """Un nodo que se cae: SUSPECT, DEAD, deja de recibir bloques, su archivo deja de
        poder leerse con un error que lo nombra, y vuelve."""
        cluster = start_cluster(
            tmp_path_factory.mktemp("caida"),
            block_size=MB,
            data_nodes=4,
            **RAPIDO,
        )
        try:
            api = sesion(cluster, "ana-caida")
            token = api.session.token

            # Un archivo que vive entero en el nodo que vamos a matar.
            victima = cluster.nodes[0]
            victima_id = victima.data_node_id

            origen = tmp_path / "en-la-victima.bin"
            sha = generar(origen, 2 * MB, semilla=2)
            plan = subir(api, origen, "/repartido.bin")

            # --- 4: SUSPECT y luego DEAD -------------------------------------
            victima.stop()

            sospechoso = esperar_estado(cluster, token, victima_id, "SUSPECT", timeout=8)
            assert sospechoso["seconds_since_heartbeat"] >= 1.0

            muerto = esperar_estado(cluster, token, victima_id, "DEAD", timeout=10)
            assert muerto["state"] == "DEAD"

            # --- 4: un put posterior no le asigna nada -----------------------
            otro = tmp_path / "despues.bin"
            generar(otro, 6 * MB, semilla=6)
            plan_posterior = subir(api, otro, "/despues.bin")
            assert victima_id not in reparto(plan_posterior)

            # --- 5: el get falla nombrando bloque y nodo ---------------------
            bloques_en_victima = [
                b.block_id for b in plan.blocks if b.replicas[0].data_node_id == victima_id
            ]
            if bloques_en_victima:
                with pytest.raises(Exception) as excinfo:
                    api.open_file("/repartido.bin")
                mensaje = str(excinfo.value)
                assert any(b in mensaje for b in bloques_en_victima), mensaje
                assert victima_id in mensaje, mensaje

            # --- 6: vuelve con el mismo boot_id ------------------------------
            victima.start()
            esperar_estado(cluster, token, victima_id, "ALIVE", timeout=15)

            # Tras el report completo del arranque, sus replicas vuelven a servirse.
            limite = time.time() + 15
            while time.time() < limite:
                try:
                    lectura = api.open_file("/repartido.bin")
                    break
                except Exception:
                    time.sleep(0.3)
            else:
                raise AssertionError("el archivo no volvio a ser legible tras reincorporar")

            destino = tmp_path / "recuperado.bin"
            download_blocks(destino, lectura.blocks, parallel=4)
            assert sha256_de(destino) == sha
        finally:
            cluster.stop()


# --- 7 ---------------------------------------------------------------------


class TestDivergencia:
    def test_un_blk_borrado_a_mano_acaba_en_missing(
        self, tmp_path_factory, tmp_path: Path
    ) -> None:
        """El ControlNode no borra nada: solo anota que la replica ya no esta."""
        cluster = start_cluster(
            tmp_path_factory.mktemp("divergencia"),
            block_size=MB,
            data_nodes=2,
            # Report completo en cada latido, para no esperar veinte.
            full_report_every_n=1,
            **RAPIDO,
        )
        try:
            api = sesion(cluster, "ana-divergencia")
            origen = tmp_path / "victima.bin"
            generar(origen, 4 * MB, semilla=4)
            plan = subir(api, origen, "/victima.bin")

            objetivo = plan.blocks[0]
            nodo = next(
                n
                for n in cluster.nodes
                if n.data_node_id == objetivo.replicas[0].data_node_id
            )

            # Se borra el .blk por debajo del sistema, como haria un fallo de disco.
            ruta = next(nodo.data_dir.rglob(f"{objetivo.block_id}.blk"))
            ruta.unlink()
            assert objetivo.block_id not in nodo.blk_en_disco()

            # Tras el siguiente report completo, la replica queda MISSING y el archivo
            # deja de poder abrirse, con un error que nombra el bloque.
            limite = time.time() + 20
            while time.time() < limite:
                try:
                    api.open_file("/victima.bin")
                except Exception as exc:
                    if objetivo.block_id in str(exc):
                        break
                time.sleep(0.3)
            else:
                raise AssertionError("la replica borrada nunca se marco como MISSING")

            # Y el resto de bloques del archivo siguen en su sitio: el ControlNode no
            # borro nada por su cuenta.
            for bloque in plan.blocks[1:]:
                duenyo = next(
                    n for n in cluster.nodes if n.data_node_id == bloque.replicas[0].data_node_id
                )
                assert bloque.block_id in duenyo.blk_en_disco()
        finally:
            cluster.stop()
