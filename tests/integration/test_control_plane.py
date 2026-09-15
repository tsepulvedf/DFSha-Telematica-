"""Plano de control contra un servidor gRPC de verdad.

Levanta la app real del ControlNode, que arranca su servidor gRPC en el lifespan, y le
habla con el stub generado. Nada de dobles: lo que se quiere comprobar es justo el
camino que un doble se saltaria.
"""

from __future__ import annotations

import time
from pathlib import Path as FsPath

import grpc
import pytest
from fastapi.testclient import TestClient

from dfsha.common.proto.gen import control_pb2, control_pb2_grpc
from dfsha.control_node.domain.entities import DataNodeState, ReplicaState
from dfsha.control_node.main import create_app
from dfsha.control_node.repositories.database import build_engine, build_session_factory
from dfsha.control_node.repositories.sql import SqlUnitOfWork

from .conftest import build_settings

MB = 1024 * 1024


@pytest.fixture()
def plano(tmp_path: FsPath):
    """ControlNode arrancado, con un stub gRPC apuntando a el."""
    settings = build_settings(
        tmp_path, suspect_after_ms=300, dead_after_ms=900, membership_interval_ms=50
    )
    with TestClient(create_app(settings)) as client:
        canal = grpc.insecure_channel(f"127.0.0.1:{settings.grpc_port}")
        grpc.channel_ready_future(canal).result(timeout=10)
        stub = control_pb2_grpc.ControlPlaneStub(canal)
        try:
            yield _Plano(stub=stub, client=client, settings=settings)
        finally:
            canal.close()


class _Plano:
    def __init__(self, stub, client, settings) -> None:
        self.stub = stub
        self.client = client
        self.settings = settings
        engine = build_engine(settings.db_url)
        self._factory = build_session_factory(engine)

    def uow(self) -> SqlUnitOfWork:
        return SqlUnitOfWork(self._factory)

    def registrar(
        self,
        url: str = "http://localhost:8001",
        dominio: str = "local-1",
        boot_id: str = "boot-A",
        capacidad: int = 100 * MB,
        data_node_id: str = "",
    ):
        return self.stub.Register(
            control_pb2.RegisterRequest(
                advertise_url=url,
                fault_domain=dominio,
                boot_id=boot_id,
                capacity_bytes=capacidad,
                data_node_id=data_node_id,
            ),
            timeout=10,
        )

    def latir(self, data_node_id: str, boot_id: str = "boot-A", **kwargs):
        """Un solo latido: abre el stream, manda uno y lo cierra."""
        secuencia = kwargs.pop("sequence", 1)
        stats = control_pb2.NodeStats(
            used_bytes=kwargs.pop("used_bytes", 0),
            capacity_bytes=kwargs.pop("capacity_bytes", 100 * MB),
            disk_free_bytes=kwargs.pop("disk_free_bytes", 10 * 1024 * MB),
            block_count=kwargs.pop("block_count", 0),
        )
        peticion = control_pb2.HeartbeatRequest(
            data_node_id=data_node_id,
            boot_id=boot_id,
            sequence=secuencia,
            stats=stats,
            added_block_ids=kwargs.pop("added", []),
            removed_block_ids=kwargs.pop("removed", []),
        )
        # Se consume el stream entero: el ack llega primero y el FullReportReq detras,
        # asi que cortar en el ack se perderia justo lo que estas pruebas comprueban.
        # Con una sola peticion, el servidor termina su generador al agotarse la entrada.
        return list(self.stub.Heartbeat(iter([peticion]), timeout=10))

    def nodo(self, data_node_id: str):
        with self.uow() as u:
            return u.data_nodes.get(data_node_id)


class TestRegistro:
    def test_alta_de_un_nodo(self, plano: _Plano) -> None:
        respuesta = plano.registrar()
        assert respuesta.data_node_id
        assert respuesta.heartbeat_interval_ms == 3000
        assert respuesta.full_report_every_n == 20

        nodo = plano.nodo(respuesta.data_node_id)
        assert nodo.fault_domain == "local-1"
        assert nodo.boot_id == "boot-A"
        assert nodo.advertise_url == "http://localhost:8001"

    def test_reinicio_con_el_mismo_id_conserva_identidad(self, plano: _Plano) -> None:
        primero = plano.registrar()
        segundo = plano.registrar(data_node_id=primero.data_node_id, boot_id="boot-A")
        assert primero.data_node_id == segundo.data_node_id

    def test_sin_id_se_reconoce_por_la_url_anunciada(self, plano: _Plano) -> None:
        # Es lo que pasa cuando el nodo pierde su node.json pero conserva su URL.
        primero = plano.registrar()
        segundo = plano.registrar(boot_id="boot-B")
        assert primero.data_node_id == segundo.data_node_id

    def test_cuatro_nodos_en_dos_dominios(self, plano: _Plano) -> None:
        ids = set()
        for i, dominio in enumerate(["local-1", "local-1", "local-2", "local-2"], start=1):
            r = plano.registrar(
                url=f"http://localhost:800{i}", dominio=dominio, boot_id=f"boot-{i}"
            )
            ids.add(r.data_node_id)
        assert len(ids) == 4

        with plano.uow() as u:
            nodos = u.data_nodes.list_all()
        assert {n.fault_domain for n in nodos} == {"local-1", "local-2"}


