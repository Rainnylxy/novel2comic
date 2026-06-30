# -*- coding: utf-8 -*-
"""角色扮演 Agent V3 —— 心智引擎 + ReAct 循环。

身份转变:
  旧: Agent = 角色本人 (System: "你是江停，用这个身份说话")
  新: Agent = 角色心智引擎 (System: "你是引擎，管理江停的记忆/情感/边界")

核心循环: Thought → retrieve_memory/adjust_emotion/check_boundary → speak
"""

import json
from typing import TYPE_CHECKING

from agentflow.runtime.toolkit import tool

from .base_agent import BaseAgent
if TYPE_CHECKING:
    from ..context import GlobalContext, ServiceRegistry
    from ..llm import UnifiedLLM


def _read_novel_text(file_path: str) -> str:
    """读取小说原文（用于蒸馏）。"""
    try:
        from pathlib import Path
        return Path(file_path).read_text(encoding="utf-8")
    except Exception:
        return ""


class RolePlayAgent(BaseAgent):
    """角色扮演心智引擎。

    init_character(name, scenario) → 加载角色配置（非 Tool）
    Agent 通过 ReAct 循环自主管理: 记忆检索 → 情感调整 → 边界检查 → 回复
    """

    SKILL_NAME = "roleplay"

    def __init__(self, ctx, services, llm, memory=None):
        super().__init__(ctx, services, llm, memory)
        self._kg = services.kg

    @property
    def rp(self):
        return self._memory.roleplay

    # ================================================================
    # 角色初始化（外部调用，不是 Tool）
    # ================================================================

    def init_character(self, character_name: str, scenario: str = ""):
        """加载角色配置，设置心智引擎的 system prompt。

        替代旧版 start_conversation Tool。
        外部（cli.py）在 Agent 创建后、对话开始前调用。
        """
        graph = self._ctx.novel.story_graph if self._ctx.novel else None
        if not graph:
            self.set_identity(f"你是角色扮演引擎。当前扮演: {character_name}。")
            return

        person = self._kg.get_person(graph, character_name)
        if not person:
            self.set_identity(f"你是角色扮演引擎。当前扮演: {character_name}。")
            return

        rp = self.rp
        rp.active_character = character_name
        rp.story_timeline_point = person.first_appearance_chapter
        rp.active_location = scenario or "未知地点"

        # 知识过滤
        knowledge = rp.build_knowledge_filter(character_name, graph)
        rp.character_knowledge[character_name] = knowledge

        # 懒加载蒸馏 Profile
        char_profile = _ensure_char_profile(
            self._ctx, character_name, graph, self._llm,
        )
        if char_profile:
            rp.set_profile(
                sensitivity=char_profile.sensitivity.to_dict() if char_profile.sensitivity else None,
                recovery=char_profile.recovery.to_dict() if char_profile.recovery else None,
                baseline=char_profile.state.baseline if char_profile.state else None,
            )
        else:
            char_profile = None

        # 构建引擎 system prompt
        identity = self._build_engine_prompt(
            character_name, scenario, char_profile,
        )
        self.set_identity(identity)

    def switch_character(self, character_name: str):
        """切换到另一个角色（外部调用）。"""
        return self.init_character(character_name, self.rp.active_location)

    # ================================================================
    # 引擎 System Prompt 构建
    # ================================================================

    def _build_engine_prompt(self, character_name: str, scenario: str = "",
                             char_profile=None) -> str:
        """构建心智引擎的 system prompt。

        包含: 角色档案(KG) + Voice + Boundary + State + 引擎指令
        """
        graph = self._ctx.novel.story_graph if self._ctx.novel else None
        if not graph:
            return f"你是角色扮演引擎。当前扮演: {character_name}。"

        person = self._kg.get_person(graph, character_name)

        # 角色基础档案
        if person:
            prompt = (
                f"## 当前扮演角色: {character_name}\n\n"
                f"### 角色设定\n"
                f"- 身份: {person.role_type}\n"
                f"- 派系: {person.faction}\n"
                f"- 重要度: {person.importance}/10\n"
                f"- 状态: {person.status}\n"
                f"- 简介: {person.description}\n"
            )
        else:
            prompt = f"## 当前扮演角色: {character_name}\n\n"

        # 知识边界
        knowledge = self.rp.character_knowledge.get(character_name, "")
        if knowledge:
            prompt += f"\n{knowledge}\n"

        # 蒸馏 Profile
        if char_profile:
            prompt += self._format_profile_sections(char_profile)

        # 场景
        if scenario:
            prompt += f"\n当前场景: {scenario}\n"

        # 引擎指令
        prompt += """
## 引擎指令
你是角色的心智引擎。每次对话必须经过完整的 ReAct 循环:

1. **Thought**: 用角色的第一人称内心独白表达潜台词
   - 涉及过去 → 先调 retrieve_memory
   - 触发情绪 → 先调 adjust_emotion
   - 行为有风险 → 先调 check_boundary
2. **Action**: 调用工具
3. **Observation**: 读取结果
4. 重复 1-3 直到确认状态正确
5. **speak**: 以角色身份输出最终回复（必须是循环的最后一步）
"""
        return prompt

    # ================================================================
    # Profile 格式化（同 V2）
    # ================================================================

    def _format_profile_sections(self, char_profile) -> str:
        parts = []
        if char_profile.voice and char_profile.voice.summary:
            parts.append(self._format_voice_section(char_profile.voice))
        if char_profile.boundary:
            parts.append(self._format_boundary_section(char_profile.boundary))
        if char_profile.state and char_profile.state.baseline:
            parts.append(self._format_state_baseline_section(char_profile.state))
        return "\n\n" + "\n\n".join(parts) if parts else ""

    def _format_voice_section(self, voice) -> str:
        lines = ["### 表达风格 (Voice)"]
        if voice.summary:
            lines.append(f"- 风格: {voice.summary}")
        if voice.avg_sentence_length > 0:
            lines.append(
                f"- 句长: 平均 {voice.avg_sentence_length:.0f} 字 "
                f"(范围 {voice.sentence_range[0]}-{voice.sentence_range[1]})"
            )
        if voice.response_pattern:
            pattern_map = {
                "silence_first": "先沉默再开口", "direct": "说话直接",
                "counter_question": "习惯反问", "deflect": "习惯转移话题",
            }
            lines.append(f"- 回应: {pattern_map.get(voice.response_pattern, voice.response_pattern)}")
        if voice.taboo_words:
            lines.append(f"- 禁用词: {', '.join(voice.taboo_words)}")
        if voice.taboo_patterns:
            lines.append(f"- 禁用句式: {', '.join(voice.taboo_patterns)}")
        return "\n".join(lines)

    def _format_boundary_section(self, boundary) -> str:
        lines = ["### 行为边界 (Boundary)"]
        if boundary.hard_rules:
            lines.append("硬底线（绝不违反）:")
            for r in boundary.hard_rules:
                lines.append(f"  - {r}")
        if boundary.tendencies:
            lines.append("行为倾向:")
            for t in boundary.tendencies:
                lines.append(f"  - {t}")
        return "\n".join(lines)

    def _format_state_baseline_section(self, state) -> str:
        if not state.baseline:
            return ""
        items = [f"{k}:{v}" for k, v in state.baseline.items()]
        return "### 心理基线\n" + ", ".join(items)

    # ================================================================
    # 动态状态前缀（注入 user message，保持 system prompt 缓存）
    # ================================================================

    def _build_dynamic_prefix(self) -> str:
        rp = self.rp
        if not rp.runtime_state:
            return ""
        parts = []
        state_str = rp.format_state_for_prompt()
        if state_str:
            parts.append(state_str)
        emotion = rp.get_emotion_summary()
        if emotion and emotion != "情绪平稳":
            parts.append(f"情绪: {emotion}")
        if rp.active_location:
            parts.append(f"位置: {rp.active_location}")
        if not parts:
            return ""
        return "[当前状态] " + " | ".join(parts) + "\n\n"

    async def run(self, task: str):
        prefix = self._build_dynamic_prefix()
        if prefix:
            task = prefix + task
        return await super().run(task)

    # ================================================================
    # Tools: ReAct 循环的四个标准工具
    # ================================================================

    def _build_tools(self) -> list:
        ctx = self._ctx
        rp = self.rp

        @tool
        def retrieve_memory(query: str, aspect: str = "any") -> str:
            """从角色的知识边界内检索相关记忆。

            当用户提到过去的事件、人物或经历时，先调用此工具获取角色视角的记忆。

            Args:
                query: 检索关键词（如 "师父", "秘境试炼", "三年前"）
                aspect: 聚焦类型 — "event" | "person" | "emotion" | "any"

            Returns:
                角色视角内的相关记忆文本
            """
            char = rp.active_character
            if not char:
                return json.dumps({"status": "error", "message": "无活跃角色"})

            graph = ctx.novel.story_graph if ctx.novel else None
            if not graph:
                return json.dumps({"status": "error", "message": "KG 不可用"})

            results = []

            # 1. 查角色参与的事件
            events = graph.character_events(char) if graph else []
            if aspect in ("event", "any"):
                for ev in events:
                    name = ev.get("name", "")
                    summary = ev.get("summary", "")
                    if query.lower() in name.lower() or query.lower() in summary.lower():
                        results.append(
                            f"[事件] {name} (第{ev.get('chapter_start', '?')}章): {summary}"
                        )

            # 2. 查人际关系
            if aspect in ("person", "any") and graph:
                for edge in graph.relationship_edges:
                    if edge.from_char == char or edge.to_char == char:
                        target = edge.to_char if edge.from_char == char else edge.from_char
                        if query.lower() in target.lower():
                            results.append(
                                f"[关系] 与 {target}: {edge.relation_type} "
                                f"(亲密度:{edge.intimacy:+d})"
                                + (f" | {edge.shared_history}" if edge.shared_history else "")
                            )

            # 3. 查角色知识缓存
            kg_text = rp.character_knowledge.get(char, "")
            if kg_text and query.lower() in kg_text.lower():
                lines = kg_text.split("\n")
                for i, line in enumerate(lines):
                    if query.lower() in line.lower():
                        snippet = "\n".join(lines[max(0, i-1):i+3])
                        results.append(f"[知识] ...{snippet[:300]}...")
                        break

            if not results:
                return json.dumps({
                    "status": "ok",
                    "query": query,
                    "results": f"角色的记忆中没有找到与「{query}」直接相关的内容。"
                               f"角色对此事可能记忆模糊，或此事不在角色的经历中。",
                }, ensure_ascii=False)

            return json.dumps({
                "status": "ok",
                "query": query,
                "results": "\n".join(results[:5]),
            }, ensure_ascii=False)

        @tool
        def adjust_emotion(trigger: str, intensity: float = 0.5) -> str:
            """调整角色的情感状态。

            当对话内容触发了角色的情绪反应时调用。
            系统会基于角色的敏感度系数计算实际变化量。

            Args:
                trigger: 触发事件描述（如 "被提及痛苦往事", "感受到威胁", "收到真诚关心"）
                intensity: 主观感受强度 0.0-1.0（默认 0.5）

            Returns:
                情感变化摘要
            """
            char = rp.active_character
            if not char or not rp.sensitivity_profile:
                return json.dumps({"status": "ok", "message": "无 sensitiviy profile"})

            deltas = rp.apply_event(trigger, intensity)

            emotion = rp.get_emotion_summary()

            return json.dumps({
                "status": "ok",
                "trigger": trigger,
                "intensity": intensity,
                "changes": deltas,
                "current_state": rp.runtime_state,
                "emotion_summary": emotion,
            }, ensure_ascii=False)

        @tool
        def check_boundary(intended_action: str) -> str:
            """检查角色打算做的行为是否触犯硬底线。

            在角色打算说/做可能有风险的事情前调用。

            Args:
                intended_action: 打算做的事或说的话的描述

            Returns:
                "ok" 或违规警告
            """
            char_profile = ctx.character_profiles.get(rp.active_character)
            if not char_profile or not char_profile.boundary:
                return json.dumps({"status": "ok"})

            boundary = char_profile.boundary
            warnings = []

            # 简单关键词匹配检查硬底线
            for rule in boundary.hard_rules:
                rule_keywords = rule.replace("不会", "").replace("绝不", "")
                if any(kw in intended_action for kw in rule_keywords.split("、")):
                    warnings.append(f"⚠ 触犯底线: {rule}")

            if warnings:
                return json.dumps({
                    "status": "warn",
                    "warnings": warnings,
                    "suggestion": "请调整行为，避免触犯角色的硬底线。考虑角色的行为倾向作为替代。",
                }, ensure_ascii=False)

            return json.dumps({"status": "ok"})

        @tool
        def speak(text: str) -> str:
            """以角色身份输出最终回复。

            必须是 ReAct 循环的最后一步。在此之前确保:
            - retrieve_memory 已调用（如果涉及过去）
            - adjust_emotion 已调用（如果有情绪触发）
            - check_boundary 已调用（如果行为有风险）

            Args:
                text: 角色说的台词（可含动作描写如 "(握紧拳头)……我知道了。"）

            Returns:
                角色的最终回复文本
            """
            # speak 不做加工，直接返回。Agent 可以通过 Observation 看到。
            return text

        return [
            retrieve_memory,
            adjust_emotion,
            check_boundary,
            speak,
        ]


def _ensure_char_profile(ctx: "GlobalContext", name: str, graph, llm: "UnifiedLLM"):
    """懒加载角色蒸馏 Profile。"""
    profile = ctx.character_profiles.get(name)
    if profile:
        return profile
    if ctx.novel and ctx.novel.file_path:
        novel_text = _read_novel_text(ctx.novel.file_path)
        if novel_text:
            try:
                from ..character_distiller import CharacterDistiller
                distiller = CharacterDistiller(llm, ctx.services.kg)
                profile = distiller.distill_character(name, novel_text, graph)
                ctx.character_profiles[name] = profile
            except Exception:
                pass
    return profile
