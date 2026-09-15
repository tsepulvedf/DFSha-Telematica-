"""RF2 de punta a punta: cliente real, ControlNode real, DataNode real, puertos reales."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import httpx
import pytest

from dfsha.client.api import ControlApi
from dfsha.client.session import Session
from dfsha.client.transfer import download_blocks, upload_blocks
from dfsha.common.checksum import sha256_bytes

from .cluster import MB, Cluster, start_cluster


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    c = start_cluster(tmp_path_factory.mktemp("dfsha"), block_size=MB)
    try:
        yield c
    finally:
        c.stop()


def nueva_sesion(cluster: Cluster, username: str) -> Session:
    sesion = Session(control_url=cluster.control_url)
    api = ControlApi(sesion)
    api.register(username, "contrasena-larga")
    token = api.login(username, "contrasena-larga")
    sesion.token = token.access_token
    sesion.username = username
    return sesion


@pytest.fixture()
def api(cluster: Cluster, request) -> ControlApi:
    return ControlApi(nueva_sesion(cluster, f"u{abs(hash(request.node.name)) % 10**9}"))


def generar(path: Path, size: int, semilla: int = 1234) -> str:
    """Contenido pseudoaleatorio reproducible. Devuelve su SHA-256."""
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


def subir(api: ControlApi, local: Path, remoto: str, parallel: int = 4):
    plan = api.create_file(remoto, local.stat().st_size)
    upload_blocks(local, plan.blocks, parallel=parallel)
    return plan, api.commit_file(plan.file_id)


def bajar(api: ControlApi, remoto: str, destino: Path, parallel: int = 4):
    plan = api.open_file(remoto)
    download_blocks(destino, plan.blocks, parallel=parallel)
    return plan


class TestRoundTrip:
    def test_50mb_en_bloques_de_1mb(self, cluster: Cluster, api: ControlApi, tmp_path: Path) -> None:
        origen = tmp_path / "grande.bin"
        sha_original = generar(origen, 50 * MB)

        plan, confirmado = subir(api, origen, "/grande.bin")

        assert len(plan.blocks) == 50
        assert confirmado.block_count == 50
        assert confirmado.size == 50 * MB
        assert confirmado.path == "/grande.bin"
        # Cada bloque fue a parar a un DataNode elegido y registrado por el ControlNode.
        assert all(len(b.replicas) == 1 for b in plan.blocks)

        destino = tmp_path / "bajado.bin"
        bajar(api, "/grande.bin", destino)

        assert destino.stat().st_size == 50 * MB
        assert sha256_de(destino) == sha_original

    @pytest.mark.parametrize("tamano", [0, 1, 1023, MB - 1, MB, MB + 1])
    def test_bordes_de_tamano(
        self, cluster: Cluster, api: ControlApi, tmp_path: Path, tamano: int
    ) -> None:
        origen = tmp_path / f"archivo-{tamano}.bin"
        sha_original = generar(origen, tamano, semilla=tamano + 1)

        _, confirmado = subir(api, origen, f"/archivo-{tamano}.bin")
        esperados = (tamano + MB - 1) // MB
        assert confirmado.block_count == esperados

        destino = tmp_path / f"bajado-{tamano}.bin"
        bajar(api, f"/archivo-{tamano}.bin", destino)
        assert destino.stat().st_size == tamano
        assert sha256_de(destino) == sha_original

    def test_archivo_vacio_no_crea_bloques(
        self, cluster: Cluster, api: ControlApi, tmp_path: Path
    ) -> None:
        origen = tmp_path / "vacio.bin"
        origen.write_bytes(b"")

        plan, confirmado = subir(api, origen, "/vacio.bin")
        assert plan.blocks == []
        assert confirmado.block_count == 0

        destino = tmp_path / "vacio-bajado.bin"
        bajar(api, "/vacio.bin", destino)
        assert destino.read_bytes() == b""

    def test_descarga_en_paralelo_y_en_serie_dan_lo_mismo(
        self, cluster: Cluster, api: ControlApi, tmp_path: Path
    ) -> None:
        origen = tmp_path / "paralelo.bin"
        sha_original = generar(origen, 5 * MB, semilla=77)
        subir(api, origen, "/paralelo.bin", parallel=4)

        for parallel in (1, 8):
            destino = tmp_path / f"p{parallel}.bin"
            bajar(api, "/paralelo.bin", destino, parallel=parallel)
            assert sha256_de(destino) == sha_original


class TestIntegridad:
    def test_un_bloque_con_checksum_malo_se_rechaza_y_el_commit_falla(
        self, cluster: Cluster, api: ControlApi
    ) -> None:
        plan = api.create_file("/corrupto.bin", 10)
        bloque = plan.blocks[0]
        destino = bloque.replicas[0].base_url

        # El checksum anunciado no corresponde a los bytes: el DataNode lo detecta.
        respuesta = httpx.put(
            f"{destino}/api/v1/blocks/{bloque.block_id}",
            content=b"0123456789",
            headers={"X-DFSha-Checksum": "0" * 64},
            timeout=30,
        )
        assert respuesta.status_code == 422
        assert respuesta.json()["code"] == "checksum_mismatch"

        # Y como no se almaceno, el commit no puede confirmar el archivo.
        with pytest.raises(Exception) as excinfo:
            api.commit_file(plan.file_id)
        assert "blocks_not_stored" in str(excinfo.value) or "faltan bloques" in str(excinfo.value)

    def test_falta_la_cabecera_de_checksum(self, cluster: Cluster, api: ControlApi) -> None:
        plan = api.create_file("/sin-checksum.bin", 10)
        bloque = plan.blocks[0]
        respuesta = httpx.put(
            f"{bloque.replicas[0].base_url}/api/v1/blocks/{bloque.block_id}",
            content=b"0123456789",
            timeout=30,
        )
        assert respuesta.status_code == 422

    def test_los_bloques_son_inmutables(self, cluster: Cluster, api: ControlApi) -> None:
        datos = b"0123456789"
        plan = api.create_file("/inmutable.bin", len(datos))
        bloque = plan.blocks[0]
        url = f"{bloque.replicas[0].base_url}/api/v1/blocks/{bloque.block_id}"
        cabeceras = {"X-DFSha-Checksum": sha256_bytes(datos)}

        assert httpx.put(url, content=datos, headers=cabeceras, timeout=30).status_code == 201
        segunda = httpx.put(url, content=datos, headers=cabeceras, timeout=30)
        assert segunda.status_code == 409
        assert segunda.json()["code"] == "block_already_exists"

    def test_el_datanode_devuelve_el_checksum_en_la_descarga(
        self, cluster: Cluster, api: ControlApi, tmp_path: Path
    ) -> None:
        origen = tmp_path / "con-checksum.bin"
        generar(origen, 2 * MB, semilla=5)
        subir(api, origen, "/con-checksum.bin")

        plan = api.open_file("/con-checksum.bin")
        bloque = plan.blocks[0]
        respuesta = httpx.get(
            f"{bloque.replicas[0].base_url}/api/v1/blocks/{bloque.block_id}", timeout=30
        )
        assert respuesta.headers["X-DFSha-Checksum"] == bloque.checksum_sha256
        assert sha256_bytes(respuesta.content) == bloque.checksum_sha256


class TestVisibilidadYAislamiento:
    def test_un_archivo_a_medio_subir_no_se_ve(self, cluster: Cluster, api: ControlApi) -> None:
        api.create_file("/a-medias.bin", 10)
        assert [e.name for e in api.ls("/").entries if e.name == "a-medias.bin"] == []
        with pytest.raises(Exception):
            api.open_file("/a-medias.bin")

    def test_abort_libera_el_nombre(self, cluster: Cluster, api: ControlApi) -> None:
        plan = api.create_file("/abortado.bin", 10)
        api.abort_file(plan.file_id)
        # Se puede volver a reservar la misma ruta inmediatamente.
        api.create_file("/abortado.bin", 10)

    def test_dos_usuarios_no_se_ven_los_archivos(
        self, cluster: Cluster, tmp_path: Path
    ) -> None:
        ana = ControlApi(nueva_sesion(cluster, "ana-aislamiento"))
        beto = ControlApi(nueva_sesion(cluster, "beto-aislamiento"))

        origen = tmp_path / "privado.bin"
        generar(origen, 2 * MB, semilla=9)
        subir(ana, origen, "/privado.bin")

        assert [e.name for e in ana.ls("/").entries] == ["privado.bin"]
        assert beto.ls("/").entries == []
        with pytest.raises(Exception):
            beto.open_file("/privado.bin")
        with pytest.raises(Exception):
            beto.stat("/privado.bin")

    def test_copy_on_write_sobre_una_ruta_ocupada(
        self, cluster: Cluster, api: ControlApi, tmp_path: Path
    ) -> None:
        primero = tmp_path / "v1.bin"
        segundo = tmp_path / "v2.bin"
        generar(primero, 2 * MB, semilla=1)
        sha_segundo = generar(segundo, 3 * MB, semilla=2)

        plan1, _ = subir(api, primero, "/version.bin")
        plan2, confirmado = subir(api, segundo, "/version.bin")

        # Bloques nuevos, no sobrescritos: WORM.
        assert set(b.block_id for b in plan1.blocks).isdisjoint(
            b.block_id for b in plan2.blocks
        )
        assert confirmado.block_count == 3

        destino = tmp_path / "version-bajada.bin"
        bajar(api, "/version.bin", destino)
        assert sha256_de(destino) == sha_segundo
        # Una sola entrada visible en la ruta.
        assert [e.name for e in api.ls("/").entries].count("version.bin") == 1


class TestMv:
    def test_mv_no_toca_el_datanode(
        self, cluster: Cluster, api: ControlApi, tmp_path: Path
    ) -> None:
        origen = tmp_path / "mover.bin"
        sha_original = generar(origen, 5 * MB, semilla=42)
        subir(api, origen, "/mover.bin")

        antes = api.open_file("/mover.bin")
        salud_antes = cluster.health()

        api.mkdir("/destino")
        api.mv("/mover.bin", "/destino")

        despues = api.open_file("/destino/mover.bin")
        salud_despues = cluster.health()

        # Los mismos block_id, en el mismo orden, en el mismo nodo.
        assert [b.block_id for b in despues.blocks] == [b.block_id for b in antes.blocks]
        assert [b.checksum_sha256 for b in despues.blocks] == [
            b.checksum_sha256 for b in antes.blocks
        ]
        # Y el DataNode ni se entero: ni un byte mas ni un bloque mas.
        assert salud_despues["used_bytes"] == salud_antes["used_bytes"]
        assert salud_despues["block_count"] == salud_antes["block_count"]

        destino = tmp_path / "movido.bin"
        bajar(api, "/destino/mover.bin", destino)
        assert sha256_de(destino) == sha_original


class TestSalud:
    def test_health_refleja_el_disco(self, cluster: Cluster, api: ControlApi, tmp_path: Path) -> None:
        antes = cluster.health()

        origen = tmp_path / "salud.bin"
        generar(origen, 4 * MB, semilla=3)
        subir(api, origen, "/salud.bin")

        despues = cluster.health()
        assert despues["block_count"] == antes["block_count"] + 4
        assert despues["used_bytes"] == antes["used_bytes"] + 4 * MB
        assert despues["disk_free_bytes"] > 0
        assert despues["capacity_bytes"] == 512 * MB
        assert despues["data_node_id"] not in (None, "sin-registrar")


class TestReservaVencida:
    def test_el_commit_de_una_reserva_vencida_es_410(self, tmp_path_factory) -> None:
        # TTL de un segundo: se crea la reserva, se espera, y el commit ya no vale.
        cluster = start_cluster(
            tmp_path_factory.mktemp("ttl"), block_size=MB, write_ttl_seconds=1
        )
        try:
            api = ControlApi(nueva_sesion(cluster, "ana-ttl"))
            plan = api.create_file("/vencido.bin", 10)
            time.sleep(1.2)

            with pytest.raises(Exception) as excinfo:
                api.commit_file(plan.file_id)
            assert "reservation_expired" in str(excinfo.value) or "vencio" in str(excinfo.value)

            # Y la ruta vuelve a estar libre: la reserva muerta no bloquea el nombre.
            nuevo = api.create_file("/vencido.bin", 10)
            assert nuevo.file_id != plan.file_id
        finally:
            cluster.stop()
