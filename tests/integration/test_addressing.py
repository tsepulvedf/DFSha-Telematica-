"""Dos direcciones por DataNode: la del cliente y la de sus pares.

## Por que existe este fichero

El Bloque B paso sus pruebas con el direccionamiento roto, y este fichero es la
correccion de esa cobertura, no un extra.

`start_cluster` levanta los cuatro DataNodes como hilos uvicorn **en el mismo proceso,
sobre 127.0.0.1**. Ahi la direccion que alcanza el cliente y la que alcanza un vecino son
literalmente la misma, asi que el reenvio del pipeline funcionaba porque no habia forma de
que no funcionara. `test_el_cliente_sube_los_bytes_una_sola_vez` comprobaba que el bloque
acababa en tres discos, y eso era cierto — pero verificaba el **mecanismo** y no el
**direccionamiento**, y en esa topologia no puede distinguirlos.

En Docker si se distinguen: el cliente esta fuera (`localhost:800N`) y los vecinos dentro
(`data-node-N:8001`). Con una sola direccion, DN1 reenviaba a `localhost:8002` y eso
resolvia a **si mismo**. Cada bloque quedaba con una copia y el `commit` fallaba con 409
por no alcanzar W=2. El mismo error estaba en la re-replicacion, donde el nodo destino se
habria descargado el bloque de si mismo.

## Las dos formas de probarlo, y por que hacen falta las dos

1. **Contrato** (`TestContrato`): que el ControlNode pone la direccion de PAR en la cadena
   y la de CLIENTE en el plan, y que el cliente copia la primera tal cual. Es la
   afirmacion que habria fallado desde el primer dia, y no necesita red.

2. **Escenario invertido** (`TestDireccionDeClienteMuerta`): un cluster donde la direccion
   de cliente **no responde desde ningun sitio**, dejando la de par como la unica que
   funciona. Es la inversion del caso de Docker —alli la de cliente funciona desde fuera y
   no desde dentro— y tiene la misma propiedad util: cualquier camino nodo-a-nodo que use
   la direccion equivocada falla. Es lo que permite reproducir en un solo proceso un fallo
   que solo se manifestaba con dos redes.

   Es la misma tactica que `test_report_incremental_nunca_marca_missing` de la Etapa 2:
   montar el escenario donde la implementacion incorrecta produce un sintoma visible, en
   vez de confiar en que el correcto se note.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from dfsha.common.blocktoken import BLOCK_TOKEN_HEADER
from dfsha.client.api import ControlApi
from dfsha.client.session import Session
from dfsha.client.transfer import PIPELINE_HEADER, upload_blocks

from .cluster import MB, Cluster, start_cluster

RAPIDO = dict(
    heartbeat_interval_ms=300,
    suspect_after_ms=1500,
    dead_after_ms=3000,
    membership_interval_ms=100,
    min_free_bytes=0,
)


def _sesion(cluster: Cluster, usuario: str) -> ControlApi:
    sesion = Session(control_url=cluster.control_url)
    api = ControlApi(sesion)
    api.register(usuario, "contrasena-de-prueba")
    sesion.token = api.login(usuario, "contrasena-de-prueba").access_token
    return api


def _archivo(tmp_path: Path, nombre: str, tamano: int) -> tuple[Path, str]:
    ruta = tmp_path / nombre
    datos = bytes((i * 11 + 5) % 256 for i in range(tamano))
    ruta.write_bytes(datos)
    return ruta, hashlib.sha256(datos).hexdigest()


# --- Pareja 1: el contrato -------------------------------------------------


class TestContrato:
    """Que cada direccion va donde le toca. Sin red, solo mirando los mensajes."""

    @pytest.fixture()
    def cluster(self, tmp_path_factory):
        c = start_cluster(
            tmp_path_factory.mktemp("contrato"),
            block_size=MB,
            data_nodes=4,
            fault_domains=["zona-a", "zona-b", "zona-c", "zona-d"],
            replication_factor=3,
            write_quorum=2,
            advertise_muerta=True,  # las dos direcciones son distintas y se ve cual es cual
            **RAPIDO,
        )
        try:
            yield c
        finally:
            c.stop()

    def test_el_plan_separa_la_direccion_de_cliente_de_la_de_par(self, cluster) -> None:
        api = _sesion(cluster, "ana")
        plan = api.create_file("/contrato.bin", MB)
        bloque = plan.blocks[0]

        anunciadas = {n.settings.datanode_advertise_url for n in cluster.nodes}
        de_par = {n.url for n in cluster.nodes}

        # `replicas` es lo que el cliente usa para mandar los bytes: direcciones de
        # cliente.
        for replica in bloque.replicas:
            assert replica.base_url in anunciadas

        # `pipeline` es lo que va a viajar entre nodos: direcciones de par, y NINGUNA de
        # cliente. Esta es la assercion que habria fallado con el bug.
        assert len(bloque.pipeline) == 2
        for url in bloque.pipeline:
            assert url in de_par
            assert url not in anunciadas

    def test_la_cadena_no_incluye_al_primer_nodo(self, cluster) -> None:
        """El primero recibe los bytes del cliente; la cadena es el RESTO.

        Incluirlo haria que se reenviara el bloque a si mismo y contestara 409, que
        contaria como replica y daria un quorum falso.
        """
        api = _sesion(cluster, "beto")
        bloque = api.create_file("/cadena.bin", MB).blocks[0]

        por_id = {n.data_node_id: n for n in cluster.nodes}
        primero = por_id[bloque.replicas[0].data_node_id]

        assert primero.url not in bloque.pipeline

    def test_el_cliente_copia_la_cadena_tal_cual(self, cluster, tmp_path) -> None:
        """El cliente no deduce direcciones de par: repite la lista que le dieron.

        Se comprueba interceptando la cabecera que sale de `upload_blocks`, porque es
        exactamente donde estaba el fallo: la version anterior la construia a partir de
        `replicas`, que son direcciones de cliente.
        """
        api = _sesion(cluster, "carla")
        local, _ = _archivo(tmp_path, "cabecera.bin", MB)
        plan = api.create_file("/cabecera.bin", MB)
        bloque = plan.blocks[0]

        enviadas: list[str] = []
        put_real = httpx.put

        def espia(url, **kwargs):
            cabecera = kwargs.get("headers", {}).get(PIPELINE_HEADER)
            if cabecera:
                enviadas.append(cabecera)
            return put_real(url, **kwargs)

        import dfsha.client.transfer as transfer

        transfer.httpx.put = espia
        try:
            # Va a la direccion de PAR, que es la unica que responde en este cluster.
            # Lo que se mira es la cabecera, no si la subida termina.
            slots_url = {n.data_node_id: n.url for n in cluster.nodes}
            bloque.replicas[0] = bloque.replicas[0].model_copy(
                update={"base_url": slots_url[bloque.replicas[0].data_node_id]}
            )
            upload_blocks(local, [bloque], parallel=1)
        except Exception:
            pass  # la subida puede fallar; la cabecera ya se capturo
        finally:
            transfer.httpx.put = put_real

        assert enviadas, "el cliente no mando cabecera de pipeline"
        assert enviadas[0].split(",") == list(bloque.pipeline)


# La comprobacion de contrato de la RE-REPLICACION vive en
# `tests/unit/test_rereplication_leadership.py`, no aqui, y por un motivo concreto: en un
# cluster vivo el planificador real despacha la copia y el DataNode la completa en
# milisegundos, asi que para cuando esta prueba miraria la cola, la tarea ya esta cerrada.
# La primera version de esta prueba fallaba por eso — no por el codigo, sino porque el
# sistema hacia su trabajo demasiado rapido. Una assercion de contrato no debe depender de
# ganarle una carrera al propio sistema.
#
# Lo que si se comprueba aqui es su mitad invertida, mas abajo: que la recuperacion
# funciona en un cluster donde la direccion de cliente no responde.


# --- Pareja 2: el escenario invertido --------------------------------------


class TestDireccionDeClienteMuerta:
    """Un cluster donde la direccion de cliente no responde.

    Si algun camino nodo-a-nodo la usara, fallaria igual que fallo en Docker. Que estas
    pruebas pasen es la demostracion de que ninguno la usa.
    """

    @pytest.fixture()
    def cluster(self, tmp_path_factory):
        c = start_cluster(
            tmp_path_factory.mktemp("invertido"),
            block_size=MB,
            data_nodes=4,
            fault_domains=["zona-a", "zona-b", "zona-c", "zona-d"],
            replication_factor=3,
            write_quorum=2,
            advertise_muerta=True,
            **RAPIDO,
        )
        try:
            yield c
        finally:
            c.stop()

    def test_el_pipeline_replica_aunque_la_direccion_de_cliente_no_responda(
        self, cluster, tmp_path
    ) -> None:
        """LA prueba de este fichero.

        Los bytes se mandan a la direccion de PAR del primer nodo, que es como llegarian
        desde la red donde el cliente si alcanza a ese nodo. A partir de ahi, todo el
        reenvio depende de que la cadena traiga direcciones de par: con las de cliente,
        cada `next_hop` seria un puerto muerto, `acked` seria 1 y el commit fallaria con
        409 — que es exactamente el sintoma que se vio en Docker.
        """
        api = _sesion(cluster, "elena")
        local, _ = _archivo(tmp_path, "invertido.bin", 2 * MB)

        plan = api.create_file("/invertido.bin", 2 * MB)
        por_id = {n.data_node_id: n for n in cluster.nodes}

        for bloque in plan.blocks:
            primero = por_id[bloque.replicas[0].data_node_id]
            datos = local.read_bytes()[
                bloque.index * MB : bloque.index * MB + bloque.size
            ]
            respuesta = httpx.put(
                f"{primero.url}/api/v1/blocks/{bloque.block_id}",
                content=datos,
                headers={
                    "X-DFSha-Checksum": hashlib.sha256(datos).hexdigest(),
                    PIPELINE_HEADER: ",".join(bloque.pipeline),
                    # El token viaja con los bytes y cada salto de la cadena lo
                    # reenvia tal cual: los tres nodos escriben el mismo bloque por
                    # orden del mismo ControlNode.
                    BLOCK_TOKEN_HEADER: bloque.token,
                },
                timeout=60,
            )
            assert respuesta.status_code == 201
            # La cadena entera confirmo: tres copias, no una.
            assert respuesta.headers["X-DFSha-Replicas-Acked"] == "3"

        # Y el commit pasa, que es lo que fallaba con 409 en Docker.
        api.commit_file(plan.file_id)

        info = api.stat("/invertido.bin")
        assert info.replication_state == "FULLY_REPLICATED"
        assert info.min_replicas == 3

        # Los bloques estan en tres discos distintos de verdad.
        for bloque in plan.blocks:
            tienen = [n for n in cluster.nodes if bloque.block_id in n.blk_en_disco()]
            assert len(tienen) == 3

    def test_la_rereplicacion_se_recupera_con_la_direccion_de_cliente_muerta(
        self, tmp_path_factory, tmp_path
    ) -> None:
        """La mitad invertida del otro sitio con el mismo defecto.

        El nodo destino descarga el bloque del origen usando la URL que le manda el
        ControlNode. Con la direccion de cliente —muerta en este cluster— la descarga
        fallaria y el archivo se quedaria sub-replicado para siempre. Que vuelva a
        FULLY_REPLICATED es la prueba de que se usa la de par.
        """
        import time

        cluster = start_cluster(
            tmp_path_factory.mktemp("invertido-recup"),
            block_size=MB,
            data_nodes=4,
            fault_domains=["zona-a", "zona-b", "zona-c", "zona-d"],
            replication_factor=3,
            write_quorum=2,
            advertise_muerta=True,
            rereplication_grace_ms=1000,
            rereplication_interval_ms=200,
            **RAPIDO,
        )
        try:
            api = _sesion(cluster, "fran")
            local, _ = _archivo(tmp_path, "recup.bin", MB)

            por_id = {n.data_node_id: n for n in cluster.nodes}
            plan = api.create_file("/recup.bin", MB)
            bloque = plan.blocks[0]
            datos = local.read_bytes()

            primero = por_id[bloque.replicas[0].data_node_id]
            respuesta = httpx.put(
                f"{primero.url}/api/v1/blocks/{bloque.block_id}",
                content=datos,
                headers={
                    "X-DFSha-Checksum": hashlib.sha256(datos).hexdigest(),
                    PIPELINE_HEADER: ",".join(bloque.pipeline),
                    # El token viaja con los bytes y cada salto de la cadena lo
                    # reenvia tal cual: los tres nodos escriben el mismo bloque por
                    # orden del mismo ControlNode.
                    BLOCK_TOKEN_HEADER: bloque.token,
                },
                timeout=60,
            )
            assert respuesta.status_code == 201
            api.commit_file(plan.file_id)
            assert api.stat("/recup.bin").replication_state == "FULLY_REPLICATED"

            tenedores = {n for n, h in por_id.items() if bloque.block_id in h.blk_en_disco()}
            libres = set(por_id) - tenedores
            por_id[sorted(tenedores)[0]].stop()

            limite = time.time() + 45
            recuperado = False
            while time.time() < limite:
                if any(bloque.block_id in por_id[n].blk_en_disco() for n in libres):
                    recuperado = True
                    break
                time.sleep(0.3)

            assert recuperado, (
                "la tercera copia no se restauro: el nodo destino no pudo descargar del "
                "origen, que es lo que pasa si se le manda la direccion de cliente"
            )
            assert api.stat("/recup.bin").replication_state == "FULLY_REPLICATED"
        finally:
            cluster.stop()
