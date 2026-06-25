# -*- coding: utf-8 -*-
"""角色扮演会话状态管理（向后兼容层）。

RolePlaySession 已合并到 AgentMemory 中。
此文件保留为 re-export，确保旧代码无需修改即可运行。
"""

from novel2comic.src.agent_memory import (
    CharacterState,
    ConversationTurn,
    RelationshipChange,
    RolePlayState as RolePlaySession,  # 兼容旧名
)

__all__ = [
    "CharacterState",
    "ConversationTurn",
    "RelationshipChange",
    "RolePlaySession",
]
