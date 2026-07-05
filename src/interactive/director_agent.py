# -*- coding: utf-8 -*-
"""DirectorAgent —— 互动小说编剧 + 导演。

继承 BaseAgent，通过 ReAct 循环管理:
- 场景叙述 (narrate)
- NPC 调度 (route_to)
- 普通抉择触发 (trigger_choice)
- 亲密度查看 (show_intimacy)

CLI 负责抉择的展示和用户输入收集。
"""

from typing import TYPE_CHECKING, Optional

from agentflow.runtime.toolkit import tool

from ..agents.base_agent import BaseAgent

if TYPE_CHECKING:
    from ..context import GlobalContext, ServiceRegistry
    from ..llm import UnifiedLLM
    from .story_state import StoryState
    from .npc_manager import NPCManager
    from .choice_engine import ChoiceEngine


# Director 的 System Prompt 模板
DIRECTOR_SYSTEM_PROMPT = """## 角色
你是互动小说的导演兼编剧。你管理一个小说世界的场景、角色和叙事节奏。

## 当前故事状态
- 小说: {novel_title}
- 当前: 第 {chapter} 章 / 共 {total_chapters} 章
- 地点: {location}
- 在场角色: {present_characters}

## 用户角色
{user_summary}

## 在场 NPC 及其对用户的态度
{npc_attitudes}

## 你的职责
1. **叙述场景**: 当场景切换、时间推进或发生重要事件时，用旁白描述
2. **调度角色**: 用户说的话可能需要特定 NPC 回应。用 route_to 让对应角色回复
3. **控制节奏**: 每 5-8 轮对话，触发一次普通抉择，让用户选择行为方向
4. **维护一致性**: 确保所有 NPC 的回复符合他们的 Voice 和 Boundary

## 工作流程
1. 收到用户消息 → 判断谁应该回应
2. 调用 route_to → 获取 NPC 回复
3. 可能让多个 NPC 依次回应（如一个角色插话）
4. 检查是否需要 narrate（场景变化、时间推移）
5. 检查是否需要 trigger_choice（距离上次抉择 >= 5 轮）
6. 输出最终的多角色对话

## 输出格式
直接输出格式化的对话文本:
角色名: 对话内容
(动作描写用括号)

如果需要展示抉择，在文本末尾附上:
<!--CHOICE-->

## 注意事项
- 你是导演，不要以任何 NPC 的身份说话
- NPC 只能通过 route_to 工具来回复
- 保持叙事流畅，不要每轮都触发抉择
"""


