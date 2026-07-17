# -*- coding: utf-8 -*-
"""续写质量 LLM Judge —— 基于原文 KG 对续写内容打分。

评估 5 个维度:
  1. character_consistency — 角色行为是否符合前文 Voice/Boundary
  2. setting_consistency   — 设定是否有矛盾（死活、派系、时间线）
  3. style_consistency     — 基调、笔法是否接近原文
  4. plot_coherence        — 续写各章之间是否自洽
  5. writing_quality       — 对话是否自然、叙述是否流畅

每维度 1-10 分，附带理由。
"""

import json
import os
from typing import Optional

# 默认 prompt 模板
JUDGE_SYSTEM_PROMPT = """你是专业小说编辑，擅长评估续写质量。请根据原文前文（KG来源）和已蒸馏的参考标准对续写内容进行评分。

评分标准（每题1-10分）：

1. **角色一致性**: 对照角色档案（Voice、Boundary、敏感点、行为锚点），续写中的角色言行是否符合？
   - 10分: 完全贴合所有角色的 Voice 和硬底线，无 OOC
   - 5分: 主要角色基本贴合，次要角色有轻微偏差
   - 1分: 角色严重脱离档案设定

2. **设定一致性**: 有没有角色死而复生、派系写错、时间线矛盾、身份错误？
   - 10分: 无任何设定矛盾
   - 5分: 有1-2处小矛盾但不影响主线
   - 1分: 严重的设定矛盾（如死人复活、身份颠倒）

3. **风格一致性**: 对照文风档案（基调、句法、节奏、章尾钩子风格），续写是否匹配？
   - 10分: 句法、基调、节奏完全吻合文风档案，几乎无法分辨是续写
   - 5分: 大方向一致，但偶有波动
   - 1分: 与文风档案严重偏离

4. **情节连贯性**: 续写各章之间是否自洽？情节推进是否合理？
   - 10分: 情节层层推进，章间衔接自然，无跳跃感
   - 5分: 基本连贯，偶有突兀转折
   - 1分: 章间断裂，情节不连贯

5. **写作质量**: 对话是否自然？叙述是否流畅？有没有水字数/重复？
   - 10分: 文笔出色，对话鲜活，节奏得当
   - 5分: 通顺可读，偶有冗余
   - 1分: 语言生硬、大量重复或无效内容

请返回 JSON:
{
  "scores": {
    "character_consistency": {"score": N, "reason": "..."},
    "setting_consistency": {"score": N, "reason": "..."},
    "style_consistency": {"score": N, "reason": "..."},
    "plot_coherence": {"score": N, "reason": "..."},
    "writing_quality": {"score": N, "reason": "..."}
  },
  "overall": N,
  "summary": "一句话总结"
}

只返回 JSON，不要其他内容。"""


def format_style_profile(style_profile) -> str:
    """将 AuthorStyleProfile 格式化为 judge 参考文本。"""
    if not style_profile:
        return ""

    parts = ["## 文风参考标准（蒸馏档案）"]

    # Atmosphere
    atmos = style_profile.atmosphere
    if atmos:
        lines = ["### 基调氛围"]
        if atmos.overall_tone:
            lines.append(f"- 整体基调: {atmos.overall_tone}")
        if atmos.emotional_tendency:
            lines.append(f"- 情感倾向: {atmos.emotional_tendency}")
        parts.append("\n".join(lines))

    # Narrative
    narrative = style_profile.narrative
    if narrative:
        lines = ["### 叙事手法"]
        if narrative.cliffhanger_style:
            lines.append(f"- 章尾钩子: {narrative.cliffhanger_style}")
        if narrative.scene_transition_style:
            lines.append(f"- 场景过渡: {narrative.scene_transition_style}")
        parts.append("\n".join(lines))

    # Syntax
    syntax = style_profile.syntax
    if syntax:
        lines = ["### 句法特征"]
        if syntax.avg_sentence_length:
            lines.append(f"- 平均句长: {syntax.avg_sentence_length:.0f}字")
        if syntax.short_long_ratio:
            lines.append(f"- 短长句比: {syntax.short_long_ratio:.1f}")
        if syntax.common_patterns:
            lines.append(f"- 惯用句式: {', '.join(syntax.common_patterns[:5])}")
        parts.append("\n".join(lines))

    return "\n".join(parts)


def format_character_profiles(profiles: dict) -> str:
    """将 CharacterProfile dict 格式化为 judge 参考文本。"""
    if not profiles:
        return ""

    parts = ["## 角色参考标准（蒸馏档案）"]
    for name, profile in sorted(profiles.items()):
        char_lines = [f"### {name}"]

        # Voice
        v = getattr(profile, 'voice', None)
        if v:
            if v.summary:
                char_lines.append(f"- Voice: {v.summary}")
            if v.taboo_words:
                char_lines.append(f"- 禁用词: {', '.join(v.taboo_words)}")
            if v.taboo_patterns:
                char_lines.append(f"- 禁用句式: {', '.join(v.taboo_patterns)}")

        # Boundary
        b = getattr(profile, 'boundary', None)
        if b:
            if b.hard_rules:
                char_lines.append(f"- 硬底线: {', '.join(b.hard_rules)}")
            if b.tendencies:
                char_lines.append(f"- 行为倾向: {', '.join(b.tendencies[:3])}")

        # Sensitivity
        sens = getattr(profile, 'sensitivity', None)
        if sens and sens.entries:
            for e in sens.entries[:2]:
                triggers = ', '.join(getattr(e, 'triggers', [])[:3])
                if triggers:
                    char_lines.append(f"- 敏感触发: {triggers}")

        # State baseline
        state = getattr(profile, 'state', None)
        if state and state.baseline:
            base_str = ', '.join(f"{k}={v}" for k, v in list(state.baseline.items())[:5])
            char_lines.append(f"- 心理基线: {base_str}")

        parts.append("\n".join(char_lines))

    return "\n".join(parts)


