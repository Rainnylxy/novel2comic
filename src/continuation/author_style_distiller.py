# -*- coding: utf-8 -*-
"""AuthorStyleDistiller —— 作者文风蒸馏器。

从小说原文中蒸馏作者文风指纹。一次性分析、缓存复用。
与 CharacterDistiller 互补：一个蒸馏角色，一个蒸馏文风。

蒸馏流程:
  1. 均匀抽样 N 个章节文本（覆盖前中后期）
  2. 本地统计分析（句长、词频、对话占比）
  3. LLM 分析叙事模式和氛围基调
  4. 抽取 3-5 段典范段落作为 Exemplars
  5. 汇总为 AuthorStyleProfile
"""

import re
import math
from collections import Counter
from typing import TYPE_CHECKING, Optional

from .author_style_profile import (
    AuthorStyleProfile,
    SyntaxProfile,
    LexiconProfile,
    NarrativePatternProfile,
    AtmosphereProfile,
)

if TYPE_CHECKING:
    from ..llm import UnifiedLLM


# ============================================================
# LLM Prompt 模板
# ============================================================

NARRATIVE_ANALYSIS_PROMPT = """你是一位专业的文学风格分析师。请分析以下小说片段的叙事手法。

## 小说片段
{novel_sample}

## 任务
分析以上片段的叙事特征，返回严格 JSON:

{{
  "pov_frequency": "视角切换频率描述（如: 以第三人称有限视角为主，偶尔切换）",
  "inner_monologue_density": 0.0-1.0 之间的数值，表示心理描写占比,
  "cliffhanger_style": "章尾钩子的典型写法描述",
  "scene_transition_style": "场景过渡方式描述",
  "description_density": 0.0-1.0 之间的数值，表示环境描写占比
}}"""


ATMOSPHERE_ANALYSIS_PROMPT = """你是一位专业的文学风格分析师。请分析以下小说片段的氛围基调。

## 小说片段
{novel_sample}

## 任务
分析以上片段的氛围特征，返回严格 JSON:

{{
  "emotional_tendency": "冷峻 / 温暖 / 压抑 / 紧张 / 悬疑 等",
  "violence_scale": "含蓄 / 适度 / 直白",
  "intimacy_scale": "含蓄 / 适度 / 直白",
  "overall_tone": "一句话描述整体氛围基调"
}}"""


LEXICON_ANALYSIS_PROMPT = """你是一位专业的文学风格分析师。请分析以下小说的词汇使用特征。

## 统计高频词（代码预计算，仅供参考）
{stats_text}

## 小说片段样本
{novel_sample}

## 任务
基于以上信息和你的文学知识，返回严格 JSON:

{{
  "top_words": ["高频词1", "高频词2", ...] (最多20个，优先特色词而非通用词),
  "signature_words": ["特色词1", "特色词2", ...] (这个作者特有的用词，最多15个),
  "taboo_modern_words": ["禁用词1", ...] (如果这个时代/风格的设定中不应出现的现代词汇),
  "action_verb_style": "动作描写的习惯用词风格（如: 用叠词副词+单动词，'微微抬眸''轻轻放下'）"
}}"""


# ============================================================
# 本地统计
# ============================================================

# 中文标点 + 句子分隔符
_SENTENCE_END_PAT = re.compile(r'[。！？；\n]')
_CHINESE_CHAR_PAT = re.compile(r'[一-鿿]')
_DIALOGUE_PAT = re.compile(r'["""“”「」]([^""“”「」]+?)["""“”「」]')


def _split_sentences(text: str) -> list[str]:
    """按中文标点切分句子。"""
    text = text.replace("\n", "。")
    parts = _SENTENCE_END_PAT.split(text)
    return [p.strip() for p in parts if p.strip() and _CHINESE_CHAR_PAT.search(p)]


def _count_chinese_chars(text: str) -> int:
    """统计中文字符数。"""
    return len(_CHINESE_CHAR_PAT.findall(text))


def _extract_dialogue_text(text: str) -> str:
    """提取所有引号内的对话文本。"""
    return " ".join(_DIALOGUE_PAT.findall(text))


