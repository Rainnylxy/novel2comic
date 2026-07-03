# -*- coding: utf-8 -*-
"""NPCManager —— NPC 角色池 + 亲密度注入。

管理多个 RolePlayAgent 实例。
每个 NPC 的 system prompt 中融入当前亲密度对用户的态度。
"""

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ..context import GlobalContext, ServiceRegistry
    from ..llm import UnifiedLLM
    from ..agents.roleplay_agent import RolePlayAgent
    from .story_state import StoryState


class NPCManager:
    """管理 NPC RolePlayAgent 池。

    用法:
        npcs = NPCManager(ctx, services, llm, ["江停", "严峫"], start_chapter=24)
        npcs.set_user_context(user_name, intimacy_score)
        response = await npcs.route("江停", "你好")
    """

    def __init__(
        self,
        ctx: "GlobalContext",
        services: "ServiceRegistry",
        llm: "UnifiedLLM",
        npc_names: list[str],
        start_chapter: int = 0,
    ):
        self._ctx = ctx
        self._services = services
        self._llm = llm
        self._npc_names = list(npc_names)
        self._chapter = start_chapter

        # 构建 NPC Agent
        self._agents: dict[str, "RolePlayAgent"] = {}
        self._build_agents()

    def _build_agents(self):
        """为每个 NPC 构建 RolePlayAgent。"""
        from ..agents.roleplay_agent import RolePlayAgent

        for name in self._npc_names:
            agent = RolePlayAgent(self._ctx, self._services, self._llm)
            try:
                agent.init_character(name, start_chapter=self._chapter)
            except Exception:
                # KG 中没有该角色或初始化失败 → 跳过
                continue
            self._agents[name] = agent

    def set_user_context(self, user_name: str, state: "StoryState"):
        """根据 StoryState 更新所有 NPC 对用户的态度。

        每个 NPC 的 system prompt 末尾注入亲密度上下文。
        """
        for name, agent in self._agents.items():
            attitude = state.npc_attitude(name)
            agent.set_npc_mode(user_name, attitude)

    def has(self, name: str) -> bool:
        return name in self._agents

    def list_all(self) -> list[str]:
        return list(self._agents.keys())

    async def route(self, npc_name: str, message: str) -> Optional[str]:
        """向指定 NPC 发送消息并获取回复。

        Args:
            npc_name: NPC 名称
            message: 要发送的消息（已包含上下文）

        Returns:
            NPC 的回复文本，或 None（NPC 不可用）
        """
        agent = self._agents.get(npc_name)
        if not agent:
            return None
        try:
            return await agent.run(message)
        except Exception:
            return None

    def route_sync(self, npc_name: str, message: str) -> Optional[str]:
        """同步版本的 route（供 Director 的工具函数使用）。"""
        import asyncio
        agent = self._agents.get(npc_name)
        if not agent:
            return None
        try:
            # 尝试获取或创建事件循环
            try:
                loop = asyncio.get_running_loop()
                # 如果在运行中的事件循环中，需要特殊处理
                import concurrent.futures
                future = asyncio.run_coroutine_threadsafe(
                    agent.run(message), loop
                )
                return future.result(timeout=60)
            except RuntimeError:
                # 没有运行中的事件循环，创建新的
                return asyncio.run(agent.run(message))
        except Exception:
            return None

    def add_npc(self, name: str):
        """动态添加 NPC。"""
        if name in self._agents:
            return
        from ..agents.roleplay_agent import RolePlayAgent
        agent = RolePlayAgent(self._ctx, self._services, self._llm)
        try:
            agent.init_character(name, start_chapter=self._chapter)
        except Exception:
            return
        self._agents[name] = agent
        self._npc_names.append(name)

    def remove_npc(self, name: str):
        """移除 NPC。"""
        self._agents.pop(name, None)
        if name in self._npc_names:
            self._npc_names.remove(name)

    def get_present_characters_str(self) -> str:
        """获取在场角色列表字符串。"""
        return ", ".join(self._npc_names) if self._npc_names else "无"