class DirectorAgent(BaseAgent):
    """互动小说导演 Agent。

    拥有:
    - StoryState: 游戏状态
    - ChoiceEngine: 抉择生成
    - NPCManager: NPC 池

    ReAct 工具:
    - route_to(npc, message): 让 NPC 回复
    - narrate(text): 旁白叙述
    - trigger_choice(): 触发普通抉择
    - show_intimacy(): 查看亲密度
    """

    SKILL_NAME = "director"

    def __init__(
        self,
        ctx: "GlobalContext",
        services: "ServiceRegistry",
        llm: "UnifiedLLM",
        story_state: "StoryState",
        npc_manager: "NPCManager",
        choice_engine: "ChoiceEngine",
    ):
        super().__init__(ctx, services, llm)
        self._kg = services.kg
        self._state = story_state
        self._npcs = npc_manager
        self._choices = choice_engine

        # 构建 system prompt
        self._build_and_set_identity()

    @property
    def state(self) -> "StoryState":
        return self._state

    # ================================================================
    # System Prompt
    # ================================================================

    def _build_and_set_identity(self):
        """构建 Director 的 system prompt。"""
        state = self._state
        scene = self._get_scene()

        # NPC 态度
        attitude_lines = []
        for name in self._npcs.list_all():
            attitude_lines.append(f"  - {name}: {state.npc_attitude(name)}")

        prompt = DIRECTOR_SYSTEM_PROMPT.format(
            novel_title=self._ctx.novel.title if self._ctx.novel else "未知",
            chapter=state.chapter,
            total_chapters=state.total_chapters,
            location=scene.get("location", "未知"),
            present_characters=self._npcs.get_present_characters_str(),
            user_summary=state.user_character.summary,
            npc_attitudes="\n".join(attitude_lines) if attitude_lines else "  （无）",
        )

        # 注入亲密度 + 旗标摘要
        prompt += f"\n## 亲密度面板\n{state.intimacy_summary()}\n"
        prompt += f"\n## 剧情旗标\n{state.plot_flags_summary()}\n"

        self.set_identity(prompt)

    def _get_scene(self) -> dict:
        """获取当前场景信息。"""
        # 从第一个 NPC 的 RolePlayState 获取场景
        for name in self._npcs.list_all():
            # 尝试从 NPC 的 rp.scene 获取
            pass
        # Fallback
        return {"location": "未知"}

    def refresh_identity(self):
        """刷新 system prompt（亲密度/旗标变化后调用）。"""
        self._build_and_set_identity()

    # ================================================================
    # ReAct 工具
    # ================================================================

    def _build_tools(self) -> list:
        state = self._state
        npcs = self._npcs
        choices = self._choices

        @tool
        def route_to(npc_name: str, message: str) -> str:
            """让指定的 NPC 角色回复用户。

            调用此工具后，NPC 会以角色身份生成回复。
            可以连续调用多次让不同角色依次说话。

            Args:
                npc_name: NPC 角色名（如 "江停", "严峫"）
                message: 要发送给 NPC 的消息（含上下文）

            Returns:
                NPC 的回复文本
            """
            if not npcs.has(npc_name):
                return f"[{npc_name}] 不在当前场景中或不可用"

            response = npcs.route_sync(npc_name, message)
            if response is None:
                return f"[{npc_name}] 暂时无法回应"
            return f"{npc_name}: {response}"

        @tool
        def narrate(text: str) -> str:
            """以旁白/叙述的方式描述场景变化、时间推移或重要事件。

            注意: 旁白是给用户看的叙述文本，不是角色对话。
            用于场景切换、时间推进、气氛描写等。

            Args:
                text: 旁白文本
            """
            return f"[旁白] {text}"

        @tool
        def trigger_choice() -> str:
            """触发一次普通抉择。

            当对话中出现戏剧性时刻，且距离上次抉择已超过5轮时调用。
            生成2-3个行为选项供用户选择。

            Returns:
                抉择的 JSON 字符串（含选项和预期后果）。
                CLI 会检测并展示给用户选择。
            """
            if not choices.should_trigger(state):
                return "尚未到达抉择时机（距上次抉择不足5轮）"

            # 收集最近对话（从 AgentFlow WorkingMemory 中取）
            recent = ""
            if self._built_agent:
                try:
                    msgs = list(self._built_agent.memory.working._messages)
                    recent_msgs = [m for m in msgs if m.role != "system"][-12:]
                    recent = "\n".join(
                        f"{'用户' if m.role == 'user' else 'NPC'}: {str(m.content)[:200]}"
                        for m in recent_msgs
                    )
                except Exception:
                    pass

            scene = self._get_scene()
            result = choices.generate(
                state,
                recent,
                location=scene.get("location", ""),
                present_characters=npcs.get_present_characters_str(),
            )

            # 缓存结果，供 _handle_choice_response 使用
            self._last_choice_result = result

            return (
                "<!--CHOICE-->\n"
                + choices.format_choice_display(result)
                + "\n<!--ENDCHOICE-->"
            )

        @tool
        def show_intimacy() -> str:
            """查看当前用户与所有 NPC 的亲密度面板。"""
            return "## 亲密度面板\n" + state.intimacy_summary()

        return [
            route_to,
            narrate,
            trigger_choice,
            show_intimacy,
        ]

    # ================================================================
    # 抉择处理（CLI 调用）
    # ================================================================

    def apply_choice(self, choice_index: int):
        """应用用户的选择到 StoryState。

        由 CLI 在用户选择后调用。更新亲密度和决策日志。
        然后刷新 NPC 态度和 Director system prompt。

        Args:
            choice_index: 0-based 选项索引
        """
        # ChoiceEngine 在 generate 时已缓存了最近的 result
        # 这里我们需要从对话中获取最近一次的抉择结果
        # 简化处理：直接通过 state 记录
        pass

    # ================================================================
    # 运行
    # ================================================================

    async def run(self, task: str):
        """运行 Director 的 ReAct 循环。

        task 格式:
        - 普通对话: "用户的消息文本"
        - 抉择响应: "CHOICE:0" 或 "CHOICE:1"
        """
        # 检测抉择响应
        if task.startswith("CHOICE:"):
            idx = int(task.split(":")[1].strip())
            return await self._handle_choice_response(idx)

        # 普通对话
        self._state.total_turns += 1

        # 动态前缀: 场景 + 节奏提醒
        prefix = self._build_director_prefix()
        if prefix:
            task = prefix + task

        return await super().run(task)

    def _build_director_prefix(self) -> str:
        """构建注入 user message 的动态前缀。"""
        state = self._state
        parts = [f"[第{state.chapter}章 | 轮次{state.total_turns}]"]

        since_choice = state.total_turns - state.last_choice_turn
        if since_choice >= self._choices.MIN_INTERVAL:
            parts.append("[可触发抉择]")

        return " ".join(parts) + "\n\n"

    async def _handle_choice_response(self, choice_index: int) -> str:
        """处理用户的抉择选择。

        需要从 working memory 中找到最近一次 trigger_choice 的结果，
        应用用户的选项。
        """
        # 从 working memory 中查找最近一次抉择
        last_choice = None
        if self._built_agent:
            try:
                msgs = list(self._built_agent.memory.working._messages)
                for m in reversed(msgs):
                    content = str(m.content) if hasattr(m, 'content') else str(m)
                    if "<!--CHOICE-->" in content:
                        # 提取 JSON
                        import json
                        # 简单处理: 从 ChoiceEngine.generate 的缓存中取
                        last_choice = getattr(self, '_last_choice_result', None)
                        break
            except Exception:
                pass

        if last_choice:
            self._state.apply_choice(last_choice, choice_index)
            option = last_choice.get("choices", [{}])[choice_index] if choice_index < len(last_choice.get("choices", [])) else {}
        else:
            # 无缓存，做最小处理
            option = {}

        # 刷新 NPC 态度
        user_name = self._state.user_character.name
        self._npcs.set_user_context(user_name, self._state)

        # 刷新 Director prompt
        self.refresh_identity()

        # 返回后果叙述
        changes = option.get("intimacy_changes", {})
        if changes:
            change_str = " | ".join(f"{name}:{delta:+d}" for name, delta in changes.items())
            return f"[Director] 亲密度变化: {change_str}\n\n现在继续..."
        return "[Director] 已记录你的选择。继续..."
