# -*- coding: utf-8 -*-
"""AuthorStyleProfile —— 作者文风指纹数据模型。

维度:
  1. 句法特征 (Syntax)
  2. 词汇指纹 (Lexicon)
  3. 叙事惯用手法 (Narrative Patterns)
  4. 氛围基调 (Atmosphere)
  5. 写作范式样本 (Exemplars)
"""

from dataclasses import dataclass, field, asdict


@dataclass
class SyntaxProfile:
    """句法特征 —— 本地统计分析。"""

    avg_sentence_length: float = 0.0
    sentence_length_range: list = field(default_factory=lambda: [0, 0])  # [min, max]
    short_long_ratio: float = 1.0  # 短句(<15字)与长句(>30字)比值
    dialogue_ratio: float = 0.0    # 对话占总字数比例
    narration_ratio: float = 0.0   # 叙述占总字数比例
    common_patterns: list[str] = field(default_factory=list)  # 惯用句式

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SyntaxProfile":
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


@dataclass
class LexiconProfile:
    """词汇指纹 —— 词频 + 特色词汇。"""

    top_words: list[str] = field(default_factory=list)          # 高频词 top-20
    signature_words: list[str] = field(default_factory=list)    # 特色词汇（非通用高频词）
    taboo_modern_words: list[str] = field(default_factory=list) # 不应出现的现代口语
    action_verb_style: str = ""  # 动作描写习惯用词风格描述

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "LexiconProfile":
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


@dataclass
class NarrativePatternProfile:
    """叙事惯用手法 —— LLM 分析。"""

    pov_frequency: str = ""         # 视角切换频率描述
    inner_monologue_density: float = 0.0  # 心理描写密度 (0-1)
    cliffhanger_style: str = ""     # 章尾钩子典型写法
    scene_transition_style: str = "" # 场景过渡方式
    description_density: float = 0.0    # 环境描写占比 (0-1)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "NarrativePatternProfile":
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


@dataclass
class AtmosphereProfile:
    """氛围基调 —— LLM 分析。"""

    emotional_tendency: str = ""  # 冷峻 / 温暖 / 压抑 / 紧张
    violence_scale: str = ""      # 暴力描写尺度：含蓄 / 适度 / 直白
    intimacy_scale: str = ""      # 亲密描写尺度：含蓄 / 适度 / 直白
    overall_tone: str = ""        # 一句话氛围描述

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AtmosphereProfile":
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


@dataclass
class AuthorStyleProfile:
    """作者文风指纹 —— 从小说原文中蒸馏。

    一次性分析、缓存复用。与 CharacterDistiller 平级但互补。
    CharacterDistiller 蒸馏单个角色的 Voice/Boundary，
    AuthorStyleDistiller 蒸馏整部小说的文风指纹。
    """

    novel_title: str = ""
    syntax: SyntaxProfile = field(default_factory=SyntaxProfile)
    lexicon: LexiconProfile = field(default_factory=LexiconProfile)
    narrative: NarrativePatternProfile = field(default_factory=NarrativePatternProfile)
    atmosphere: AtmosphereProfile = field(default_factory=AtmosphereProfile)
    exemplars: list[str] = field(default_factory=list)  # 3-5 段风格锚点段落

    def to_dict(self) -> dict:
        return {
            "novel_title": self.novel_title,
            "syntax": self.syntax.to_dict(),
            "lexicon": self.lexicon.to_dict(),
            "narrative": self.narrative.to_dict(),
            "atmosphere": self.atmosphere.to_dict(),
            "exemplars": self.exemplars,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AuthorStyleProfile":
        return cls(
            novel_title=d.get("novel_title", ""),
            syntax=SyntaxProfile.from_dict(d.get("syntax", {})),
            lexicon=LexiconProfile.from_dict(d.get("lexicon", {})),
            narrative=NarrativePatternProfile.from_dict(d.get("narrative", {})),
            atmosphere=AtmosphereProfile.from_dict(d.get("atmosphere", {})),
            exemplars=d.get("exemplars", []),
        )

    def summary(self) -> str:
        """生成供 Chapter Writer prompt 使用的风格摘要（~500字）。"""
        parts = [f"## 文风约束: 《{self.novel_title}》"]

        if self.atmosphere.overall_tone:
            parts.append(f"\n### 氛围基调\n{self.atmosphere.overall_tone}")
        if self.atmosphere.emotional_tendency:
            parts.append(f"情感倾向: {self.atmosphere.emotional_tendency}")

        if self.narrative.cliffhanger_style:
            parts.append(f"\n### 叙事手法\n章尾钩子: {self.narrative.cliffhanger_style}")
        if self.narrative.scene_transition_style:
            parts.append(f"场景过渡: {self.narrative.scene_transition_style}")
        if self.narrative.inner_monologue_density > 0:
            parts.append(f"心理描写密度: {self.narrative.inner_monologue_density:.0%}")

        if self.syntax.avg_sentence_length > 0:
            parts.append(f"\n### 句法特征\n平均句长: {self.syntax.avg_sentence_length:.0f} 字")
        if self.syntax.common_patterns:
            parts.append(f"惯用句式: {', '.join(self.syntax.common_patterns[:5])}")

        if self.lexicon.signature_words:
            parts.append(f"\n### 词汇特征\n特色词汇: {', '.join(self.lexicon.signature_words[:10])}")
        if self.lexicon.taboo_modern_words:
            parts.append(f"禁用词: {', '.join(self.lexicon.taboo_modern_words)}")

        if self.atmosphere.violence_scale:
            parts.append(f"\n### 描写尺度\n暴力: {self.atmosphere.violence_scale}")
        if self.atmosphere.intimacy_scale:
            parts.append(f"亲密: {self.atmosphere.intimacy_scale}")

        return "\n".join(parts) + "\n"

    def exemplars_text(self) -> str:
        """格式化的 Exemplars 文本（供 Writer prompt 使用）。"""
        if not self.exemplars:
            return ""
        lines = ["## 风格参考段落（请模仿以下段落的笔法）"]
        for i, ex in enumerate(self.exemplars, 1):
            lines.append(f"\n### 参考段落 {i}\n{ex}")
        return "\n".join(lines)