def _analyze_syntax(text: str) -> SyntaxProfile:
    """本地统计分析句法特征。"""
    sentences = _split_sentences(text)
    if not sentences:
        return SyntaxProfile()

    lengths = [_count_chinese_chars(s) for s in sentences]
    avg_len = sum(lengths) / len(lengths)

    short_count = sum(1 for l in lengths if l < 15)
    long_count = sum(1 for l in lengths if l > 30)
    mid_count = len(lengths) - short_count - long_count
    # short_long_ratio: 短句/长句比值。mid 算一半给两边
    short_long_ratio = (short_count + mid_count * 0.5) / max(1, long_count + mid_count * 0.5)

    total_chars = _count_chinese_chars(text)
    dialogue_chars = _count_chinese_chars(_extract_dialogue_text(text))
    dialogue_ratio = dialogue_chars / max(1, total_chars)
    narration_ratio = 1.0 - dialogue_ratio

    # 惯用句式检测（简化版）
    common_patterns = []
    # 检测 "XX了XX" 模式
    liao_pattern = re.findall(r'[一-鿿]了[一-鿿]', text)
    if len(liao_pattern) > 3:
        common_patterns.append("'X了Y' 句式")

    # 检测叠词修饰
    redup_pattern = re.findall(r'([一-鿿])\1(?=[一-鿿]{1,2})', text)
    if len(redup_pattern) > 3:
        common_patterns.append("叠词修饰（如'微微''沉沉'）")

    return SyntaxProfile(
        avg_sentence_length=round(avg_len, 1),
        sentence_length_range=[min(lengths), max(lengths)],
        short_long_ratio=round(short_long_ratio, 2),
        dialogue_ratio=round(dialogue_ratio, 3),
        narration_ratio=round(narration_ratio, 3),
        common_patterns=common_patterns,
    )


def _analyze_lexicon_locally(text: str) -> dict:
    """本地词频统计（简化：单字+双字 n-gram）。"""
    chars = _CHINESE_CHAR_PAT.findall(text)

    # 双字词频
    bigrams = [chars[i] + chars[i + 1] for i in range(len(chars) - 1)]
    bigram_counter = Counter(bigrams)

    # 单字词频（过滤常见虚词）
    stop_chars = set("的了一是在我有不这人他她它个们这那来去上到说也为和中就都而及与可以之被把但")
    char_counter = Counter(c for c in chars if c not in stop_chars)

    return {
        "top_bigrams": [w for w, _ in bigram_counter.most_common(30)],
        "top_chars": [w for w, _ in char_counter.most_common(20)],
        "total_chars": len(chars),
        "unique_bigrams": len(bigram_counter),
    }


def _extract_exemplars(text: str, count: int = 5) -> list[str]:
    """从文本中抽取典型段落作为 Exemplars。

    策略: 按段落切分 → 过滤太短的 → 均匀抽样。
    """
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    # 过滤太短的段落（< 50 中文字符）
    valid = [p for p in paragraphs if _count_chinese_chars(p) >= 50]
    if not valid:
        # 放宽条件
        valid = [p for p in paragraphs if _count_chinese_chars(p) >= 20]

    if len(valid) <= count:
        return valid

    # 均匀抽样
    step = (len(valid) - 1) / max(1, count - 1)
    indices = [int(i * step) for i in range(count)]
    return [valid[min(idx, len(valid) - 1)] for idx in indices]


# ============================================================
# AuthorStyleDistiller
# ============================================================