class TestHeartbeat:
    def test_el_latido_recibe_ack_y_guarda_las_estadisticas(self, plano: _Plano) -> None:
        nodo_id = plano.registrar().data_node_id
        respuestas = plano.latir(nodo_id, used_bytes=5 * MB, block_count=3, disk_free_bytes=7 * MB)

        assert respuestas[0].HasField("ack")
        assert respuestas[0].ack.acked_sequence == 1

        nodo = plano.nodo(nodo_id)
        assert nodo.stats.used_bytes == 5 * MB
        assert nodo.stats.block_count == 3
        assert nodo.stats.disk_free_bytes == 7 * MB
        assert nodo.last_heartbeat_at is not None

    def test_un_nodo_desconocido_recibe_not_found(self, plano: _Plano) -> None:
        # Pasa si el metadato se recreo: el nodo tiene que volver a registrarse en vez
        # de latir al vacio para siempre.
        with pytest.raises(grpc.RpcError) as excinfo:
            plano.latir("inventado")
        assert excinfo.value.code() is grpc.StatusCode.NOT_FOUND

    def test_un_boot_id_distinto_pide_registro(self, plano: _Plano) -> None:
        nodo_id = plano.registrar(boot_id="boot-A").data_node_id
        respuestas = plano.latir(nodo_id, boot_id="boot-OTRO")
        pedidos = [m for m in respuestas if m.HasField("full_report")]
        assert pedidos, "deberia pedir un report completo"
        assert "registr" in pedidos[0].full_report.reason

    def test_un_bloque_desconocido_provoca_peticion_de_report(self, plano: _Plano) -> None:
        nodo_id = plano.registrar().data_node_id
        respuestas = plano.latir(nodo_id, added=["bloque-fantasma"])
        assert any(m.HasField("full_report") for m in respuestas)


class TestEstados:
    def test_de_alive_a_suspect_y_a_dead(self, plano: _Plano) -> None:
        # Umbrales de 300 ms y 900 ms, con el evaluador cada 50 ms.
        nodo_id = plano.registrar().data_node_id
        plano.latir(nodo_id)
        assert plano.nodo(nodo_id).state is DataNodeState.ALIVE

        time.sleep(0.5)
        assert plano.nodo(nodo_id).state is DataNodeState.SUSPECT

        time.sleep(0.7)
        assert plano.nodo(nodo_id).state is DataNodeState.DEAD

    def test_un_latido_lo_reincorpora(self, plano: _Plano) -> None:
        nodo_id = plano.registrar().data_node_id
        plano.latir(nodo_id, sequence=1)
        time.sleep(0.5)
        assert plano.nodo(nodo_id).state is DataNodeState.SUSPECT

        plano.latir(nodo_id, sequence=2)
        assert plano.nodo(nodo_id).state is DataNodeState.ALIVE