def build_judge_prompt(source_text: str, generated_text: str,
                        genre: str = "",
                        style_profile=None,
                        character_profiles: dict = None,
                        evidence_text: str = "",
                        max_source: int = 2000,
                        max_generated: int = 4000) -> str:
    """构建 judge prompt。

    Args:
        source_text: 原文前 N 章（KG 来源）
        generated_text: 续写生成的完整文本
        genre: 小说类型
        style_profile: AuthorStyleProfile 蒸馏档案
        character_profiles: {name: CharacterProfile} 蒸馏档案
        evidence_text: 规则检测出的客观证据文本
        max_source: 原文最大输入长度
        max_generated: 续写最大输入长度

    Returns:
        完整的 user prompt 字符串
    """
    genre_hint = f"小说类型: {genre}\n" if genre else ""

    parts = [
        f"## 原文前文（KG来源）",
        f"{genre_hint}",
        f"{source_text[:max_source]}",
        "",
    ]

    # 注入蒸馏档案
    style_ref = format_style_profile(style_profile)
    if style_ref:
        parts.append(style_ref)
        parts.append("")

    char_ref = format_character_profiles(character_profiles)
    if char_ref:
        parts.append(char_ref)
        parts.append("")

    # 注入规则检测证据
    if evidence_text:
        parts.append(evidence_text)
        parts.append("")

    parts.extend([
        "---",
        "",
        "## 续写内容",
        f"{generated_text[:max_generated]}",
        "",
        "请根据原文前文、参考标准和规则检测结果评估此续写的质量，按评分标准逐维度打分。",
    ])

    return "\n".join(parts)


def fragments_to_text(fragments: list) -> str:
    """将 StoryFragment 列表拼接为可读文本。

    Args:
        fragments: [{"type": "dialogue", "character": "江停", "text": "..."}, ...]

    Returns:
        格式化的续写文本
    """
    lines = []
    for f in fragments:
        ftype = f.get("type", "narration")
        text = f.get("text", "")
        char = f.get("character", "")

        if ftype == "divider":
            label = f.get("divider_label", "")
            lines.append(f"\n--- {label} ---\n")
        elif ftype == "dialogue":
            lines.append(f"「{char}」{text}")
        elif ftype == "action":
            lines.append(f"({char} {text})")
        elif ftype == "inner_thought":
            lines.append(f"【{char} 内心】{text}")
        else:
            lines.append(text)
    return "\n".join(lines)


class QualityJudge:
    """续写质量评估器。

    用法:
        judge = QualityJudge(llm)
        result = judge.evaluate(source_chapters, generated_chapters, genre="刑侦推理")
        # result = {
        #     "scores": {...},
        #     "overall": 7.5,
        #     "summary": "..."
        # }
    """

    def __init__(self, llm):
        """初始化 Judge。

        Args:
            llm: UnifiedLLM 实例（需要 chat_json 方法）
        """
        self._llm = llm

    def evaluate(self, source_text: str, generated_text: str,
                 genre: str = "", style_profile=None,
                 character_profiles: dict = None,
                 evidence_text: str = "") -> dict:
        """运行评估。

        Args:
            source_text: 原文前 N 章文本（KG 来源）
            generated_text: 续写生成的完整文本
            genre: 小说类型
            style_profile: AuthorStyleProfile 蒸馏档案
            character_profiles: {name: CharacterProfile}
            evidence_text: 规则检测证据文本

        Returns:
            评估结果 dict，含 scores / overall / summary
        """
        prompt = build_judge_prompt(
            source_text, generated_text, genre,
            style_profile=style_profile,
            character_profiles=character_profiles,
            evidence_text=evidence_text,
        )

        try:
            result = self._llm.chat_json(
                system_prompt=JUDGE_SYSTEM_PROMPT,
                user_prompt=prompt,
                temperature=0.3,
                max_tokens=2048,
            )
            if isinstance(result, dict) and "scores" in result:
                return self._normalize(result)
        except Exception as e:
            print(f"  [Judge] LLM 调用失败: {e}")

        # Fallback
        return {
            "scores": {d: {"score": 0, "reason": "judge failed"}
                       for d in ["character_consistency", "setting_consistency",
                                  "style_consistency", "plot_coherence", "writing_quality"]},
            "overall": 0,
            "summary": f"Judge 评估失败",
            "error": str(e) if 'e' in dir() else "unknown",
        }

    def _normalize(self, result: dict) -> dict:
        """规范化 judge 输出。"""
        # 确保所有维度存在
        expected_dims = [
            "character_consistency", "setting_consistency",
            "style_consistency", "plot_coherence", "writing_quality",
        ]
        scores = result.get("scores", {})
        for dim in expected_dims:
            if dim not in scores:
                scores[dim] = {"score": 0, "reason": "missing"}

        # 计算 overall（如果没有）
        if "overall" not in result or not isinstance(result["overall"], (int, float)):
            valid_scores = [s["score"] for s in scores.values()
                           if isinstance(s.get("score"), (int, float)) and s["score"] > 0]
            result["overall"] = (sum(valid_scores) / len(valid_scores)
                                 if valid_scores else 0)

        return result

    def evaluate_chapter(self, source_text: str, chapter_fragments: list,
                         genre: str = "") -> dict:
        """评估单章续写。

        Args:
            source_text: 原文前文文本
            chapter_fragments: 本章 fragment 列表
            genre: 小说类型

        Returns:
            评估结果
        """
        generated = fragments_to_text(chapter_fragments)
        return self.evaluate(source_text, generated, genre)
