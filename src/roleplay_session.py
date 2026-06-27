# -*- coding: utf-8 -*-
"""角色扮演会话状态管理（向后兼容层）。

RolePlaySession 已精简合并到 AgentMemory.RolePlayState 中。
此文件保留为 re-export，确保旧代码无需修改即可运行。
"""

from novel2comic.src.agent_memory import RolePlayState as RolePlaySession

__all__ = ["RolePlaySession"]
