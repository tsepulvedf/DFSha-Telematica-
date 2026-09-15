"""Dominio del ControlNode: entidades, reglas del namespace y particion en bloques.

No importa FastAPI, SQLAlchemy ni httpx. Si algun dia lo hace, la separacion se rompio.
"""

from .entities import (
    Block,
    BlockReplica,
    DataNode,
    DataNodeState,
    Directory,
    File,
    FileState,
    ReplicaState,
    User,
    utcnow,
)
from .partition import BlockSpec, block_count_for, plan_blocks
from .path import Path
from .rules import (
    MoveDecision,
    MoveKind,
    decide_move,
    ensure_can_abort,
    ensure_can_commit,
    ensure_directory_is_empty,
    ensure_move_is_legal,
    ensure_name_is_free,
    ensure_visible,
)

__all__ = [
    "Block",
    "BlockReplica",
    "BlockSpec",
    "DataNode",
    "DataNodeState",
    "Directory",
    "File",
    "FileState",
    "MoveDecision",
    "MoveKind",
    "Path",
    "ReplicaState",
    "User",
    "block_count_for",
    "decide_move",
    "ensure_can_abort",
    "ensure_can_commit",
    "ensure_directory_is_empty",
    "ensure_move_is_legal",
    "ensure_name_is_free",
    "ensure_visible",
    "plan_blocks",
    "utcnow",
]
