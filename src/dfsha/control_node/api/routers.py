"""Routers HTTP del ControlNode.

Solo traduccion: HTTP entra, caso de uso se ejecuta, DTO sale. Ninguna regla de negocio
vive aqui, ni una sola consulta SQL. Si un handler crece mas alla de armar argumentos y
devolver un DTO, la logica esta en el sitio equivocado.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from dfsha.common.dto import (
    BlockReadPlan,
    ClusterStatusResponse,
    DataNodeStatus,
    BlockStoredRequest,
    BlockWritePlan,
    CommitResponse,
    CreateFileRequest,
    CreateFileResponse,
    AclGrant,
    AclResponse,
    GcConfirmRequest,
    GcDispatchResponse,
    GroupInfo,
    GroupMemberRequest,
    GroupRequest,
    GroupsResponse,
    ShareRequest,
    SharedEntryInfo,
    SharedWithMeResponse,
    UnshareRequest,
    LeadershipResponse,
    LoginRequest,
    LsEntry,
    LsResponse,
    MkdirRequest,
    MvRequest,
    OpenFileResponse,
    OrphanBlock,
    OrphanBlocksResponse,
    RegisterRequest,
    ReplicaRef,
    StatResponse,
    TokenResponse,
)
from dfsha.control_node.commands import acl as acl_commands
from dfsha.control_node.commands import auth as auth_commands
from dfsha.control_node.commands import files as file_commands
from dfsha.control_node.commands import internal as internal_commands
from dfsha.control_node.commands import leadership as leadership_commands
from dfsha.control_node.commands import namespace as namespace_commands
from dfsha.control_node.queries import acl as acl_queries
from dfsha.control_node.queries import cluster as cluster_queries
from dfsha.control_node.queries import files as file_queries
from dfsha.control_node.queries import gc as gc_queries
from dfsha.control_node.queries import namespace as namespace_queries

from dfsha.control_node.domain.entities import utcnow
from dfsha.control_node.domain.membership import MembershipThresholds

from .deps import CurrentUser, Placement, QueryUow, Settings, Uow

__all__ = [
    "auth_router",
    "cluster_router",
    "fs_router",
    "files_router",
    "acl_router",
    "internal_router",
]

auth_router = APIRouter(prefix="/auth", tags=["auth"])
cluster_router = APIRouter(prefix="/cluster", tags=["cluster"])
fs_router = APIRouter(prefix="/fs", tags=["namespace"])
files_router = APIRouter(prefix="/files", tags=["transferencia"])
acl_router = APIRouter(prefix="/acl", tags=["permisos"])
#: Sin dependencia de autenticacion, y no es un descuido: este router se monta en una
#: app aparte, servida en un puerto propio con TLS mutuo. La puerta la guarda el
#: handshake, no el codigo, asi que no hay forma de anadir una ruta aqui y olvidarse de
#: protegerla. Ver `create_internal_app` en main.py.
internal_router = APIRouter(prefix="/internal/v1", tags=["interno"])


def _replicas(pares: list[tuple[str, str]]) -> list[ReplicaRef]:
    return [ReplicaRef(data_node_id=nodo, base_url=url) for nodo, url in pares]


# --- Autenticacion ---------------------------------------------------------


@auth_router.post("/register", status_code=status.HTTP_201_CREATED)
def register(body: RegisterRequest, uow: Uow) -> dict[str, str]:
    user_id = auth_commands.register_user(uow, body.username, body.password)
    return {"user_id": user_id, "username": body.username}


@auth_router.post("/login")
def login(body: LoginRequest, uow: Uow, settings: Settings) -> TokenResponse:
    token, expires_in = auth_commands.login(
        uow, body.username, body.password, settings.jwt_secret, settings.jwt_ttl_seconds
    )
    return TokenResponse(access_token=token, expires_in=expires_in)


# --- Cluster ---------------------------------------------------------------


@cluster_router.get("/status")
def cluster_status(uow: QueryUow, user: CurrentUser, settings: Settings) -> ClusterStatusResponse:
    """Estado de todos los DataNodes.

    Pide token como el resto de `/api/v1`, pero no filtra por usuario: la topologia del
    cluster es la misma para todos y no revela nada del arbol de nadie.
    """
    nodos = cluster_queries.cluster_status(
        uow,
        MembershipThresholds.from_millis(
            settings.suspect_after_ms, settings.dead_after_ms
        ),
    )
    salud = cluster_queries.replication_health(uow, settings.replication_factor)
    return ClusterStatusResponse(
        nodes=[
            DataNodeStatus(
                data_node_id=n.data_node_id,
                advertise_url=n.advertise_url,
                fault_domain=n.fault_domain,
                state=n.state,
                used_bytes=n.used_bytes,
                capacity_bytes=n.capacity_bytes,
                disk_free_bytes=n.disk_free_bytes,
                block_count=n.block_count,
                replica_count=n.replica_count,
                seconds_since_heartbeat=n.seconds_since_heartbeat,
                writes_in_flight=n.writes_in_flight,
                reads_in_flight=n.reads_in_flight,
            )
            for n in nodos
        ],
        replication_factor=settings.replication_factor,
        write_quorum=settings.write_quorum,
        suspect_after_ms=settings.suspect_after_ms,
        dead_after_ms=settings.dead_after_ms,
        under_replicated_blocks=salud.under_replicated,
        critical_blocks=salud.critical,
    )


@cluster_router.get("/leadership")
def leadership(request: Request, uow: Uow, user: CurrentUser) -> LeadershipResponse:
    """Quien sostiene el lease, con que epoca y cuanto le queda.

    Va contra el PRIMARIO: el lease es el estado mas cambiante del sistema y servirlo
    desde una replica con retraso diria que el lider es quien ya dejo de serlo, que es
    justo lo contrario de para lo que se consulta.
    """
    servicio = request.app.state.leadership
    ahora = utcnow()
    lease = leadership_commands.read_lease(uow, ahora)

    return LeadershipResponse(
        leader_id=lease.leader_id,
        epoch=lease.epoch,
        is_self=lease.leader_id == servicio.instance_id,
        instance_id=servicio.instance_id,
        expires_in_seconds=round(lease.remaining_seconds(ahora), 3),
        acquired_at=lease.acquired_at,
        renewed_at=lease.renewed_at,
    )


# --- Namespace (RF1) -------------------------------------------------------


@fs_router.get("/ls")
def ls(path: str, uow: QueryUow, user: CurrentUser) -> LsResponse:
    entradas = namespace_queries.ls(uow, user.user_id, path)
    return LsResponse(
        entries=[
            LsEntry(name=e.name, type=e.type, size=e.size, created_at=e.created_at)
            for e in entradas
        ]
    )


@fs_router.get("/stat")
def stat(path: str, uow: QueryUow, user: CurrentUser, settings: Settings) -> StatResponse:
    s = namespace_queries.stat(
        uow, user.user_id, path, replication_factor=settings.replication_factor
    )
    return StatResponse(
        path=s.path,
        type=s.type,
        size=s.size,
        block_size=s.block_size,
        block_count=s.block_count,
        created_at=s.created_at,
        replication_state=s.replication_state,
        min_replicas=s.min_replicas,
        max_replicas=s.max_replicas,
        replication_factor=s.replication_factor,
    )


@fs_router.post("/mkdir", status_code=status.HTTP_201_CREATED)
def mkdir(body: MkdirRequest, uow: Uow, user: CurrentUser) -> Response:
    namespace_commands.mkdir(uow, user.user_id, body.path, body.parents)
    return Response(status_code=status.HTTP_201_CREATED)


@fs_router.delete("/rmdir", status_code=status.HTTP_204_NO_CONTENT)
def rmdir(path: str, uow: Uow, user: CurrentUser, recursive: bool = False) -> Response:
    namespace_commands.rmdir(uow, user.user_id, path, recursive)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@fs_router.delete("/rm", status_code=status.HTTP_204_NO_CONTENT)
def rm(path: str, uow: Uow, user: CurrentUser) -> Response:
    namespace_commands.rm(uow, user.user_id, path)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@fs_router.post("/mv", status_code=status.HTTP_204_NO_CONTENT)
def mv(body: MvRequest, uow: Uow, user: CurrentUser) -> Response:
    namespace_commands.mv(uow, user.user_id, body.src, body.dst)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Permisos (Etapa 3, Bloque C) ------------------------------------------
#
# Todos estos endpoints delegan la comprobacion en `services.permissions.require`, que es
# la misma que usa el resto del sistema. Un modulo de permisos que comprobara sus propios
# permisos con su propia logica seria el sitio mas facil de equivocarse y el menos
# probable de que alguien revisara.


@acl_router.post("/share", status_code=status.HTTP_204_NO_CONTENT)
def share(body: ShareRequest, uow: Uow, user: CurrentUser) -> Response:
    acl_commands.share(uow, user.user_id, body.path, body.principal, body.permission)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@acl_router.post("/unshare", status_code=status.HTTP_204_NO_CONTENT)
def unshare(body: UnshareRequest, uow: Uow, user: CurrentUser) -> Response:
    acl_commands.unshare(uow, user.user_id, body.path, body.principal)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@acl_router.get("/show")
def show_acl(path: str, uow: QueryUow, user: CurrentUser) -> AclResponse:
    """Que puede quien pregunta, y quien mas tiene concesiones aqui.

    Son dos cosas distintas a proposito: `effective` responde «que puedo hacer», ya
    resuelto con herencia y grupos; `grants` responde «quien mas puede». Mezclarlas seria
    la forma mas rapida de que nadie entendiera ninguna.
    """
    vista = acl_queries.acl_of(uow, user.user_id, path)
    return AclResponse(
        path=vista.path,
        effective=vista.effective,
        source=vista.source,
        inherited_from=vista.inherited_from,
        grants=[
            AclGrant(
                principal=g.principal,
                principal_type=g.principal_type,
                permission=g.permission,
                granted_by=g.granted_by,
                granted_at=g.granted_at,
            )
            for g in vista.grants
        ],
    )


@acl_router.get("/shared-with-me")
def shared_with_me(uow: QueryUow, user: CurrentUser) -> SharedWithMeResponse:
    """Lo que otros comparten contigo. NO se mezcla con tu arbol."""
    return SharedWithMeResponse(
        entries=[
            SharedEntryInfo(
                owner=e.owner,
                name=e.name,
                permission=e.permission,
                via_group=e.via_group or None,
                path=e.path,
            )
            for e in acl_queries.shared_with_me(uow, user.user_id)
        ]
    )


@acl_router.post("/groups", status_code=status.HTTP_201_CREATED)
def create_group(body: GroupRequest, uow: Uow, user: CurrentUser) -> Response:
    acl_commands.create_group(uow, user.user_id, body.name)
    return Response(status_code=status.HTTP_201_CREATED)


@acl_router.get("/groups")
def list_groups(uow: QueryUow, user: CurrentUser) -> GroupsResponse:
    return GroupsResponse(
        groups=[
            GroupInfo(name=nombre, members=miembros)
            for nombre, miembros in acl_queries.groups_of(uow, user.user_id)
        ]
    )


@acl_router.post("/groups/members", status_code=status.HTTP_204_NO_CONTENT)
def add_member(body: GroupMemberRequest, uow: Uow, user: CurrentUser) -> Response:
    acl_commands.add_member(uow, user.user_id, body.name, body.username)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@acl_router.post("/groups/members/remove", status_code=status.HTTP_204_NO_CONTENT)
def remove_member(body: GroupMemberRequest, uow: Uow, user: CurrentUser) -> Response:
    """POST y no DELETE: un DELETE con cuerpo lo tratan distinto proxies y clientes, y
    aqui hacen falta dos campos (grupo y usuario) que no caben comodos en la ruta."""
    acl_commands.remove_member(uow, user.user_id, body.name, body.username)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Transferencia (RF2) ---------------------------------------------------


@files_router.post("/create", status_code=status.HTTP_201_CREATED)
def create_file(
    body: CreateFileRequest,
    uow: Uow,
    user: CurrentUser,
    settings: Settings,
    placement: Placement,
) -> CreateFileResponse:
    creado = file_commands.create_file(
        uow,
        placement,
        user.user_id,
        body.path,
        body.size,
        default_block_size=settings.block_size,
        write_ttl_seconds=settings.write_ttl_seconds,
        block_size=body.block_size,
        replication_factor=settings.replication_factor,
    )
    return CreateFileResponse(
        file_id=creado.file_id,
        block_size=creado.block_size,
        expires_at=creado.expires_at,
        blocks=[
            BlockWritePlan(
                block_id=b.block_id,
                index=b.index,
                size=b.size,
                replicas=_replicas(b.replicas),
                pipeline=b.pipeline,
            )
            for b in creado.blocks
        ],
    )


@files_router.post("/{file_id}/commit")
def commit_file(
    file_id: str, uow: Uow, user: CurrentUser, settings: Settings
) -> CommitResponse:
    confirmado = file_commands.commit_file(
        uow,
        user.user_id,
        file_id,
        write_quorum=settings.write_quorum,
        replication_factor=settings.replication_factor,
    )
    return CommitResponse(
        path=confirmado.path, size=confirmado.size, block_count=confirmado.block_count
    )


@files_router.post("/{file_id}/abort", status_code=status.HTTP_204_NO_CONTENT)
def abort_file(file_id: str, uow: Uow, user: CurrentUser) -> Response:
    file_commands.abort_file(uow, user.user_id, file_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@files_router.get("/open")
def open_file(path: str, uow: QueryUow, user: CurrentUser) -> OpenFileResponse:
    plan = file_queries.open_file(uow, user.user_id, path)
    return OpenFileResponse(
        file_id=plan.file_id,
        size=plan.size,
        block_size=plan.block_size,
        blocks=[
            BlockReadPlan(
                block_id=b.block_id,
                index=b.index,
                size=b.size,
                checksum_sha256=b.checksum_sha256,
                replicas=_replicas(b.replicas),
            )
            for b in plan.blocks
        ],
    )


# --- Plano interno: DataNode y GC -----------------------------------------
#
# El registro del DataNode ya no esta aqui: se fue a gRPC (ControlPlane.Register) con el
# resto del plano de control. Lo que queda en REST es la confirmacion de bloque
# almacenado, que el DataNode llama de forma sincrona antes de responder 201 al cliente,
# y los dos endpoints del GC, que usa el script.


@internal_router.post("/blocks/{block_id}/stored", status_code=status.HTTP_204_NO_CONTENT)
def block_stored(block_id: str, body: BlockStoredRequest, uow: Uow) -> Response:
    internal_commands.mark_block_stored(
        uow, block_id, body.data_node_id, body.size, body.checksum_sha256
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@internal_router.get("/gc/orphan-blocks")
def orphan_blocks(uow: Uow) -> OrphanBlocksResponse:
    """Es una consulta, pero va al PRIMARIO a proposito.

    El GC no lee esta lista para mostrarla: la lee para **borrar bloques del disco**. Una
    replica retrasada podria incluir un bloque cuyo archivo se acaba de recrear, y el
    resultado no seria una pantalla desactualizada sino un borrado que no tocaba. La
    regla de la Etapa 3 es que las consultas cuya respuesta dispara una escritura
    destructiva no se sirven desde la replica.
    """
    return OrphanBlocksResponse(
        blocks=[
            OrphanBlock(
                block_id=b.block_id, size=b.size, replicas=_replicas(b.replicas)
            )
            for b in gc_queries.orphan_blocks(uow)
        ]
    )


@internal_router.post("/gc/dispatch")
def gc_dispatch(uow: Uow, settings: Settings) -> GcDispatchResponse:
    """Encola el borrado de los huerfanos por el **canal de control**.

    Es el GC por el camino que ya esta abierto: las ordenes viajan en el stream de
    heartbeat, asi que el recolector no necesita alcanzar a cada DataNode por REST desde
    fuera. En AWS eso importa, porque los DataNodes anuncian su IP privada.

    El script de `scripts/gc.py` sigue existiendo y sigue siendo el que pide el
    enunciado; esto es una segunda via, no un reemplazo. Y la diferencia practica es
    real: por el canal de control no hace falta que quien recolecta tenga ruta hasta los
    DataNodes, solo hasta el ControlNode.

    **No borra el metadato.** Encola el borrado en disco y ya esta; las filas se quitan
    con `/gc/confirm` cuando conste que el bloque no esta en ningun disco, igual que
    siempre. El ControlNode nunca borra datos por su cuenta.
    """
    resultado = internal_commands.dispatch_gc_deletions(
        uow, ttl_seconds=settings.write_ttl_seconds
    )
    return GcDispatchResponse(
        blocks=resultado.blocks, orders=resultado.orders, skipped=resultado.skipped
    )


@internal_router.post("/gc/confirm", status_code=status.HTTP_204_NO_CONTENT)
def gc_confirm(body: GcConfirmRequest, uow: Uow) -> Response:
    internal_commands.confirm_gc(uow, body.block_ids)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
