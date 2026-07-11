# -*- coding: utf-8 -*-
"""角色蒸馏引擎 —— 从原文 + KG 中蒸馏出结构化角色定义。

蒸馏流程:
  1. 收集角色相关文本片段（规则匹配，无需 LLM）
  2. 蒸馏 Voice（统计 + LLM 标注）
  3. 蒸馏 Boundary（LLM 反推硬底线 + 行为倾向）
  4. 蒸馏 State + Sensitivity + Recovery（LLM 推断初始值 + 敏感度 + 恢复速率）
  5. 蒸馏 Policy Anchors（LLM 提取行为锚点）

每个角色约 4 次 LLM 调用。importance >= 6 的角色进入蒸馏。
"""

import json
import re
from collections import Counter
from datetime import datetime
from typing import TYPE_CHECKING, Optional
from .character_profile import VoiceProfile, BoundaryProfile, StateProfile, SensitivityProfile

# PromptContext removed — distiller uses UnifiedLLM.chat_json() directly

if TYPE_CHECKING:
    from .llm import UnifiedLLM
    from ..core.models import StoryGraph
    from .character_profile import CharacterProfile


# ============================================================
# 蒸馏 Prompt 模板
# ============================================================

DISTILL_VOICE_PROMPT = """你是一位专业的文学角色分析师。你需要从角色对话文本中提取该角色的「表达特征」。

## 角色名
{character_name}

## 角色简介（来自 KG）
{character_summary}

## 该角色的对话/内心独白片段
{char_text}

## 该角色的统计特征（代码预计算，仅供参考）
{stats_text}

## 任务
分析以上文本，返回 JSON:

{{
  "tone_cold_warm": 0.0-1.0（0=极冷, 1=极热）,
  "tone_hard_soft": 0.0-1.0（0=强硬, 1=柔和）,
  "tone_distant_close": 0.0-1.0（0=疏离, 1=亲近）,
  "response_pattern": "silence_first|direct|counter_question|deflect",
  "rhythm": "initiator|responder",
  "sentence_types": {{"declarative": 0.0, "imperative": 0.0, "interrogative": 0.0, "exclamatory": 0.0}},
  "taboo_words": ["这个词角色绝不会说"],
  "taboo_patterns": ["这种说话方式角色绝不会用"],
  "voice_shift": {{
    "notes": "角色对不同对象说话时表达方式如何变化（句长、用词、语气等）"
  }},
  "summary": "一句话总结角色的表达风格（20字以内）"
}}

## 规则
1. 只分析表达方式（怎么说），不分析性格（什么人）
2. taboo_words 和 taboo_patterns 要谨慎——只在明确看到"角色在所有情境下都不用"时列出
3. 语气光谱要给出理由（例：因为对敌人仍保持克制 → 偏冷但不是极冷）
4. voice_shift 基于对话对象的变化，如果样本不足可以不填"""


DISTILL_BOUNDARY_PROMPT = """你是一位专业的文学角色分析师。你需要从角色行为描述中提取「行为边界」。

## 角色名
{character_name}

## 角色简介（来自 KG）
{character_summary}

## 该角色的行为描述/关键场景片段
{char_text}

## 任务
分析该角色的行为模式，返回 JSON:

{{
  "hard_rules": ["绝不会做的事——即使在极端压力下也不做的事（空列表是允许的，如果样本不足以确定）"],
  "tendencies": ["通常会做的事——角色的行为倾向模式"],
  "relationship_behaviors": {{
    "角色A": "对该角色的行为特点（一句话）",
    "角色B": "对该角色的行为特点（一句话）"
  }}
}}

## 规则
1. hard_rules 从"角色在压力下仍然没做的事"反推。例如：被敌人围困→没有求饶 → "不会求饶"
2. hard_rules 用动词开头，表示可验证的行为（"不会求饶"而非"有尊严"）
3. tendencies 是统计趋势而非绝对约束（"压力下先沉默再开口"）
4. 如果没有足够的压力情境来推断 hard_rules，返回空列表——不要编造"""


