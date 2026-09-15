"""Repositorios contra SQLite en memoria.

Comprueba lo que solo se ve cuando hay una base de datos de verdad: el indice unico
parcial sobre COMMITTED, la atomicidad del copy-on-write, y que las fechas vuelven con
zona horaria.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from dfsha.control_node.domain.entities import (
    Block,
    BlockReplica,
    Directory,
    File,
    FileState,
    ReplicaState,
    User,
)
from dfsha.control_node.repositories.database import (
    build_engine,
    build_session_factory,
    create_schema,
)
from dfsha.control_node.repositories.sql import SqlUnitOfWork, new_id

AHORA = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)
MB = 1024 * 1024


@pytest.fixture()
def uow_factory():
    engine = build_engine("sqlite://")  # en memoria
    create_schema(engine)
    factory = build_session_factory(engine)

    # Una sola conexion para toda la prueba: con SQLite en memoria, cada conexion nueva
    # veria una base de datos distinta.
    def abrir() -> SqlUnitOfWork:
        return SqlUnitOfWork(factory)

    engine_holder = engine.connect()
    try:
        yield abrir
    finally:
        engine_holder.close()
        engine.dispose()


@pytest.fixture()
def uow(uow_factory):
    with uow_factory() as unidad:
        yield unidad


def sembrar_usuario(uow: SqlUnitOfWork, username: str = "ana") -> tuple[User, Directory]:
    usuario = User(id=new_id(), username=username, password_hash="x", created_at=AHORA)
    raiz = Directory(
        id=new_id(), parent_id=None, name="", owner_id=usuario.id, created_at=AHORA
    )
    uow.users.add(usuario)
    uow.directories.add(raiz)
    uow.flush()
    return usuario, raiz


def hacer_archivo(
    owner_id: str,
    directory_id: str,
    name: str = "datos.bin",
    state: FileState = FileState.COMMITTED,
    size: int = 3 * MB,
    expires_at: datetime | None = None,
) -> File:
    return File(
        id=new_id(),
        directory_id=directory_id,
        name=name,
        owner_id=owner_id,
        size=size,
        block_size=MB,
        state=state,
        created_at=AHORA,
        committed_at=AHORA if state is FileState.COMMITTED else None,
        expires_at=expires_at,
    )


class TestUsuariosYDirectorios:
    def test_alta_y_busqueda_por_nombre(self, uow: SqlUnitOfWork) -> None:
        usuario, _ = sembrar_usuario(uow)
        encontrado = uow.users.get_by_username("ana")
        assert encontrado is not None and encontrado.id == usuario.id
        assert uow.users.get_by_username("nadie") is None

    def test_cada_usuario_tiene_su_propia_raiz(self, uow: SqlUnitOfWork) -> None:
        ana, raiz_ana = sembrar_usuario(uow, "ana")
        beto, raiz_beto = sembrar_usuario(uow, "beto")

        assert uow.directories.get_root(ana.id).id == raiz_ana.id
        assert uow.directories.get_root(beto.id).id == raiz_beto.id
        assert raiz_ana.id != raiz_beto.id

    def test_las_fechas_vuelven_con_zona_horaria(self, uow: SqlUnitOfWork) -> None:
        # Sin esto, comparar expires_at con utcnow() lanzaria TypeError justo en la
        # comprobacion de reservas vencidas.
        usuario, _ = sembrar_usuario(uow)
        uow.commit()
        recargado = uow.users.get(usuario.id)
        assert recargado.created_at.tzinfo is not None
        assert recargado.created_at == AHORA

    def test_nombre_duplicado_en_el_mismo_padre(self, uow: SqlUnitOfWork) -> None:
        usuario, raiz = sembrar_usuario(uow)

        def agregar() -> None:
            uow.directories.add(
                Directory(
                    id=new_id(),
                    parent_id=raiz.id,
                    name="fotos",
                    owner_id=usuario.id,
                    created_at=AHORA,
                )
            )

        agregar()
        with pytest.raises(IntegrityError):
            agregar()

    def test_count_children_cuenta_subdirectorios_y_archivos_vivos(
        self, uow: SqlUnitOfWork
    ) -> None:
        usuario, raiz = sembrar_usuario(uow)
        uow.directories.add(
            Directory(
                id=new_id(),
                parent_id=raiz.id,
                name="sub",
                owner_id=usuario.id,
                created_at=AHORA,
            )
        )
        uow.files.add(hacer_archivo(usuario.id, raiz.id, "vivo.bin"))
        borrado = hacer_archivo(usuario.id, raiz.id, "muerto.bin")
        uow.files.add(borrado)
        uow.files.mark_deleted(borrado.id, AHORA)
        uow.flush()

        assert uow.directories.count_children(raiz.id) == 2

    def test_list_descendants_recorre_el_subarbol(self, uow: SqlUnitOfWork) -> None:
        usuario, raiz = sembrar_usuario(uow)
        padre_id = raiz.id
        creados = []
        for nombre in ("a", "b", "c"):
            hijo = Directory(
                id=new_id(),
                parent_id=padre_id,
                name=nombre,
                owner_id=usuario.id,
                created_at=AHORA,
            )
            uow.directories.add(hijo)
            creados.append(hijo.id)
            padre_id = hijo.id
        uow.flush()

        assert {d.id for d in uow.directories.list_descendants(raiz.id)} == set(creados)


class TestArchivos:
    def test_dos_committed_en_la_misma_ruta_colisionan(self, uow: SqlUnitOfWork) -> None:
        usuario, raiz = sembrar_usuario(uow)
        uow.files.add(hacer_archivo(usuario.id, raiz.id))
        with pytest.raises(IntegrityError):
            uow.files.add(hacer_archivo(usuario.id, raiz.id))

    def test_el_indice_parcial_deja_convivir_versiones_muertas(
        self, uow: SqlUnitOfWork
    ) -> None:
        # Copy-on-write: la misma ruta acumula versiones DELETED y reservas WRITING
        # abandonadas sin que el indice unico se queje.
        usuario, raiz = sembrar_usuario(uow)
        for _ in range(3):
            viejo = hacer_archivo(usuario.id, raiz.id)
            uow.files.add(viejo)
            uow.flush()
            uow.files.mark_deleted(viejo.id, AHORA)
        uow.files.add(
            hacer_archivo(
                usuario.id, raiz.id, state=FileState.WRITING, expires_at=AHORA
            )
        )
        vivo = hacer_archivo(usuario.id, raiz.id)
        uow.files.add(vivo)
        uow.flush()

        assert uow.files.get_live_by_name(raiz.id, "datos.bin").id == vivo.id

    def test_get_live_by_name_prefiere_el_committed(self, uow: SqlUnitOfWork) -> None:
        usuario, raiz = sembrar_usuario(uow)
        reserva = hacer_archivo(
            usuario.id, raiz.id, state=FileState.WRITING, expires_at=AHORA
        )
        confirmado = hacer_archivo(usuario.id, raiz.id)
        uow.files.add(reserva)
        uow.files.add(confirmado)
        uow.flush()

        assert uow.files.get_live_by_name(raiz.id, "datos.bin").id == confirmado.id

    def test_get_live_by_name_devuelve_la_reserva_vencida(self, uow: SqlUnitOfWork) -> None:
        # El repositorio no decide si sigue viva: devuelve la fila y el dominio juzga.
        usuario, raiz = sembrar_usuario(uow)
        vencida = hacer_archivo(
            usuario.id,
            raiz.id,
            state=FileState.WRITING,
            expires_at=AHORA - timedelta(seconds=1),
        )
        uow.files.add(vencida)
        uow.flush()

        encontrada = uow.files.get_live_by_name(raiz.id, "datos.bin")
        assert encontrada.id == vencida.id
        assert encontrada.is_expired(AHORA) is True
        assert encontrada.holds_name(AHORA) is False

    def test_commit_limpia_el_vencimiento(self, uow: SqlUnitOfWork) -> None:
        usuario, raiz = sembrar_usuario(uow)
        reserva = hacer_archivo(
            usuario.id,
            raiz.id,
            state=FileState.WRITING,
            expires_at=AHORA + timedelta(minutes=10),
        )
        uow.files.add(reserva)
        uow.flush()
        uow.files.mark_committed(reserva.id, AHORA)

        confirmado = uow.files.get(reserva.id)
        assert confirmado.state is FileState.COMMITTED
        assert confirmado.expires_at is None
        assert confirmado.is_expired(AHORA + timedelta(days=1)) is False

    def test_list_committed_ignora_lo_que_no_esta_vivo(self, uow: SqlUnitOfWork) -> None:
        usuario, raiz = sembrar_usuario(uow)
        uow.files.add(hacer_archivo(usuario.id, raiz.id, "a.bin"))
        uow.files.add(
            hacer_archivo(
                usuario.id, raiz.id, "b.bin", state=FileState.WRITING, expires_at=AHORA
            )
        )
        borrado = hacer_archivo(usuario.id, raiz.id, "c.bin")
        uow.files.add(borrado)
        uow.flush()
        uow.files.mark_deleted(borrado.id, AHORA)

        assert [f.name for f in uow.files.list_committed(raiz.id)] == ["a.bin"]

    def test_mover_es_solo_metadato(self, uow: SqlUnitOfWork) -> None:
        usuario, raiz = sembrar_usuario(uow)
        destino = Directory(
            id=new_id(),
            parent_id=raiz.id,
            name="sub",
            owner_id=usuario.id,
            created_at=AHORA,
        )
        uow.directories.add(destino)
        archivo = hacer_archivo(usuario.id, raiz.id)
        uow.files.add(archivo)
        uow.blocks.add_plan(
            [Block(block_id="b0", file_id=archivo.id, index=0, size=MB)],
            [
                BlockReplica(
                    block_id="b0",
                    data_node_id=self._nodo(uow).id,
                    state=ReplicaState.PENDING,
                    created_at=AHORA,
                )
            ],
        )
        uow.flush()
        antes = [b.block_id for b in uow.blocks.list_for_file(archivo.id)]

        uow.files.move(archivo.id, destino.id, "otro.bin")

        movido = uow.files.get(archivo.id)
        assert (movido.directory_id, movido.name) == (destino.id, "otro.bin")
        assert [b.block_id for b in uow.blocks.list_for_file(archivo.id)] == antes

    @staticmethod
    def _nodo(uow: SqlUnitOfWork):
        return uow.data_nodes.register("http://dn:8001", 1_000_000_000, AHORA)


class TestBloquesYReplicas:
    def _preparar(self, uow: SqlUnitOfWork, bloques: int = 3):
        usuario, raiz = sembrar_usuario(uow)
        nodo = uow.data_nodes.register("http://dn:8001", 10 * MB, AHORA)
        archivo = hacer_archivo(
            usuario.id,
            raiz.id,
            state=FileState.WRITING,
            size=bloques * MB,
            expires_at=AHORA + timedelta(minutes=10),
        )
        uow.files.add(archivo)
        ids = [f"blk-{i}" for i in range(bloques)]
        uow.blocks.add_plan(
            [
                Block(block_id=bid, file_id=archivo.id, index=i, size=MB)
                for i, bid in enumerate(ids)
            ],
            [
                BlockReplica(
                    block_id=bid,
                    data_node_id=nodo.id,
                    state=ReplicaState.PENDING,
                    created_at=AHORA,
                )
                for bid in ids
            ],
        )
        uow.flush()
        return usuario, raiz, nodo, archivo, ids

    def test_todos_pendientes_al_crear_el_plan(self, uow: SqlUnitOfWork) -> None:
        _, _, _, archivo, ids = self._preparar(uow)
        assert uow.blocks.pending_block_ids(archivo.id) == ids

    def test_mark_stored_fija_checksum_y_vacia_los_pendientes(
        self, uow: SqlUnitOfWork
    ) -> None:
        _, _, nodo, archivo, ids = self._preparar(uow)
        for bid in ids:
            assert uow.blocks.mark_stored(bid, nodo.id, MB, "a" * 64) is True

        assert uow.blocks.pending_block_ids(archivo.id) == []
        assert all(b.checksum_sha256 == "a" * 64 for b in uow.blocks.list_for_file(archivo.id))

    def test_mark_stored_de_un_bloque_desconocido(self, uow: SqlUnitOfWork) -> None:
        _, _, nodo, _, _ = self._preparar(uow)
        assert uow.blocks.mark_stored("no-existe", nodo.id, MB, "a" * 64) is False

    def test_mark_stored_desde_un_nodo_fuera_del_plan(self, uow: SqlUnitOfWork) -> None:
        # Un nodo que no estaba en el plan no confirma nada, y sobre todo no fija el
        # checksum: si lo hiciera, bastaria una notificacion equivocada para que el
        # cliente verificase sus descargas contra un hash que nadie escribio.
        _, _, _, archivo, ids = self._preparar(uow)
        otro = uow.data_nodes.register("http://dn2:8001", MB, AHORA)

        assert uow.blocks.mark_stored(ids[0], otro.id, MB, "a" * 64) is False

        bloques = {b.block_id: b for b in uow.blocks.list_for_file(archivo.id)}
        assert bloques[ids[0]].checksum_sha256 is None
        assert uow.blocks.pending_block_ids(archivo.id) == ids

    def test_list_replicas_agrupa_por_bloque(self, uow: SqlUnitOfWork) -> None:
        _, _, nodo, _, ids = self._preparar(uow)
        replicas = uow.blocks.list_replicas(ids)
        assert set(replicas) == set(ids)
        assert all(r[0].data_node_id == nodo.id for r in replicas.values())

    def test_list_replicas_sin_ids(self, uow: SqlUnitOfWork) -> None:
        assert uow.blocks.list_replicas([]) == {}

    def test_total_size(self, uow: SqlUnitOfWork) -> None:
        _, _, _, _, ids = self._preparar(uow)
        assert uow.blocks.total_size(ids) == 3 * MB
        assert uow.blocks.total_size([]) == 0


class TestHuerfanos:
    def test_archivo_borrado_y_reserva_vencida_son_huerfanos(
        self, uow: SqlUnitOfWork
    ) -> None:
        usuario, raiz = sembrar_usuario(uow)
        nodo = uow.data_nodes.register("http://dn:8001", 10 * MB, AHORA)

        vivo = hacer_archivo(usuario.id, raiz.id, "vivo.bin")
        borrado = hacer_archivo(usuario.id, raiz.id, "borrado.bin")
        vencido = hacer_archivo(
            usuario.id,
            raiz.id,
            "vencido.bin",
            state=FileState.WRITING,
            expires_at=AHORA - timedelta(seconds=1),
        )
        en_curso = hacer_archivo(
            usuario.id,
            raiz.id,
            "en_curso.bin",
            state=FileState.WRITING,
            expires_at=AHORA + timedelta(minutes=10),
        )
        for archivo, etiqueta in (
            (vivo, "v"),
            (borrado, "b"),
            (vencido, "x"),
            (en_curso, "c"),
        ):
            uow.files.add(archivo)
            uow.blocks.add_plan(
                [Block(block_id=f"{etiqueta}0", file_id=archivo.id, index=0, size=MB)],
                [
                    BlockReplica(
                        block_id=f"{etiqueta}0",
                        data_node_id=nodo.id,
                        state=ReplicaState.STORED,
                        created_at=AHORA,
                    )
                ],
            )
        uow.flush()
        uow.files.mark_deleted(borrado.id, AHORA)

        huerfanos = uow.blocks.list_orphans(AHORA)
        assert {b.block_id for b, _ in huerfanos} == {"b0", "x0"}
        assert all(replicas[0].data_node_id == nodo.id for _, replicas in huerfanos)

    def test_delete_blocks_borra_bloques_y_replicas(self, uow: SqlUnitOfWork) -> None:
        usuario, raiz = sembrar_usuario(uow)
        nodo = uow.data_nodes.register("http://dn:8001", 10 * MB, AHORA)
        archivo = hacer_archivo(usuario.id, raiz.id)
        uow.files.add(archivo)
        uow.blocks.add_plan(
            [Block(block_id="b0", file_id=archivo.id, index=0, size=MB)],
            [
                BlockReplica(
                    block_id="b0",
                    data_node_id=nodo.id,
                    state=ReplicaState.STORED,
                    created_at=AHORA,
                )
            ],
        )
        uow.flush()

        assert uow.blocks.delete_blocks(["b0"]) == 1
        assert uow.blocks.list_for_file(archivo.id) == []
        assert uow.blocks.list_replicas(["b0"]) == {}
        assert uow.blocks.delete_blocks([]) == 0


class TestDataNodes:
    def test_registro_idempotente_por_base_url(self, uow: SqlUnitOfWork) -> None:
        # Reiniciar un DataNode no puede crear un nodo nuevo: sus replicas apuntan al id
        # viejo y se volverian ilocalizables.
        primero = uow.data_nodes.register("http://dn:8001", 100, AHORA)
        segundo = uow.data_nodes.register("http://dn:8001", 200, AHORA)

        assert primero.id == segundo.id
        assert segundo.capacity_bytes == 200
        assert len(uow.data_nodes.list_alive()) == 1

    def test_used_bytes_sube_y_baja_sin_pasar_de_cero(self, uow: SqlUnitOfWork) -> None:
        nodo = uow.data_nodes.register("http://dn:8001", 100, AHORA)
        uow.data_nodes.add_used_bytes(nodo.id, 60)
        assert uow.data_nodes.get(nodo.id).used_bytes == 60

        uow.data_nodes.add_used_bytes(nodo.id, -20)
        assert uow.data_nodes.get(nodo.id).used_bytes == 40

        uow.data_nodes.add_used_bytes(nodo.id, -999)
        assert uow.data_nodes.get(nodo.id).used_bytes == 0

    def test_free_bytes(self, uow: SqlUnitOfWork) -> None:
        nodo = uow.data_nodes.register("http://dn:8001", 100, AHORA)
        uow.data_nodes.add_used_bytes(nodo.id, 30)
        actualizado = uow.data_nodes.get(nodo.id)
        assert actualizado.free_bytes == 70
        assert actualizado.has_room_for(70) is True
        assert actualizado.has_room_for(71) is False


class TestUnidadDeTrabajo:
    def test_sin_commit_no_se_persiste_nada(self, uow_factory) -> None:
        with uow_factory() as unidad:
            sembrar_usuario(unidad, "ana")
            # se sale sin commit

        with uow_factory() as otra:
            assert otra.users.get_by_username("ana") is None

    def test_commit_persiste(self, uow_factory) -> None:
        with uow_factory() as unidad:
            sembrar_usuario(unidad, "ana")
            unidad.commit()

        with uow_factory() as otra:
            assert otra.users.get_by_username("ana") is not None

    def test_una_excepcion_deshace_la_transaccion_entera(self, uow_factory) -> None:
        # Es la garantia de la que depende el copy-on-write: viejo a DELETED y nuevo a
        # COMMITTED, o ninguna de las dos cosas.
        with pytest.raises(RuntimeError):
            with uow_factory() as unidad:
                usuario, raiz = sembrar_usuario(unidad, "ana")
                viejo = hacer_archivo(usuario.id, raiz.id)
                unidad.files.add(viejo)
                unidad.flush()
                unidad.files.mark_deleted(viejo.id, AHORA)
                raise RuntimeError("caida a mitad del commit")

        with uow_factory() as otra:
            assert otra.users.get_by_username("ana") is None

    def test_copy_on_write_en_una_sola_transaccion(self, uow_factory) -> None:
        with uow_factory() as unidad:
            usuario, raiz = sembrar_usuario(unidad, "ana")
            viejo = hacer_archivo(usuario.id, raiz.id)
            unidad.files.add(viejo)
            unidad.commit()

        with uow_factory() as unidad:
            raiz = unidad.directories.get_root(
                unidad.users.get_by_username("ana").id
            )
            nuevo = hacer_archivo(
                unidad.users.get_by_username("ana").id,
                raiz.id,
                state=FileState.WRITING,
                expires_at=AHORA + timedelta(minutes=10),
            )
            unidad.files.add(nuevo)
            unidad.flush()
            # El viejo sale de COMMITTED y el nuevo entra, en el mismo commit.
            unidad.files.mark_deleted(viejo.id, AHORA)
            unidad.files.mark_committed(nuevo.id, AHORA)
            unidad.commit()

        with uow_factory() as otra:
            vivos = otra.files.list_committed(raiz.id)
            assert [f.id for f in vivos] == [nuevo.id]
            assert otra.files.get(viejo.id).state is FileState.DELETED
