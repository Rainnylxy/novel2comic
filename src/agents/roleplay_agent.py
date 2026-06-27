# -*- coding: utf-8 -*-
"""角色扮演 Agent V2 —— 固定身份 system prompt。

start_conversation("江停") 之后：
- system prompt 固定为 "你是江停。角色档案 + 人际关系 + 对话原则"
- WorkingMemory 保留对话历史
- Agent 直接以角色身份回复，不需要 respond 工具
"""

import json
from typing import TYPE_CHECKING

from agentflow.runtime.toolkit import tool

from novel2comic.src.agents.base_agent import BaseAgent
from novel2comic.src.prompt_context import PromptNeed

if TYPE_CHECKING:
    from novel2comic.src.context import GlobalContext, ServiceRegistry
    from novel2comic.src.llm import UnifiedLLM


def _read_novel_text(file_path: str) -> str:
    """读取小说原文（用于蒸馏）。"""
    try:
        from pathlib import Path
        return Path(file_path).read_text(encoding="utf-8")
    except Exception:
        return ""


class RolePlayAgent(BaseAgent):
    """角色扮演 Agent。

    核心设计：start_conversation 后，system prompt 固定为角色身份。
    Agent 自己就是角色，直接对话，无需 respond 工具。
    """

    SKILL_NAME = "roleplay"

    def __init__(self, ctx, services, llm, memory=None):
        super().__init__(ctx, services, llm, memory)
        self._kg = services.kg

    @property
    def rp(self):
        return self._memory.roleplay

    # ================================================================
    # 构建角色身份 Prompt
    # ================================================================

    def _build_identity_prompt(self, character_name: str, scenario: str = "",
                               char_profile=None) -> str:
        """从 KG + 蒸馏 Profile 构建固定角色身份 system prompt。

        这条 prompt 会被设置为 Agent 的 system prompt，
        之后所有对话都基于这个身份。

        Args:
            character_name: 角色名
            scenario: 场景描述
            char_profile: CharacterProfile 蒸馏定义（可选）
        """
        graph = self._ctx.novel.story_graph if self._ctx.novel else None
        if not graph:
            return f"你是 {character_name}。请以这个角色的身份对话。"

        person = self._kg.get_person(graph, character_name)

        # 角色基础档案
        if person:
            prompt_text = (
                f"你是 {character_name}。\n\n"
                f"## 角色设定\n"
                f"- 身份: {person.role_type}\n"
                f"- 派系: {person.faction}\n"
                f"- 重要度: {person.importance}/10\n"
                f"- 状态: {person.status}\n"
                f"- 首次出场: 第{person.first_appearance_chapter}章\n"
                f"- 简介: {person.description}\n"
            )
        else:
            prompt_text = f"你是 {character_name}。\n\n"

        # 知识边界（角色视角，含人际关系）
        knowledge = self.rp.build_knowledge_filter(character_name, graph)
        if knowledge:
            prompt_text += f"\n{knowledge}\n"

        # 场景
        scene_text = f"\n当前场景: {scenario}" if scenario else ""

        # 世界观
        novel_name = self._ctx.novel.title if self._ctx.novel else "未知"
        world = f"\n\n世界观: {novel_name}。请保持符合这个世界观的说话方式。"

        # 蒸馏 Profile 增强（Voice + Boundary + State）
        profile_text = ""
        if char_profile:
            profile_text = self._format_profile_sections(char_profile)

        # 对话原则
        rules = """
## 对话原则
1. 用角色的性格、口癖、语言习惯说话
2. 对关系亲近和关系疏远的人态度不同
3. 不知道的事就说不知道——你只知道自己的经历
4. 情感反应真实——该生气生气，该温柔温柔
5. 动作描写用括号: (苏墨握紧了手中的剑)
6. 不要跳出角色解释或评价自己"""

        return prompt_text + scene_text + world + profile_text + rules

    # ================================================================
    # Profile 格式化辅助方法
    # ================================================================

    def _format_profile_sections(self, char_profile) -> str:
        """将 CharacterProfile 格式化为 identity prompt 的增强部分。"""
        parts = []

        if char_profile.voice and char_profile.voice.summary:
            parts.append(self._format_voice_section(char_profile.voice))
        if char_profile.boundary:
            parts.append(self._format_boundary_section(char_profile.boundary))
        if char_profile.state and char_profile.state.baseline:
            parts.append(self._format_state_section(char_profile))

        return "\n\n" + "\n\n".join(parts) if parts else ""

    def _format_voice_section(self, voice) -> str:
        """格式化 Voice Profile 为 prompt 文本。"""
        lines = ["## 表达风格（Voice）"]

        if voice.summary:
            lines.append(f"- 风格: {voice.summary}")
        if voice.avg_sentence_length > 0:
            lines.append(
                f"- 句长: 平均 {voice.avg_sentence_length:.0f} 字 "
                f"(范围 {voice.sentence_range[0]}-{voice.sentence_range[1]})"
            )
        if voice.response_pattern:
            pattern_map = {
                "silence_first": "习惯先沉默再开口",
                "direct": "说话直接",
                "counter_question": "习惯反问",
                "deflect": "习惯转移话题",
            }
            label = pattern_map.get(voice.response_pattern, voice.response_pattern)
            lines.append(f"- 回应模式: {label}")
        if voice.rhythm:
            rhythm_map = {
                "initiator": "主动开启话题",
                "responder": "被动回应居多",
            }
            label = rhythm_map.get(voice.rhythm, voice.rhythm)
            lines.append(f"- 对话节奏: {label}")

        tone_parts = []
        if voice.tone_cold_warm > 0:
            tone_parts.append(f"冷暖: {voice.tone_cold_warm:.1f}")
        if voice.tone_hard_soft > 0:
            tone_parts.append(f"软硬: {voice.tone_hard_soft:.1f}")
        if tone_parts:
            lines.append(f"- 语气光谱: {', '.join(tone_parts)}")

        if voice.taboo_words:
            lines.append(f"- 绝不会用的词: {', '.join(voice.taboo_words)}")
        if voice.taboo_patterns:
            lines.append(f"- 绝不会用的句式: {', '.join(voice.taboo_patterns)}")

        if voice.voice_shift and voice.voice_shift.get("notes"):
            lines.append(f"- 表达差异: {voice.voice_shift['notes']}")

        return "\n".join(lines)

    def _format_boundary_section(self, boundary) -> str:
        """格式化 Boundary Profile 为 prompt 文本。"""
        lines = ["## 行为边界（Boundary）"]

        if boundary.hard_rules:
            lines.append("以下行为绝不能做（硬底线）:")
            for rule in boundary.hard_rules:
                lines.append(f"  - {rule}")

        if boundary.tendencies:
            lines.append("通常的行为倾向:")
            for t in boundary.tendencies:
                lines.append(f"  - {t}")

        return "\n".join(lines)

    def _format_state_section(self, char_profile) -> str:
        """格式化 State Profile 为 prompt 文本。

        只包含静态 baseline，不包含运行时动态状态。
        动态状态通过 _build_dynamic_prefix() 注入 user message。
        """
        state = char_profile.state
        if not state or not state.baseline:
            return ""

        lines = ["## 心理基线（State Baseline）"]
        items = [f"{k}:{v}" for k, v in state.baseline.items()]
        lines.append(", ".join(items))
        lines.append("（当前状态值会在对话中动态变化，见每条消息头部的 [当前状态]）")

        return "\n".join(lines)

    # ================================================================
    # 动态状态注入（不破坏 system prompt 缓存）
    # ================================================================

    def _build_dynamic_prefix(self) -> str:
        """构建动态状态前缀，注入到 user message 头部。

        system prompt 保持静态可缓存，
        运行时变化的 state/emotion 挂在这里，每轮随 user message 发送。
        """
        rp = self.rp
        if not rp.runtime_state:
            return ""

        parts = []
        # 量化状态
        state_str = rp.format_state_for_prompt()
        if state_str:
            parts.append(state_str)
        # 情绪描述
        emotion = rp.get_emotion_summary()
        if emotion and emotion != "情绪平稳":
            parts.append(f"情绪: {emotion}")
        # 位置
        if rp.active_location:
            parts.append(f"位置: {rp.active_location}")

        if not parts:
            return ""

        return "[当前状态] " + " | ".join(parts) + "\n\n"

    async def run(self, task: str):
        """运行 agent，自动在用户消息前注入动态状态。

        覆盖基类方法：将动态 state 前缀拼到 task 前，
        system prompt 不变 → LLM 可缓存头部。
        """
        prefix = self._build_dynamic_prefix()
        if prefix:
            task = prefix + task
        return await super().run(task)

    # ================================================================
    # 工具（3 个，不包含 respond）
    # ================================================================

    def _build_tools(self) -> list:
        ctx = self._ctx
        kg = self._kg
        llm = self._llm
        build_prompt = self._build_prompt
        rp = self.rp
        agent_ref = self  # 闭包捕获 self 引用

        def _ensure_char_profile(c, graph, agent):
            """懒加载角色蒸馏 Profile。已在缓存中则直接返回。"""
            profile = ctx.character_profiles.get(c)
            if profile:
                return profile
            # 尝试按需蒸馏
            if ctx.novel and ctx.novel.file_path:
                novel_text = _read_novel_text(ctx.novel.file_path)
                if novel_text:
                    try:
                        from novel2comic.src.character_distiller import CharacterDistiller
                        distiller = CharacterDistiller(agent._llm, ctx.services.kg)
                        profile = distiller.distill_character(c, novel_text, graph)
                        ctx.character_profiles[c] = profile
                    except Exception:
                        pass
            return profile

        @tool
        def start_conversation(character_name: str, scenario: str = "") -> str:
            """以指定角色身份开始对话。

            设置固定的 system prompt 为角色身份。
            之后所有对话中，Agent 就是该角色。

            Args:
                character_name: 角色名
                scenario: 对话场景（如"在长安城的茶楼里"）
            """
            graph = ctx.novel.story_graph if ctx.novel else None
            if not graph:
                return json.dumps({"error": "请先加载小说"})

            person = kg.get_person(graph, character_name)
            if not person:
                return json.dumps({
                    "error": f"角色「{character_name}」不存在于知识图谱中",
                })

            # 设置角色扮演状态
            rp.active_character = character_name
            rp.story_timeline_point = person.first_appearance_chapter
            rp.active_location = scenario or "未知地点"

            # 加载知识边界
            knowledge = rp.build_knowledge_filter(character_name, graph)
            rp.character_knowledge[character_name] = knowledge

            # 加载蒸馏 Profile（懒加载：首次对话时按需蒸馏）
            char_profile = _ensure_char_profile(
                ctx, character_name, graph, agent_ref,
            )
            if char_profile:
                rp.set_profile(
                    sensitivity=char_profile.sensitivity.to_dict() if char_profile.sensitivity else None,
                    recovery=char_profile.recovery.to_dict() if char_profile.recovery else None,
                    baseline=char_profile.state.baseline if char_profile.state else None,
                )

            # 构建并设置固定身份 prompt
            identity = agent_ref._build_identity_prompt(character_name, scenario, char_profile)
            agent_ref.set_identity(identity)

            return json.dumps({
                "status": "ok",
                "character": character_name,
                "role": person.role_type,
                "scenario": scenario,
                "message": f"身份已设定。你现在是 {character_name}。请以角色的身份开始对话。",
            }, ensure_ascii=False)

        @tool
        def switch_character(character_name: str) -> str:
            """切换到另一个角色。

            更新固定身份 prompt，保留之前的对话上下文。

            Args:
                character_name: 要切换到的角色名
            """
            graph = ctx.novel.story_graph if ctx.novel else None
            if not graph:
                return json.dumps({"error": "请先加载小说"})

            old = rp.active_character

            person = kg.get_person(graph, character_name)
            if not person:
                return json.dumps({"error": f"角色「{character_name}」不存在"})

            # 更新状态
            rp.active_character = character_name
            rp.story_timeline_point = person.first_appearance_chapter
            if not rp.active_location:
                rp.active_location = "未知地点"

            # 加载新角色的知识
            if character_name not in rp.character_knowledge:
                rp.character_knowledge[character_name] = rp.build_knowledge_filter(character_name, graph)

            # 加载蒸馏 Profile
            char_profile = ctx.character_profiles.get(character_name)
            if char_profile:
                rp.set_profile(
                    sensitivity=char_profile.sensitivity.to_dict() if char_profile.sensitivity else None,
                    recovery=char_profile.recovery.to_dict() if char_profile.recovery else None,
                    baseline=char_profile.state.baseline if char_profile.state else None,
                )

            # 切换身份 prompt
            identity = agent_ref._build_identity_prompt(character_name, rp.active_location, char_profile)
            agent_ref.set_identity(identity)

            return json.dumps({
                "status": "ok",
                "previous": old,
                "current": character_name,
                "message": f"身份已切换。你现在是 {character_name}。",
            }, ensure_ascii=False)

        @tool
        def reflect_on_relationship(target_name: str) -> str:
            """以当前角色身份，表达对另一角色的主观看法。

            基于 KG 中的关系数据 + 对话历史中的情感变化。

            Args:
                target_name: 目标角色名
            """
            char_name = rp.active_character
            if not char_name:
                return json.dumps({"error": "没有活跃角色。请先调用 start_conversation"})

            graph = ctx.novel.story_graph if ctx.novel else None
            rel_data = None
            if graph:
                edge = graph.get_relationship_edge(char_name, target_name)
                if edge:
                    rel_data = {
                        "type": edge.relation_type, "intimacy": edge.intimacy,
                        "tension": edge.current_tension, "power": edge.power_dynamic,
                        "public": edge.public_knowledge, "history": edge.shared_history,
                    }

            # 取 State 历史作为情感变化参考
            recent_changes = rp.state_history[-5:]

            result = llm.chat_json(**build_prompt(PromptNeed.of(
                "roleplay_reflect",
                inputs={
                    "character_name": char_name,
                    "mood": rp.get_emotion_summary(),
                    "target_name": target_name,
                    "relationship_data": json.dumps(rel_data, ensure_ascii=False) if rel_data else f"你和 {target_name} 没有直接的已知关系。",
                    "recent_changes": json.dumps(recent_changes, ensure_ascii=False) if recent_changes else "无",
                },
            )).__dict__)

            reflection = result.get("reflection", "")

            return json.dumps({
                "status": "ok",
                "character": char_name,
                "target": target_name,
                "reflection": reflection,
                "relationship_data": rel_data,
            }, ensure_ascii=False)

        @tool
        def advance_scenario(event_description: str) -> str:
            """推进剧情场景。角色对发生的事件做出自主反应。

            Args:
                event_description: 事件描述（"门外突然传来急促的脚步声"）
            """
            char_name = rp.active_character
            if not char_name:
                return json.dumps({"error": "没有活跃角色"})

            # 从 AgentFlow WorkingMemory 取对话上下文
            recent = agent_ref._get_conversation_context(max_turns=10)

            # 应用事件到 runtime_state
            rp.apply_event(event_description, intensity=0.8)

            result = llm.chat_json(**build_prompt(PromptNeed.of(
                "roleplay_advance",
                inputs={
                    "character_name": char_name,
                    "mood": rp.get_emotion_summary(),
                    "location": rp.active_location,
                    "goals": ", ".join(rp.active_goals) if rp.active_goals else "无",
                    "recent_history": recent,
                    "event_description": event_description,
                    "current_state": rp.format_state_for_prompt(),
                },
            )).__dict__)

            reaction = result.get("reaction", "")
            dialogue = result.get("dialogue", "")
            action = result.get("action_taken", "")

            return json.dumps({
                "status": "ok",
                "character": char_name,
                "reaction": reaction,
                "dialogue": dialogue,
                "action_taken": action,
                "emotion": rp.get_emotion_summary(),
            }, ensure_ascii=False)

        return [
            start_conversation,
            switch_character,
            reflect_on_relationship,
            advance_scenario,
        ]
