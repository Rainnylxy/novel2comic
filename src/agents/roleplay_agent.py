# -*- coding: utf-8 -*-
"""角色扮演 Agent V3 —— 心智引擎 + ReAct 循环。

身份转变:
  旧: Agent = 角色本人 (System: "你是江停，用这个身份说话")
  新: Agent = 角色心智引擎 (System: "你是引擎，管理江停的记忆/情感/边界")

核心循环: Thought → retrieve_memory/adjust_emotion/check_boundary → 直接输出对话（自然终止）
"""

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


# ============================================================
# 中文语义匹配工具
# ============================================================

def _extract_ngrams(text: str):
    """提取文本的 unigrams 和 bigrams（字符级）。"""
    chars = list(text)
    unigrams = set(chars)
    bigrams = {chars[i] + chars[i + 1] for i in range(len(chars) - 1)}
    return unigrams | bigrams


def _score_relevance(query: str, text: str) -> float:
    """计算 query 与 text 的中文语义相关度。

    使用 containment + Jaccard 混合评分：
    - containment: query 的 n-grams 有多少比例在 text 中出现（主导，权重 0.7）
    - Jaccard: 双向相似度（辅助，权重 0.3）
    - 完整子串出现额外加分

    这样短查询（2-4字）不会因为分母过大而被淹没。

    Returns:
        0.0 ~ 1.0 的相似度分数
    """
    if not query or not text:
        return 0.0

    q_ngrams = _extract_ngrams(query)
    t_ngrams = _extract_ngrams(text)

    if not q_ngrams or not t_ngrams:
        return 0.0

    intersection = q_ngrams & t_ngrams
    if not intersection:
        return 0.0

    # Containment: query 有多少比例在 text 中
    containment = len(intersection) / len(q_ngrams)

    # Jaccard: 双向相似度
    union = q_ngrams | t_ngrams
    jaccard = len(intersection) / len(union) if union else 0.0

    # 混合评分（containment 主导，适合检索场景）
    score = 0.7 * containment + 0.3 * jaccard

    # 完整子串加分
    if len(query) >= 2 and query in text:
        score = min(1.0, score + 0.2)

    return round(score, 4)


_RELEVANCE_THRESHOLD = 0.25