class AuthorStyleDistiller:
    """作者文风蒸馏器。

    用法:
        distiller = AuthorStyleDistiller(llm)
        profile = distiller.distill(novel_text)
    """

    def __init__(self, llm: "UnifiedLLM"):
        self._llm = llm

    def distill(self, novel_text: str, sample_chapters: int = 10) -> AuthorStyleProfile:
        """从小说原文蒸馏文风指纹。

        Args:
            novel_text: 小说全文（UTF-8 文本）
            sample_chapters: 抽样章节数（均匀分布覆盖前中后期）

        Returns:
            完整的 AuthorStyleProfile
        """
        if not novel_text or not novel_text.strip():
            return AuthorStyleProfile()

        # 1. 均匀抽样：取 N 段均匀分布的文本
        total_len = len(novel_text)
        segment_size = total_len // sample_chapters if sample_chapters > 0 else total_len
        samples = []
        for i in range(min(sample_chapters, max(1, total_len // 1000))):
            start = i * segment_size
            end = min(start + min(segment_size, 3000), total_len)
            chunk = novel_text[start:end]
            if _count_chinese_chars(chunk) >= 100:
                samples.append(chunk)

        sample_text = "\n\n".join(samples) if samples else novel_text[:5000]

        # 2. 本地统计分析
        syntax = _analyze_syntax(sample_text)
        lexical_stats = _analyze_lexicon_locally(sample_text)

        # 3. LLM 分析（3 次调用：叙事 + 氛围 + 词汇）
        narrative = self._llm_analyze_narrative(sample_text[:4000])
        atmosphere = self._llm_analyze_atmosphere(sample_text[:4000])
        lexicon = self._llm_analyze_lexicon(sample_text[:4000], lexical_stats)

        # 4. 抽取 Exemplars
        exemplars = _extract_exemplars(novel_text, count=4)

        return AuthorStyleProfile(
            syntax=syntax,
            lexicon=lexicon,
            narrative=narrative,
            atmosphere=atmosphere,
            exemplars=exemplars,
        )

    def _llm_analyze_narrative(self, sample: str) -> NarrativePatternProfile:
        """LLM 分析叙事手法。"""
        try:
            result = self._llm.chat_json(
                system_prompt="你是一位专业的文学风格分析师。只返回 JSON，不返回其他内容。",
                user_prompt=NARRATIVE_ANALYSIS_PROMPT.format(novel_sample=sample),
                temperature=0.3,
                max_tokens=1024,
            )
            if isinstance(result, dict):
                return NarrativePatternProfile(
                    pov_frequency=result.get("pov_frequency", ""),
                    inner_monologue_density=float(result.get("inner_monologue_density", 0)),
                    cliffhanger_style=result.get("cliffhanger_style", ""),
                    scene_transition_style=result.get("scene_transition_style", ""),
                    description_density=float(result.get("description_density", 0)),
                )
        except Exception:
            pass
        return NarrativePatternProfile()

    def _llm_analyze_atmosphere(self, sample: str) -> AtmosphereProfile:
        """LLM 分析氛围基调。"""
        try:
            result = self._llm.chat_json(
                system_prompt="你是一位专业的文学风格分析师。只返回 JSON，不返回其他内容。",
                user_prompt=ATMOSPHERE_ANALYSIS_PROMPT.format(novel_sample=sample),
                temperature=0.3,
                max_tokens=512,
            )
            if isinstance(result, dict):
                return AtmosphereProfile(
                    emotional_tendency=result.get("emotional_tendency", ""),
                    violence_scale=result.get("violence_scale", ""),
                    intimacy_scale=result.get("intimacy_scale", ""),
                    overall_tone=result.get("overall_tone", ""),
                )
        except Exception:
            pass
        return AtmosphereProfile()

    def _llm_analyze_lexicon(self, sample: str, stats: dict) -> LexiconProfile:
        """LLM 分析词汇特征。"""
        stats_text = (
            f"Top 双字词(统计): {', '.join(stats.get('top_bigrams', [])[:20])}\n"
            f"Top 单字(统计): {', '.join(stats.get('top_chars', [])[:15])}\n"
            f"总字数: {stats.get('total_chars', 0)}"
        )
        try:
            result = self._llm.chat_json(
                system_prompt="你是一位专业的文学风格分析师。只返回 JSON，不返回其他内容。",
                user_prompt=LEXICON_ANALYSIS_PROMPT.format(
                    novel_sample=sample,
                    stats_text=stats_text,
                ),
                temperature=0.3,
                max_tokens=1024,
            )
            if isinstance(result, dict):
                return LexiconProfile(
                    top_words=result.get("top_words", []),
                    signature_words=result.get("signature_words", []),
                    taboo_modern_words=result.get("taboo_modern_words", []),
                    action_verb_style=result.get("action_verb_style", ""),
                )
        except Exception:
            pass
        return LexiconProfile()