DISTILL_STATE_PROMPT = """你是一位专业的文学角色分析师。你需要从原文推断角色的「心理状态参数」。

## 角色名
{character_name}

## 角色简介（来自 KG）
{character_summary}

## 该角色的关键场景（首次出场 + 行为变化）
{char_text}

## 任务
分析该角色的心理维度，返回 JSON:

{{
  "baseline": {{
    "Trust": 0-100（对他人的初始信任度）,
    "Fear": 0-100（恐惧/焦虑基线）,
    "Anger": 0-100（愤怒基线）,
    "Love": 0-100（关爱/温暖基线）,
    "Honor": 0-100（荣誉感/自尊）,
    "Greed": 0-100（物质欲望）,
    "Patience": 0-100（耐心/耐性）,
    "RiskPreference": 0-100（冒险倾向）,
    "Dominance": 0-100（支配欲/控制欲）,
    "Intelligence": 0-100（智力/谋略水平）
  }},
  "sensitivity": [
    {{
      "triggers": ["事件类型描述"],
      "effects": {{"Trust": -0.85, "Anger": 0.55}},
      "evidence": ["原文中的证据（如'第X章XX事件'）"],
      "confidence": "原文锚定|部分推测|推测"
    }}
  ],
  "recovery": {{
    "rates": {{"Trust": 0.02, "Fear": 0.20, "Anger": 0.15, "Love": 0.01, "Honor": 0.08}},
    "triggers": {{"Trust": ["恢复信任的事件类型"]}}
  }}
}}

## 规则
1. baseline 从角色首次出场时的行为推断。不要用"平均值 50"——要有依据
2. sensitivity 从原文中的"事件→行为变化"配对反推。每条至少有一个原文证据
3. recovery 从间隔章节的状态对比推断。如果原文信息不足，标注 confidence="推测"
4. 所有数字要有理由，不是拍脑袋"""


DISTILL_POLICY_PROMPT = """你是一位专业的文学角色分析师。你需要从角色行为中提取「行为锚点」。

## 角色名
{character_name}

## 角色简介（来自 KG）
{character_summary}

## 该角色的行为场景片段
{char_text}

## 任务
从以上文本中提取 3-6 个行为锚点。每个锚点是一个 (心理状态, 情境, 行为) 三元组。

返回 JSON:
{{
  "policy_anchors": [
    {{
      "state_snapshot": {{"Trust": 80, "Anger": 10, "Fear": 10}},
      "situation": "具体情境描述（一句话）",
      "action": "角色做了什么（一句话）"
    }}
  ]
}}

## 规则
1. state_snapshot 是推测的角色在此情境下的心理状态（用 baseline 维度）
2. 只提取原文中有明确行为的情境，不要编造
3. 选择具有代表性的情境——能展示角色不同侧面（压力下、日常、面对不同人）
4. 3-6 个锚点即可，质量 > 数量"""


# ============================================================
# CharacterDistiller
# ============================================================

