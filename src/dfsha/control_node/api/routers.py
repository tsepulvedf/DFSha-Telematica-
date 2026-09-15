"""Routers HTTP del ControlNode.

Solo traduccion: HTTP entra, caso de uso se ejecuta, DTO sale. Ninguna regla de negocio
vive aqui, ni una sola consulta SQL. Si un handler crece mas alla de armar argumentos y
devolver un DTO, la logica esta en el sitio equivocado.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status

from dfsha.common.dto import (
    BlockReadPlan,
    BlockStoredRequest,
    BlockWritePlan,
    CommitResponse,
    CreateFileRequest,
    CreateFileResponse,
    DataNodeRegisterRequest,
    DataNodeRegisterResponse,
    GcConfirmRequest,
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
from dfsha.control_node.commands import auth as auth_commands
from dfsha.control_node.commands import files as file_commands
from dfsha.control_node.commands import internal as internal_commands
from dfsha.control_node.commands import namespace as namespace_commands
from dfsha.control_node.queries import files as file_queries
from dfsha.control_node.queries import gc as gc_queries
from dfsha.control_node.queries import namespace as namespace_queries

from .deps import CurrentUser, Placement, Settings, Uow, require_internal_secret

__all__ = ["auth_router", "fs_router", "files_router", "internal_router"]

auth_router = APIRouter(prefix="/auth", tags=["auth"])
fs_router = APIRouter(prefix="/fs", tags=["namespace"])
files_router = APIRouter(prefix="/files", tags=["transferencia"])
internal_router = APIRouter(
    prefix="/internal/v1",
    tags=["interno"],
    dependencies=[Depends(require_internal_secret)],
)


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


# --- Namespace (RF1) -------------------------------------------------------


@fs_router.get("/ls")
def ls(path: str, uow: Uow, user: CurrentUser) -> LsResponse:
    entradas = namespace_queries.ls(uow, user.user_id, path)
    return LsResponse(
        entries=[
            LsEntry(name=e.name, type=e.type, size=e.size, created_at=e.created_at)
            for e in entradas
        ]
    )


@fs_router.get("/stat")
def stat(path: str, uow: Uow, user: CurrentUser) -> StatResponse:
    s = namespace_queries.stat(uow, user.user_id, path)
    return StatResponse(
        path=s.path,
        type=s.type,
        size=s.size,
        block_size=s.block_size,
        block_count=s.block_count,
        created_at=s.created_at,
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
            )
            for b in creado.blocks
        ],
    )


@files_router.post("/{file_id}/commit")
def commit_file(file_id: str, uow: Uow, user: CurrentUser) -> CommitResponse:
    confirmado = file_commands.commit_file(uow, user.user_id, file_id)
    return CommitResponse(
        path=confirmado.path, size=confirmado.size, block_count=confirmado.block_count
    )


@files_router.post("/{file_id}/abort", status_code=status.HTTP_204_NO_CONTENT)
def abort_file(file_id: str, uow: Uow, user: CurrentUser) -> Response:
    file_commands.abort_file(uow, user.user_id, file_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@files_router.get("/open")
def open_file(path: str, uow: Uow, user: CurrentUser) -> OpenFileResponse:
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


@internal_router.post("/datanodes/register")
def register_data_node(body: DataNodeRegisterRequest, uow: Uow) -> DataNodeRegisterResponse:
    data_node_id = internal_commands.register_data_node(
        uow, body.base_url, body.capacity_bytes
    )
    return DataNodeRegisterResponse(data_node_id=data_node_id)


@internal_router.post("/blocks/{block_id}/stored", status_code=status.HTTP_204_NO_CONTENT)
def block_stored(block_id: str, body: BlockStoredRequest, uow: Uow) -> Response:
    internal_commands.mark_block_stored(
        uow, block_id, body.data_node_id, body.size, body.checksum_sha256
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@internal_router.get("/gc/orphan-blocks")
def orphan_blocks(uow: Uow) -> OrphanBlocksResponse:
    return OrphanBlocksResponse(
        blocks=[
            OrphanBlock(
                block_id=b.block_id, size=b.size, replicas=_replicas(b.replicas)
            )
            for b in gc_queries.orphan_blocks(uow)
        ]
    )


@internal_router.post("/gc/confirm", status_code=status.HTTP_204_NO_CONTENT)
def gc_confirm(body: GcConfirmRequest, uow: Uow) -> Response:
    internal_commands.confirm_gc(uow, body.block_ids)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
