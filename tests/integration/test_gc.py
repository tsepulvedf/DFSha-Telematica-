"""Ciclo completo del recolector: put, rm, GC, y comprobar disco y metadato."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

from dfsha.client.api import ControlApi
from dfsha.client.session import Session
from dfsha.client.transfer import upload_blocks

from .cluster import MB, Cluster, start_cluster


def _cargar_gc():
    """Carga scripts/gc.py por ruta.

    Un `import gc` normal traeria el recolector de basura de Python, que ya esta
    importado y gana en sys.modules. El script se llama asi porque asi lo pide la
    especificacion, de modo que aqui se carga por ruta explicita.
    """
    ruta = Path(__file__).resolve().parents[2] / "scripts" / "gc.py"
    spec = importlib.util.spec_from_file_location("dfsha_gc_script", ruta)
    modulo = importlib.util.module_from_spec(spec)
    # Registrarlo antes de ejecutarlo: el @dataclass del script busca su propio modulo
    # en sys.modules mientras se define.
    sys.modules[spec.name] = modulo
    spec.loader.exec_module(modulo)
    return modulo


gc_script = _cargar_gc()


@pytest.fixture()
def cluster(tmp_path_factory):
    c = start_cluster(tmp_path_factory.mktemp("gc"), block_size=MB)
    try:
        yield c
    finally:
        c.stop()


def abrir_sesion(cluster: Cluster, username: str = "ana") -> ControlApi:
    sesion = Session(control_url=cluster.control_url)
    api = ControlApi(sesion)
    api.register(username, "contrasena-larga")
    sesion.token = api.login(username, "contrasena-larga").access_token
    return api


@pytest.fixture()
def api(cluster: Cluster) -> ControlApi:
    return abrir_sesion(cluster)


def generar(path: Path, size: int, semilla: int = 5) -> None:
    import random

    rng = random.Random(semilla)
    path.write_bytes(rng.randbytes(size))


def subir(api: ControlApi, local: Path, remoto: str):
    plan = api.create_file(remoto, local.stat().st_size)
    upload_blocks(local, plan.blocks, parallel=4)
    api.commit_file(plan.file_id)
    return plan


def blk_en_disco(cluster: Cluster) -> set[str]:
    return {p.stem for p in (cluster.data_dir / "blocks").rglob("*.blk")}


def huerfanos(cluster: Cluster) -> list[dict]:
    respuesta = httpx.get(
        f"{cluster.control_url}/internal/v1/gc/orphan-blocks",
        headers=cluster.internal_headers,
        timeout=30,
    )
    respuesta.raise_for_status()
    return respuesta.json()["blocks"]


def correr_gc(cluster: Cluster, dry_run: bool = False):
    return gc_script.recolectar(cluster.control_url, cluster.internal_secret, dry_run=dry_run)


class TestCicloCompleto:
    def test_put_rm_gc_deja_el_disco_como_estaba(
        self, cluster: Cluster, api: ControlApi, tmp_path: Path
    ) -> None:
        salud_inicial = cluster.health()

        origen = tmp_path / "borrable.bin"
        generar(origen, 4 * MB)
        plan = subir(api, origen, "/borrable.bin")
        ids = {b.block_id for b in plan.blocks}

        # Los bloques estan en disco y no son huerfanos todavia.
        assert ids <= blk_en_disco(cluster)
        assert huerfanos(cluster) == []
        assert cluster.health()["used_bytes"] == salud_inicial["used_bytes"] + 4 * MB

        api.rm("/borrable.bin")

        # rm es borrado logico: el archivo ya no se ve, pero los bytes siguen ahi.
        assert [e.name for e in api.ls("/").entries] == []
        assert ids <= blk_en_disco(cluster)
        assert {b["block_id"] for b in huerfanos(cluster)} == ids

        resumen = correr_gc(cluster)

        assert resumen.bloques_vistos == 4
        assert resumen.bloques_borrados == 4
        assert resumen.bytes_liberados == 4 * MB
        assert resumen.fallos == []

        # Los .blk desaparecieron del disco del DataNode...
        assert not (ids & blk_en_disco(cluster))
        # ...las filas se borraron del metadato...
        assert huerfanos(cluster) == []
        # ...y used_bytes volvio al valor previo, que es lo que la politica de
        # colocacion de la Etapa 2 va a leer.
        assert cluster.health()["used_bytes"] == salud_inicial["used_bytes"]
        assert cluster.health()["block_count"] == salud_inicial["block_count"]

    def test_dry_run_no_toca_nada(
        self, cluster: Cluster, api: ControlApi, tmp_path: Path
    ) -> None:
        origen = tmp_path / "intacto.bin"
        generar(origen, 2 * MB)
        plan = subir(api, origen, "/intacto.bin")
        ids = {b.block_id for b in plan.blocks}
        api.rm("/intacto.bin")

        resumen = correr_gc(cluster, dry_run=True)

        assert resumen.bloques_vistos == 2
        assert resumen.bloques_borrados == 0
        assert ids <= blk_en_disco(cluster)
        assert len(huerfanos(cluster)) == 2

    def test_no_recoge_lo_que_sigue_vivo(
        self, cluster: Cluster, api: ControlApi, tmp_path: Path
    ) -> None:
        vivo = tmp_path / "vivo.bin"
        muerto = tmp_path / "muerto.bin"
        generar(vivo, 2 * MB, semilla=1)
        generar(muerto, 2 * MB, semilla=2)

        plan_vivo = subir(api, vivo, "/vivo.bin")
        subir(api, muerto, "/muerto.bin")
        api.rm("/muerto.bin")

        correr_gc(cluster)

        # El archivo que sigue vivo conserva todos sus bloques y se puede seguir leyendo.
        assert {b.block_id for b in plan_vivo.blocks} <= blk_en_disco(cluster)
        assert len(api.open_file("/vivo.bin").blocks) == 2

    def test_gc_sin_huerfanos(self, cluster: Cluster, api: ControlApi) -> None:
        resumen = correr_gc(cluster)
        assert (resumen.bloques_vistos, resumen.bloques_borrados) == (0, 0)


class TestReservasVencidas:
    def test_recoge_los_bloques_de_una_reserva_abandonada(
        self, tmp_path_factory, tmp_path: Path
    ) -> None:
        import time

        # TTL de escritura de un segundo, solo para esta prueba: con un TTL corto
        # aplicado a todas, las subidas normales venderian antes de poder confirmarse.
        cluster = start_cluster(
            tmp_path_factory.mktemp("gc-ttl"), block_size=MB, write_ttl_seconds=1
        )
        try:
            self._reserva_abandonada(cluster, tmp_path)
        finally:
            cluster.stop()

    def _reserva_abandonada(self, cluster: Cluster, tmp_path: Path) -> None:
        import time

        api = abrir_sesion(cluster, "ana-ttl")

        # Un cliente que sube bloques y se cae antes del commit.
        origen = tmp_path / "abandonado.bin"
        generar(origen, 2 * MB)
        plan = api.create_file("/abandonado.bin", origen.stat().st_size)
        upload_blocks(origen, plan.blocks, parallel=2)
        ids = {b.block_id for b in plan.blocks}
        assert ids <= blk_en_disco(cluster)

        time.sleep(1.2)  # el TTL de escritura de este cluster es de 1 segundo

        assert {b["block_id"] for b in huerfanos(cluster)} == ids

        resumen = correr_gc(cluster)
        assert resumen.bloques_borrados == 2
        assert not (ids & blk_en_disco(cluster))

    def test_recoge_la_version_vieja_tras_un_copy_on_write(
        self, cluster: Cluster, api: ControlApi, tmp_path: Path
    ) -> None:
        primero = tmp_path / "v1.bin"
        segundo = tmp_path / "v2.bin"
        generar(primero, 2 * MB, semilla=1)
        generar(segundo, 2 * MB, semilla=2)

        plan1 = subir(api, primero, "/version.bin")
        plan2 = subir(api, segundo, "/version.bin")
        viejos = {b.block_id for b in plan1.blocks}
        nuevos = {b.block_id for b in plan2.blocks}

        # Sobrescribir dejo huerfanos los bloques de la version anterior.
        assert {b["block_id"] for b in huerfanos(cluster)} == viejos

        correr_gc(cluster)

        assert not (viejos & blk_en_disco(cluster))
        assert nuevos <= blk_en_disco(cluster)
        assert len(api.open_file("/version.bin").blocks) == 2


class TestSeguridad:
    def test_el_gc_exige_el_secreto_interno(self, cluster: Cluster) -> None:
        respuesta = httpx.get(
            f"{cluster.control_url}/internal/v1/gc/orphan-blocks",
            headers={"X-DFSha-Internal-Secret": "equivocado"},
            timeout=10,
        )
        assert respuesta.status_code == 401