class TestBlockReport:
    def _sembrar_replica(self, plano: _Plano, nodo_id: str, block_id: str) -> None:
        """Mete un archivo con un bloque STORED en ese nodo, saltandose la API."""
        from datetime import datetime, timezone

        from dfsha.control_node.domain.entities import (
            Block,
            BlockReplica,
            Directory,
            File,
            FileState,
            User,
        )
        from dfsha.control_node.repositories.sql import new_id

        ahora = datetime.now(timezone.utc)
        with plano.uow() as u:
            usuario = User(id=new_id(), username=f"u-{block_id}", password_hash="x", created_at=ahora)
            raiz = Directory(id=new_id(), parent_id=None, name="", owner_id=usuario.id, created_at=ahora)
            archivo = File(
                id=new_id(),
                directory_id=raiz.id,
                name=f"{block_id}.bin",
                owner_id=usuario.id,
                size=MB,
                block_size=MB,
                state=FileState.COMMITTED,
                created_at=ahora,
                committed_at=ahora,
            )
            u.users.add(usuario)
            u.directories.add(raiz)
            u.files.add(archivo)
            u.blocks.add_plan(
                [Block(block_id=block_id, file_id=archivo.id, index=0, size=MB)],
                [
                    BlockReplica(
                        block_id=block_id,
                        data_node_id=nodo_id,
                        state=ReplicaState.STORED,
                        created_at=ahora,
                    )
                ],
            )
            u.commit()

    def _reportar(self, plano: _Plano, nodo_id: str, bloques: list[str], completo: bool = True):
        return plano.stub.BlockReport(
            control_pb2.BlockReportRequest(
                data_node_id=nodo_id,
                boot_id="boot-A",
                is_full=completo,
                blocks=[control_pb2.BlockInfo(block_id=b) for b in bloques],
            ),
            timeout=10,
        )

    def test_report_que_cuadra(self, plano: _Plano) -> None:
        nodo_id = plano.registrar().data_node_id
        self._sembrar_replica(plano, nodo_id, "blk-1")

        respuesta = self._reportar(plano, nodo_id, ["blk-1"])
        assert (respuesta.unknown_blocks, respuesta.missing_blocks) == (0, 0)

    def test_bloque_de_mas_es_huerfano_y_no_se_borra(self, plano: _Plano) -> None:
        nodo_id = plano.registrar().data_node_id
        self._sembrar_replica(plano, nodo_id, "blk-1")

        respuesta = self._reportar(plano, nodo_id, ["blk-1", "huerfano"])
        assert respuesta.unknown_blocks == 1
        # El ControlNode no borra nada: el huerfano no aparece en el metadato, y los
        # bytes siguen en el nodo hasta que alguien corra el GC.
        with plano.uow() as u:
            assert u.blocks.list_replicas(["huerfano"]) == {}

    def test_bloque_que_falta_pasa_a_missing(self, plano: _Plano) -> None:
        nodo_id = plano.registrar().data_node_id
        self._sembrar_replica(plano, nodo_id, "blk-1")

        respuesta = self._reportar(plano, nodo_id, [])
        assert respuesta.missing_blocks == 1

        with plano.uow() as u:
            replicas = u.blocks.list_replicas(["blk-1"])
        assert replicas["blk-1"][0].state is ReplicaState.MISSING

    def test_un_nodo_que_vuelve_recupera_su_replica(self, plano: _Plano) -> None:
        nodo_id = plano.registrar().data_node_id
        self._sembrar_replica(plano, nodo_id, "blk-1")

        self._reportar(plano, nodo_id, [])          # se da por perdida
        self._reportar(plano, nodo_id, ["blk-1"])   # vuelve a reportarla

        with plano.uow() as u:
            replicas = u.blocks.list_replicas(["blk-1"])
        assert replicas["blk-1"][0].state is ReplicaState.STORED

    def test_un_incremental_no_marca_nada_como_perdido(self, plano: _Plano) -> None:
        nodo_id = plano.registrar().data_node_id
        self._sembrar_replica(plano, nodo_id, "blk-1")

        respuesta = self._reportar(plano, nodo_id, [], completo=False)
        assert respuesta.missing_blocks == 0
        with plano.uow() as u:
            assert u.blocks.list_replicas(["blk-1"])["blk-1"][0].state is ReplicaState.STORED


class TestDiscoPerdido:
    def test_volver_con_otro_boot_id_da_las_replicas_por_perdidas(self, plano: _Plano) -> None:
        nodo_id = plano.registrar(boot_id="boot-A").data_node_id
        TestBlockReport()._sembrar_replica(plano, nodo_id, "blk-1")

        plano.registrar(data_node_id=nodo_id, boot_id="boot-NUEVO")

        with plano.uow() as u:
            replicas = u.blocks.list_replicas(["blk-1"])
            nodo = u.data_nodes.get(nodo_id)
        assert replicas["blk-1"][0].state is ReplicaState.MISSING
        assert nodo.boot_id == "boot-NUEVO"

    def test_volver_con_el_mismo_boot_id_las_conserva(self, plano: _Plano) -> None:
        nodo_id = plano.registrar(boot_id="boot-A").data_node_id
        TestBlockReport()._sembrar_replica(plano, nodo_id, "blk-1")

        plano.registrar(data_node_id=nodo_id, boot_id="boot-A")

        with plano.uow() as u:
            replicas = u.blocks.list_replicas(["blk-1"])
        assert replicas["blk-1"][0].state is ReplicaState.STORED