class CharacterDistiller:
    """从原文 + KG 蒸馏角色定义。

    用法:
        distiller = CharacterDistiller(llm, kg_service)
        profiles = distiller.distill_all(novel_text, story_graph)
    """

    # 进入蒸馏的 importance 阈值
    MIN_IMPORTANCE = 6

    def __init__(self, llm: "UnifiedLLM", kg_service=None):
        self._llm = llm
        self._kg = kg_service
        # _prompt_ctx removed — no longer needed

    # ================================================================
    # 公有 API
    # ================================================================

    def distill_all(
        self,
        novel_text: str,
        graph: "StoryGraph",
        min_importance: int = None,
    ) -> dict:
        """对所有重要角色执行蒸馏。

        Args:
            novel_text: 完整小说文本
            graph: 知识图谱
            min_importance: 最小 importance 阈值，默认 MIN_IMPORTANCE (6)

        Returns:
            {character_name: CharacterProfile}
        """
        threshold = min_importance or self.MIN_IMPORTANCE
        profiles = {}

        # 筛选重要角色
        persons = sorted(graph.person_nodes, key=lambda p: -p.importance)
        targets = [p for p in persons if p.importance >= threshold]

        if not targets:
            # importance 数据可能不可靠，取前 5 个
            targets = persons[:5]

        print(f"  [Distill] 蒸馏目标: {len(targets)} 个角色 "
              f"({', '.join(p.name for p in targets[:8])}{'...' if len(targets) > 8 else ''})")

        for i, person in enumerate(targets):
            name = person.name
            print(f"  [Distill] ({i+1}/{len(targets)}) {name} ...", end=" ", flush=True)
            try:
                profile = self.distill_character(name, novel_text, graph)
                profiles[name] = profile
                print(f"✓ (confidence={profile.confidence})")
            except Exception as e:
                print(f"✗ ({e})")
                # 继续处理其他角色

        print(f"  [Distill] 完成: {len(profiles)}/{len(targets)} 个角色蒸馏成功")
        return profiles

    def distill_character(
        self,
        name: str,
        novel_text: str,
        graph: "StoryGraph",
    ) -> "CharacterProfile":
        """对单个角色执行完整蒸馏。

        Args:
            name: 角色名
            novel_text: 完整小说文本
            graph: 知识图谱

        Returns:
            CharacterProfile
        """
        from .character_profile import (
            CharacterProfile, VoiceProfile, BoundaryProfile,
            StateProfile, SensitivityProfile, RecoveryProfile,
            SensitivityEntry, PolicyAnchor,
        )

        # Step 0: 收集角色文本片段（KG 事件锚定 + 均匀采样）
        char_text = self._collect_character_text(name, novel_text, graph)

        # 从 KG 获取角色摘要
        char_summary = self._get_character_summary(name, graph)

        # Step 1: 蒸馏 Voice
        voice = self._distill_voice(name, char_text, char_summary)

        # Step 2: 蒸馏 Boundary
        boundary = self._distill_boundary(name, char_text, char_summary)

        # Step 3: 蒸馏 State + Sensitivity + Recovery
        state_data = self._distill_state(name, char_text, char_summary)

        # Step 4: 蒸馏 Policy Anchors
        policy_data = self._distill_policy(name, char_text, char_summary)

        # 组装
        profile = CharacterProfile(
            name=name,
            voice=voice,
            boundary=boundary,
            state=state_data.get("state", StateProfile()),
            sensitivity=state_data.get("sensitivity", SensitivityProfile()),
            recovery=state_data.get("recovery", RecoveryProfile()),
            policy_anchors=policy_data,
            distilled_at=datetime.now().isoformat(),
            confidence=self._calc_confidence(char_text, voice, boundary),
        )
        return profile

    # ================================================================
    # Step 0: 收集角色文本（KG 事件锚定 + 均匀采样 + 上下文窗口）
    # ================================================================

    def _collect_character_text(self, name: str, novel_text: str, graph) -> str:
        """收集角色相关文本片段。

        策略:
        1. KG 事件锚定: 找到角色参与的关键事件所在章节，取章节开头文本
        2. 均匀采样: 在各章节均匀取包含角色名的行（±1 行上下文）
        3. 分类整理: 对话 > 内心/动作 > 场景叙述

        解决纯人名匹配的两个问题:
        - 漏代词: KG 事件锚定覆盖了事件发生章节的全景文本
        - 截断偏见: 均匀采样而非只取前 N 行

        Args:
            name: 角色名
            novel_text: 完整小说文本
            graph: 知识图谱

        Returns:
            拼接后的文本（最多 6000 字）
        """
        TEXT_BUDGET = 6000
        lines = novel_text.split("\n")

        # ── 1. 按章节切分原文 ──
        chapters = self._split_chapters(lines)

        # ── 2. KG 事件锚定: 找出角色出现的章节 ──
        event_chapters = set()
        if graph:
            char_events = graph.character_events(name) if graph else []
            for ev in char_events:
                ch = ev.get("chapter_start", 0)
                if ch > 0:
                    event_chapters.add(ch)

        # ── 3. 收集: 从事件章节取开头 + 全章均匀采样双源 ──
        result_parts = []
        total_chars = 0
        chapter_count = max(len(chapters), 1)

        # 3a. 事件章节开头（覆盖群像和间接描写）
        for ch_idx in sorted(event_chapters):
            ch_text = chapters.get(ch_idx, "")
            if ch_text:
                header = f"\n## 第{ch_idx}章（事件锚定）\n"
                chunk = ch_text[:600]  # 章节开头 600 字
                if total_chars + len(chunk) < TEXT_BUDGET:
                    result_parts.append(header + chunk)
                    total_chars += len(chunk) + len(header)

        # 3b. 全章均匀采样: 找出所有含角色名的行位置
        name_hits = []  # [(line_idx, line_text), ...]
        for i, line in enumerate(lines):
            if name in line:
                name_hits.append(i)

        # 均匀采样: 取 N 个均匀分布的代表性行 + 上下文
        if name_hits:
            num_samples = min(20, len(name_hits))
            if len(name_hits) > num_samples:
                # 每隔 step 取一行，覆盖全书
                step = max(1, len(name_hits) // num_samples)
                sampled_indices = name_hits[::step][:num_samples]
            else:
                sampled_indices = name_hits

            # 取上下文窗口（±1 行），分类
            seen_contexts = set()
            dialogue_samples = []
            inner_samples = []
            other_samples = []

            for idx in sampled_indices:
                # 上下文窗口
                start = max(0, idx - 1)
                end = min(len(lines), idx + 2)
                context = "\n".join(lines[start:end])
                key = context[:80]
                if key in seen_contexts:
                    continue
                seen_contexts.add(key)

                line = lines[idx]
                if re.search(r'["""]|说|道|问|答|喊|叫|曰|讲', line):
                    dialogue_samples.append(context)
                elif re.search(r'想|思|觉|感|记|知|明白|心中|默默|暗自', line):
                    inner_samples.append(context)
                else:
                    other_samples.append(context)

            # 按优先级填充
            for samples, label in [
                (dialogue_samples, "对话"),
                (inner_samples, "内心/动作"),
                (other_samples, "场景"),
            ]:
                section = f"\n## {label}\n"
                section_text = ""
                for s in samples:
                    if total_chars + len(s) + len(section) > TEXT_BUDGET:
                        break
                    section_text += s + "\n"
                    total_chars += len(s) + 1
                if section_text:
                    result_parts.insert(0, section + section_text)
                if total_chars >= TEXT_BUDGET:
                    break

        # 3c. 如果 KG 事件不足，补充全章均匀分布的名词匹配
        if total_chars < TEXT_BUDGET // 2 and name_hits:
            # 直接从均匀分布的行取
            step = max(1, len(name_hits) // 15)
            for idx in name_hits[::step]:
                if total_chars >= TEXT_BUDGET:
                    break
                start = max(0, idx - 1)
                end = min(len(lines), idx + 2)
                context = "\n".join(lines[start:end])
                total_chars += len(context) + 1
                result_parts.append(context + "\n")

        return "".join(result_parts)[:TEXT_BUDGET]

    def _split_chapters(self, lines: list) -> dict:
        """按章节标记将原文切分为 {chapter_index: text}。

        支持常见章节标记: "第X章", "Chapter X", "第X节" 等。
        """
        chapters = {}
        current_ch = 1
        current_lines = []

        chapter_pattern = re.compile(
            r'^\s*(?:第\s*([零一二三四五六七八九十百千\d]+)\s*[章节回卷]|Chapter\s+(\d+))',
        )

        for line in lines:
            m = chapter_pattern.match(line.strip())
            if m:
                if current_lines:
                    chapters[current_ch] = "\n".join(current_lines)
                ch_str = m.group(1) or m.group(2)
                current_ch = self._parse_chapter_number(ch_str)
                current_lines = []
            else:
                current_lines.append(line)

        if current_lines:
            chapters[current_ch] = "\n".join(current_lines)

        return chapters

    @staticmethod
    def _parse_chapter_number(s: str) -> int:
        """解析中英文章节号。"""
        if not s:
            return 1
        if s.isdigit():
            return int(s)
        # 中文数字
        cn_map = {
            "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
            "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
            "十": 10, "百": 100, "千": 1000,
        }
        result = 0
        unit = 1
        for ch in reversed(s):
            if ch in cn_map:
                val = cn_map[ch]
                if val >= 10:
                    unit = val
                else:
                    result += val * (unit if unit >= 10 else 1)
        return result or 1

    # ================================================================
    # Step 0b: KG 角色摘要
    # ================================================================

    def _get_character_summary(self, name: str, graph) -> str:
        """从 KG 获取角色的结构化摘要。"""
        person = graph.get_person_node(name)
        if not person:
            return f"角色: {name}（KG 中无数据）"

        lines = [
            f"角色: {name}",
            f"身份: {person.role_type}",
            f"派系: {person.faction}",
            f"重要度: {person.importance}/10",
            f"状态: {person.status}",
            f"简介: {person.description}",
        ]

        # 关系
        relations = []
        for edge in graph.relationship_edges:
            if edge.from_char == name:
                relations.append(
                    f"与 {edge.to_char}: {edge.relation_type}"
                    + (f" (亲密度:{edge.intimacy:+d})" if edge.intimacy else "")
                )
            elif edge.to_char == name:
                relations.append(
                    f"与 {edge.from_char}: {edge.relation_type}"
                    + (f" (亲密度:{edge.intimacy:+d})" if edge.intimacy else "")
                )
        if relations:
            lines.append("\n人际关系:")
            for r in relations[:10]:
                lines.append(f"  {r}")

        return "\n".join(lines)

    # ================================================================
    # Step 1: 蒸馏 Voice
    # ================================================================

    def _distill_voice(
        self, name: str, char_text: str, char_summary: str,
    ) -> VoiceProfile:
        """蒸馏 Voice Profile。

        统计部分代码计算，主观部分 LLM 标注。
        """

        # — 统计部分（代码直接计算）—
        stats = self._compute_voice_stats(char_text)

        profile = VoiceProfile(
            avg_sentence_length=stats["avg_sentence_length"],
            sentence_range=stats["sentence_range"],
            exclamation_density=stats["exclamation_density"],
            ellipsis_density=stats["ellipsis_density"],
            question_density=stats["question_density"],
            first_person=stats["first_person"],
        )

        # — LLM 标注部分 —
        if char_text.strip():
            try:
                result = self._llm_json(
                    system=DISTILL_VOICE_PROMPT,
                    user_input=f"角色: {name}\n角色摘要:\n{char_summary}\n文本:\n{char_text[:3000]}\n统计:\n{json.dumps(stats, ensure_ascii=False, indent=2)}",
                    temperature=0.4,
                    max_tokens=2048,
                )
                profile.tone_cold_warm = float(result.get("tone_cold_warm", 0.5))
                profile.tone_hard_soft = float(result.get("tone_hard_soft", 0.5))
                profile.tone_distant_close = float(result.get("tone_distant_close", 0.5))
                profile.response_pattern = result.get("response_pattern", "")
                profile.rhythm = result.get("rhythm", "")
                profile.sentence_types = result.get("sentence_types", {})
                profile.taboo_words = result.get("taboo_words", [])
                profile.taboo_patterns = result.get("taboo_patterns", [])
                profile.voice_shift = result.get("voice_shift", {})
                profile.summary = result.get("summary", "")
            except Exception:
                pass  # LLM 失败保持默认值

        return profile

    def _compute_voice_stats(self, text: str) -> dict:
        """从文本中计算 Voice 统计特征。"""
        if not text.strip():
            return {
                "avg_sentence_length": 0.0,
                "sentence_range": [4, 22],
                "exclamation_density": 0.0,
                "ellipsis_density": 0.0,
                "question_density": 0.0,
                "first_person": "我",
            }

        # 分句（按中文标点）
        sentences = re.split(r'[。！？!?\n]', text)
        sentences = [s.strip() for s in sentences if s.strip() and len(s.strip()) >= 2]
        sentence_lengths = [len(s) for s in sentences]

        if not sentence_lengths:
            sentence_lengths = [10]

        total_chars = len(text)
        exclamation_count = text.count("！") + text.count("!")
        ellipsis_count = text.count("…") + text.count("...")
        question_count = text.count("？") + text.count("?")

        # 第一人称
        first_person = "我"
        for fp in ["吾", "本座", "朕", "在下", "洒家", "俺"]:
            if fp in text:
                first_person = fp
                break
        if "我" in text:
            first_person = "我"

        return {
            "avg_sentence_length": round(sum(sentence_lengths) / len(sentence_lengths), 1),
            "sentence_range": [min(sentence_lengths), max(sentence_lengths)],
            "exclamation_density": round(exclamation_count / max(total_chars, 1), 4),
            "ellipsis_density": round(ellipsis_count / max(total_chars, 1), 4),
            "question_density": round(question_count / max(total_chars, 1), 4),
            "first_person": first_person,
        }

    # ================================================================
    # Step 2: 蒸馏 Boundary
    # ================================================================

    def _distill_boundary(
        self, name: str, char_text: str, char_summary: str,
    ) -> BoundaryProfile:
        """蒸馏 Boundary Profile。"""

        profile = BoundaryProfile()

        if not char_text.strip():
            return profile

        try:
            result = self._llm_json(
                system=DISTILL_BOUNDARY_PROMPT,
                user_input=f"角色: {name}\n角色摘要:\n{char_summary}\n文本:\n{char_text[:3000]}",
                temperature=0.4,
                max_tokens=2048,
            )
            profile.hard_rules = result.get("hard_rules", [])
            profile.tendencies = result.get("tendencies", [])
            profile.relationship_behaviors = result.get("relationship_behaviors", {})
        except Exception:
            pass

        return profile

    # ================================================================
    # Step 3: 蒸馏 State + Sensitivity + Recovery
    # ================================================================

    def _distill_state(
        self, name: str, char_text: str, char_summary: str,
    ) -> dict:
        """蒸馏 State、Sensitivity、Recovery。"""
        from .character_profile import (
            StateProfile, SensitivityProfile, RecoveryProfile, SensitivityEntry,
        )

        default = {
            "state": StateProfile(),
            "sensitivity": SensitivityProfile(),
            "recovery": RecoveryProfile(),
        }

        if not char_text.strip():
            return default

        try:
            result = self._llm_json(
                system=DISTILL_STATE_PROMPT,
                user_input=f"角色: {name}\n角色摘要:\n{char_summary}\n文本:\n{char_text[:3000]}",
                temperature=0.4,
                max_tokens=3072,
            )

            baseline = result.get("baseline", {})
            state = StateProfile(baseline=baseline)

            sensitivity_entries = []
            for entry in result.get("sensitivity", []):
                sensitivity_entries.append(SensitivityEntry(
                    triggers=entry.get("triggers", []),
                    effects=entry.get("effects", {}),
                    evidence=entry.get("evidence", []),
                    confidence=entry.get("confidence", "推测"),
                ))
            sensitivity = SensitivityProfile(entries=sensitivity_entries)

            recovery_data = result.get("recovery", {})
            recovery = RecoveryProfile(
                rates=recovery_data.get("rates", {}),
                triggers=recovery_data.get("triggers", {}),
            )

            return {"state": state, "sensitivity": sensitivity, "recovery": recovery}
        except Exception:
            return default

    # ================================================================
    # Step 4: 蒸馏 Policy Anchors
    # ================================================================

    def _distill_policy(
        self, name: str, char_text: str, char_summary: str,
    ) -> list:
        """蒸馏 Policy Anchors。"""
        from .character_profile import PolicyAnchor

        if not char_text.strip():
            return []

        try:
            result = self._llm_json(
                system=DISTILL_POLICY_PROMPT,
                user_input=f"角色: {name}\n角色摘要:\n{char_summary}\n文本:\n{char_text[:3000]}",
                temperature=0.5,
                max_tokens=2048,
            )
            anchors = []
            for a in result.get("policy_anchors", []):
                anchors.append(PolicyAnchor(
                    state_snapshot=a.get("state_snapshot", {}),
                    situation=a.get("situation", ""),
                    action=a.get("action", ""),
                ))
            return anchors[:6]  # 最多 6 个
        except Exception:
            return []

    # ================================================================
    # 工具方法
    # ================================================================

    def _llm_json(self, system: str, user_input: str,
                  temperature: float = 0.4, max_tokens: int = 2048) -> dict:
        """调用 LLM 并返回解析后的 JSON。"""
        result = self._llm.chat_json(
            system_prompt=system,
            user_prompt=user_input,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return result if isinstance(result, dict) else {}

    def _calc_confidence(self, char_text: str,
                         voice: "VoiceProfile",
                         boundary: "BoundaryProfile") -> str:
        """计算蒸馏置信度。"""
        if not char_text.strip() or len(char_text) < 200:
            return "low"

        has_hard_rules = bool(boundary.hard_rules)
        has_voice_summary = bool(voice.summary)
        has_enough_text = len(char_text) > 800

        if has_hard_rules and has_voice_summary and has_enough_text:
            return "high"
        elif has_hard_rules or has_voice_summary:
            return "medium"
        return "low"