class RolePlayAgent(BaseAgent):
    """角色扮演心智引擎。

    init_character(name, scenario) → 加载角色配置（非 Tool）
    Agent 通过 ReAct 循环自主管理: 记忆检索 → 情感调整 → 边界检查 → 自然终止回复
    当 LLM 直接输出文本（无 tool_calls）时循环结束，该文本即为角色回复。
    """

    SKILL_NAME = "roleplay"

    def __init__(self, ctx, services, llm, memory=None):
        super().__init__(ctx, services, llm, memory)
        self._kg = services.kg
        # Episodic memory 写入追踪
        self._turn_count = 0
        self._pending_turns: list = []  # [(user_msg, assistant_msg), ...]

    @property
    def rp(self):
        return self._memory.roleplay

    # ================================================================
    # Post-turn 钩子 —— 周期性写入 EpisodicMemory
    # ================================================================

    def _on_post_turn(self, user_msg: str, assistant_msg: str):
        """每 5 轮对话摘要一次，写入 episodic memory。"""
        self._turn_count += 1
        self._pending_turns.append((user_msg[:500], assistant_msg[:500]))

        if self._turn_count % 5 == 0 and self._built_agent:
            self._summarize_turns_for_memory()

    def _summarize_turns_for_memory(self):
        """将最近几轮对话摘要写入 AgentFlow EpisodicMemory。"""
        if not self._pending_turns or not self._built_agent:
            return

        char = self.rp.active_character
        turns_text = "\n".join(
            f"对方: {u}\n{char}: {a}"
            for u, a in self._pending_turns
        )

        try:
            from agentflow.runtime.memory.episodic import MemoryFact

            result = self._llm.chat_json(
                system_prompt=(
                    "你是一个对话摘要器。从最近几轮对话中提取 1-3 条关键事实。\n"
                    "每条事实包含: fact_type(event|preference|decision), subject, predicate, object。\n"
                    "只提取有意义的信息（关键事件、角色态度变化、重要决定），忽略寒暄。\n"
                    "返回 JSON: {\"facts\": [{\"fact_type\": \"...\", \"subject\": \"...\", "
                    "\"predicate\": \"...\", \"object\": \"...\", \"confidence\": 0.0-1.0}]}"
                ),
                user_prompt=f"角色: {char}\n\n最近对话:\n{turns_text[:2000]}",
                temperature=0.3,
                max_tokens=1024,
            )

            facts = result.get("facts", []) if isinstance(result, dict) else []
            for f in facts:
                fact = MemoryFact(
                    fact_type=f.get("fact_type", "event"),
                    subject=char,
                    predicate=f.get("predicate", ""),
                    object=f.get("object", ""),
                    confidence=float(f.get("confidence", 0.7)),
                    source_turn=self._turn_count,
                )
                self._built_agent.memory.episodic.add(fact)

        except Exception:
            pass  # 记忆写入失败不影响主流程
        finally:
            self._pending_turns = []

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
3. **Observation**: 读取工具返回的结果
4. 重复 1-3 直到确认所有必要信息已获取
5. **终止**: 直接输出角色对话文本（不要再调用工具）
   - 当你确认记忆已检索、情感已调整、边界已检查后，直接以角色身份输出对话
   - 不调用任何工具，直接输出文本 — AgentFlow 会在此终止循环
   - 动作描写用括号，如 "(握紧拳头)……我知道了。"
   - 当不确定如何行为时，参考「行为参考 (Policy Anchors)」中的类似情境
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
        if char_profile.policy_anchors:
            parts.append(self._format_policy_section(char_profile.policy_anchors))
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

    def _format_policy_section(self, anchors: list) -> str:
        """格式化 Policy Anchors 为参考段落。"""
        if not anchors:
            return ""
        lines = ["### 行为参考 (Policy Anchors)", "当不确定如何行为时，参考角色在类似情境下的典型行为:"]
        for i, a in enumerate(anchors[:4]):
            state_str = ", ".join(f"{k}:{v}" for k, v in a.state_snapshot.items()) if hasattr(a, 'state_snapshot') and a.state_snapshot else ""
            state_preview = f"[{state_str}]" if state_str else ""
            situation = a.situation if hasattr(a, 'situation') and a.situation else ""
            action = a.action if hasattr(a, 'action') and a.action else ""
            if not situation and not action:
                # 可能是 dict 格式
                if isinstance(a, dict):
                    state_snap = a.get("state_snapshot", {})
                    state_preview = f"[{', '.join(f'{k}:{v}' for k, v in state_snap.items())}]" if state_snap else ""
                    situation = a.get("situation", "")
                    action = a.get("action", "")
            if situation and action:
                lines.append(f"- {state_preview} {situation} → {action}")
        return "\n".join(lines) if len(lines) > 2 else ""

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
        llm = self._llm  # 供 check_boundary 嵌套 LLM 调用

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
                return "错误: 无活跃角色"

            graph = ctx.novel.story_graph if ctx.novel else None
            if not graph:
                return "错误: KG 不可用"

            scored_events = []
            scored_relations = []
            scored_knowledge = []

            # 1. 查角色参与的事件（语义评分排序）
            events = graph.character_events(char) if graph else []
            if aspect in ("event", "any"):
                for ev in events:
                    name = ev.get("name", "")
                    summary = ev.get("summary", "")
                    score = _score_relevance(query, name + " " + summary)
                    if score >= _RELEVANCE_THRESHOLD:
                        scored_events.append((
                            score,
                            f"[事件] {name} (第{ev.get('chapter_start', '?')}章): {summary}"
                        ))

            # 2. 查人际关系（语义评分排序）
            if aspect in ("person", "any") and graph:
                for edge in graph.relationship_edges:
                    if edge.from_char == char or edge.to_char == char:
                        target = edge.to_char if edge.from_char == char else edge.from_char
                        score = _score_relevance(query, target)
                        if score >= _RELEVANCE_THRESHOLD:
                            scored_relations.append((
                                score,
                                f"[关系] 与 {target}: {edge.relation_type} "
                                f"(亲密度:{edge.intimacy:+d})"
                                + (f" | {edge.shared_history}" if edge.shared_history else "")
                            ))

            # 3. 查角色知识缓存（按段评分，取 top-3）
            kg_text = rp.character_knowledge.get(char, "")
            if kg_text and aspect in ("any", "event", "emotion"):
                # 按空行分段
                chunks = [c.strip() for c in kg_text.split("\n\n") if c.strip()]
                # 大段按句拆分
                fine_chunks = []
                for chunk in chunks:
                    if len(chunk) > 300:
                        sentences = chunk.replace("。", "。\n").replace("！", "！\n").replace("？", "？\n").split("\n")
                        fine_chunks.extend(s for s in sentences if len(s) >= 10)
                    else:
                        fine_chunks.append(chunk)

                for chunk in fine_chunks:
                    score = _score_relevance(query, chunk)
                    if score >= _RELEVANCE_THRESHOLD:
                        scored_knowledge.append((score, f"[知识] {chunk[:300]}"))

            # 按分数排序
            scored_events.sort(key=lambda x: -x[0])
            scored_relations.sort(key=lambda x: -x[0])
            scored_knowledge.sort(key=lambda x: -x[0])

            results = []
            results.extend(item for _, item in scored_events[:5])
            results.extend(item for _, item in scored_relations[:5])
            results.extend(item for _, item in scored_knowledge[:3])

            if not results:
                return (
                    f"角色的记忆中没有找到与「{query}」相关的内容。"
                    f"角色对此事可能记忆模糊，或此事不在角色的经历中。"
                )

            return "角色的记忆中有以下相关内容:\n" + "\n".join(results)

        @tool
        def adjust_emotion(trigger: str, intensity: float = 0.5) -> str:
            """调整角色的情感状态。

            当对话内容触发了角色的情绪反应时调用。
            系统会基于角色的敏感度系数计算实际变化量。

            Args:
                trigger: 触发事件描述（如 "被提及痛苦往事", "感受到威胁", "收到真诚关心"）
                intensity: 主观感受强度 0.0-1.0（默认 0.5）

            Returns:
                情感变化摘要（叙事化格式）
            """
            char = rp.active_character
            if not char or not rp.sensitivity_profile:
                return "情感系统未初始化，跳过情感调整。"

            # 记录变化前的状态
            old_state = dict(rp.runtime_state)

            deltas = rp.apply_event(trigger, intensity)

            if not deltas:
                return f"触发「{trigger}」未匹配到任何敏感度规则，情感状态未变化。"

            emotion = rp.get_emotion_summary()

            # 叙事化输出：变化列表 + 当前状态
            lines = [f"⚠ 情感状态已更新 (触发: {trigger}, 强度: {intensity:.1f}):"]
            for dim, delta in deltas.items():
                old_val = old_state.get(dim, 50)
                new_val = rp.runtime_state.get(dim, old_val)
                direction = "↑" if delta > 0 else "↓"
                lines.append(f"  {dim}: {old_val:.0f} → {new_val:.0f} ({delta:+.1f}) {direction}")

            # 显示未变化的维度
            unchanged = [d for d in old_state if d not in deltas]
            if unchanged and len(unchanged) <= 5:
                lines.append(f"  未变: {', '.join(f'{d}({old_state[d]:.0f})' for d in unchanged[:5])}")

            lines.append(f"当前情绪: {emotion}")
            return "\n".join(lines)

        @tool
        def check_boundary(intended_action: str) -> str:
            """检查角色打算做的行为是否触犯硬底线。

            在角色打算说/做可能有风险的事情前调用。
            使用 LLM 做语义判断，fallback 到关键词匹配。

            Args:
                intended_action: 打算做的事或说的话的描述

            Returns:
                "ok" 或违规警告
            """
            char_profile = ctx.character_profiles.get(rp.active_character)
            if not char_profile or not char_profile.boundary:
                return "边界数据未加载，跳过检查。"

            boundary = char_profile.boundary
            if not boundary.hard_rules:
                return "该角色未定义硬底线，边界检查通过。"

            rules_text = "\n".join(f"- {r}" for r in boundary.hard_rules)

            # 尝试 LLM 语义判断
            try:
                result = llm.chat_json(
                    system_prompt=(
                        "你是一个边界检查器。判断角色的行为意图是否触犯硬底线。\n"
                        "注意：要判断语义层面的冲突，不只是字面匹配。\n"
                        "例如：「委婉推辞」和底线「不会直接拒绝上级」不冲突；\n"
                        "但「暗示屈服」和底线「不会求饶」可能冲突。"
                    ),
                    user_prompt=(
                        f"角色硬底线:\n{rules_text}\n\n"
                        f"角色打算做的事: {intended_action}\n\n"
                        f"判断该行为是否触犯任何一条硬底线。返回 JSON: "
                        f'{{"violates": true/false, "violated_rule": "触犯的底线(未触犯填null)", "reason": "简短理由"}}'
                    ),
                    temperature=0.2,
                    max_tokens=512,
                )

                if result.get("violates"):
                    return (
                        f"⚠ 边界警告: {result.get('violated_rule', '触犯底线')}\n"
                        f"原因: {result.get('reason', '行为与角色底线冲突')}\n"
                        f"建议: 请调整行为，避免触犯角色的硬底线。考虑行为倾向作为替代。"
                    )
                return "边界检查通过。"

            except Exception:
                # Fallback: 关键词匹配
                warnings = []
                for rule in boundary.hard_rules:
                    rule_keywords = rule.replace("不会", "").replace("绝不", "")
                    if any(kw in intended_action for kw in rule_keywords.split("、")):
                        warnings.append(f"⚠ 触犯底线: {rule}")

                if warnings:
                    return (
                        "⚠ 边界警告 (关键词匹配):\n"
                        + "\n".join(warnings)
                        + "\n建议: 请调整行为，避免触犯角色的硬底线。"
                    )
                return "边界检查通过。"

        return [
            retrieve_memory,
            adjust_emotion,
            check_boundary,
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
