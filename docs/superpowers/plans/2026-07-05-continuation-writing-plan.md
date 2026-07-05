# 续写系统实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现多 Agent 协作的 AI 自主续写系统，支持流式输出、用户自然语言介入、聊天小说前端展示。

**Architecture:** 4 个 Agent 组成流水线（Plot Architect → Chapter Writer → Consistency Reviewer → Revision Editor）+ Author Style Distiller 文风蒸馏。Chapter Writer 直接使用 LLM 原生 streaming API 生成 StoryFragment 流，通过 SSE 推送到前端。基于现有 KG 基础设施进行角色/伏笔/一致性查询。

**Tech Stack:** Python 3.9+, Tornado (SSE), AgentFlow (ReAct Agent 框架), OpenAI-compatible streaming API, 原生 HTML/CSS/JS 前端

## Global Constraints

- 新代码放在 `src/continuation/` 目录下，与 `src/interactive/` 平级
- 不修改现有 roleplay 相关模块（`src/interactive/`、`src/agents/roleplay_agent.py`）
- Chapter Writer 不走 AgentFlow ReAct 循环，直接调用 LLM HTTP streaming API
- Plot Architect / Reviewer / Editor 继承 `BaseAgent`，走 AgentFlow
- 前端纯原生 HTML/CSS/JS，不引入框架
- 所有 LLM 调用基于现有 `UnifiedLLM`（JSON 模式）或 `httpx` 直接调用（streaming 模式）
- 需要遵循现有代码风格：UTF-8 编码头、中文注释、Google 风格 docstring

---

### Task 1: StoryFragment 数据模型

**Files:**
- Create: `src/continuation/__init__.py`
- Create: `src/continuation/fragment.py`
- Create: `src/continuation/author_style_profile.py`

**Interfaces:**
- Produces: `StoryFragment` dataclass（`type`, `text`, `character`, `divider_label`）, `PipelineEvent` dataclass（`event_type`, `data`）, `AuthorStyleProfile` dataclass

- [ ] **Step 1: 创建 `__init__.py`**

```python
# -*- coding: utf-8 -*-
"""续写系统 —— 多 Agent 协作流水线。

组件:
  - AuthorStyleDistiller: 作者文风蒸馏
  - PlotArchitect: 剧情架构师
  - ChapterWriter: 章节写手（流式核心）
  - ConsistencyReviewer: 一致性审校
  - RevisionEditor: 修订编辑
  - ContinuationPipeline: 流水线编排器
"""
```

- [ ] **Step 2: 创建 `fragment.py`**

```python
# -*- coding: utf-8 -*-
"""StoryFragment —— 续写流式输出的基本单位。

5 种片段类型对应不同的前端渲染方式:
  - dialogue:     角色对话 → 聊天气泡
  - narration:    第三人称旁白 → 居中灰字卡片
  - action:       角色动作 → 附属小字
  - inner_thought: 角色内心独白 → 虚线气泡
  - divider:      场景分隔 → 水平分割线
"""

import json
from dataclasses import dataclass, asdict
from typing import Optional, Literal

FragmentType = Literal["dialogue", "narration", "action", "inner_thought", "divider"]


@dataclass
class StoryFragment:
    """续写流的最小输出单元。

    Attributes:
        type: 片段类型，决定前端渲染方式
        text: 文本内容
        character: 角色名（dialogue / action / inner_thought 时必填）
        divider_label: 场景分隔标签（divider 时可选，如 "三小时后"）
    """

    type: FragmentType
    text: str
    character: Optional[str] = None
    divider_label: Optional[str] = None

    def to_dict(self) -> dict:
        """序列化为字典（用于 JSON → SSE 推送）。"""
        d = {"type": self.type, "text": self.text}
        if self.character:
            d["character"] = self.character
        if self.divider_label:
            d["divider_label"] = self.divider_label
        return d

    def to_sse(self) -> str:
        """序列化为单行 JSON（SSE data 字段）。

        Returns:
            不会包含换行符的紧凑 JSON 字符串
        """
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict) -> "StoryFragment":
        """从字典反序列化。"""
        return cls(
            type=d.get("type", "narration"),
            text=d.get("text", ""),
            character=d.get("character"),
            divider_label=d.get("divider_label"),
        )

    @classmethod
    def from_json(cls, json_str: str) -> "StoryFragment":
        """从 JSON 字符串反序列化。

        Raises:
            json.JSONDecodeError: 输入不是有效 JSON
        """
        return cls.from_dict(json.loads(json_str))

    @staticmethod
    def parse_stream_line(line: str) -> Optional["StoryFragment"]:
        """尝试将 LLM streaming 的一行输出解析为 StoryFragment。

        容错处理：去除首尾空白、跳过空行、跳过非 JSON 行。
        对于不完整的 JSON 行返回 None（由调用方缓冲拼接）。

        Args:
            line: LLM streaming 输出的一行文本

        Returns:
            StoryFragment 或 None（该行不是有效 fragment）
        """
        line = line.strip()
        if not line:
            return None
        # 跳过非 JSON 行（LLM 偶尔输出的解释文本）
        if not line.startswith("{"):
            return None
        try:
            return StoryFragment.from_json(line)
        except json.JSONDecodeError:
            # 不完整 JSON → 返回 None，由上层 buffer 处理
            return None


@dataclass
class PipelineEvent:
    """流水线事件 —— 用于 SSE 推送。

    事件类型:
      - "phase": 阶段切换 (data = {"phase": "planning"|"writing"|"reviewing"|"revising"})
      - "outline": Plot Architect 生成的章节大纲
      - "fragment": Chapter Writer 产出的 StoryFragment
      - "review": Consistency Reviewer 的审校结果
      - "complete": 流水线完成 (data = {"fragments": [...]} 或修订后结果)
      - "error": 错误 (data = {"message": "..."})
      - "done": 流结束标记
    """

    event_type: str
    data: dict = None

    def __post_init__(self):
        if self.data is None:
            self.data = {}

    def to_sse(self) -> str:
        """格式化为 SSE 协议文本。

        Returns:
            包含 event + data 的 SSE 消息，以双换行结尾
        """
        lines = [f"event: {self.event_type}"]
        data_str = json.dumps(self.data, ensure_ascii=False)
        lines.append(f"data: {data_str}")
        lines.append("")  # SSE 要求空行分隔
        return "\n".join(lines)
```

- [ ] **Step 3: 创建 `author_style_profile.py`**

```python
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
from typing import Optional


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
```

- [ ] **Step 4: 编写单元测试并运行**

```python
# tests/test_fragment.py
import json
import pytest
from src.continuation.fragment import StoryFragment, PipelineEvent, FragmentType


class TestStoryFragment:
    def test_dialogue_serialization(self):
        frag = StoryFragment(type="dialogue", text="你好。", character="江停")
        d = frag.to_dict()
        assert d == {"type": "dialogue", "text": "你好。", "character": "江停"}
        assert "divider_label" not in d

    def test_narration_serialization(self):
        frag = StoryFragment(type="narration", text="夜色如墨。")
        d = frag.to_dict()
        assert d == {"type": "narration", "text": "夜色如墨。"}
        assert "character" not in d

    def test_divider_with_label(self):
        frag = StoryFragment(type="divider", text="", divider_label="三小时后")
        d = frag.to_dict()
        assert d["type"] == "divider"
        assert d["divider_label"] == "三小时后"

    def test_sse_format_is_single_line(self):
        frag = StoryFragment(type="narration", text="测试文本")
        sse = frag.to_sse()
        assert "\n" not in sse

    def test_roundtrip(self):
        original = StoryFragment(type="dialogue", text="知道了。", character="严峫")
        sse = original.to_sse()
        restored = StoryFragment.from_json(sse)
        assert restored.type == original.type
        assert restored.text == original.text
        assert restored.character == original.character

    def test_parse_stream_line_valid_json(self):
        line = '{"type": "narration", "text": "夜。\\n风起。"}'
        frag = StoryFragment.parse_stream_line(line)
        assert frag is not None
        assert frag.type == "narration"
        assert frag.text == "夜。\n风起。"

    def test_parse_stream_line_empty(self):
        assert StoryFragment.parse_stream_line("") is None
        assert StoryFragment.parse_stream_line("   ") is None

    def test_parse_stream_line_non_json(self):
        assert StoryFragment.parse_stream_line("这是解释文本") is None

    def test_parse_stream_line_incomplete_json(self):
        assert StoryFragment.parse_stream_line('{"type": "dialogue"') is None

    def test_pipeline_event_sse_format(self):
        evt = PipelineEvent("phase", {"phase": "writing"})
        sse = evt.to_sse()
        assert sse.startswith("event: phase\n")
        assert "data:" in sse
        assert sse.endswith("\n\n")

    def test_fragment_type_literal(self):
        """验证 FragmentType 类型限定。"""
        valid_types = {"dialogue", "narration", "action", "inner_thought", "divider"}
        for t in valid_types:
            frag = StoryFragment(type=t, text="test")
            assert frag.type in valid_types
```

- [ ] **Step 5: 运行测试验证失败/通过**

Run: `python -m pytest tests/test_fragment.py -v`
Expected: 10 tests pass

- [ ] **Step 6: 提交**

```bash
git add src/continuation/ tests/test_fragment.py
git commit -m "feat: add StoryFragment, PipelineEvent, AuthorStyleProfile data models

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Author Style Distiller（作者文风蒸馏器）

**Files:**
- Create: `src/continuation/author_style_distiller.py`

**Interfaces:**
- Consumes: `AuthorStyleProfile`, `SyntaxProfile`, `LexiconProfile`, `NarrativePatternProfile`, `AtmosphereProfile` from Task 1
- Consumes: `UnifiedLLM` from `src/llm.py`
- Produces: `AuthorStyleDistiller.distill(novel_text, sample_chapters)` → `AuthorStyleProfile`

- [ ] **Step 1: 编写测试**

```python
# tests/test_author_style_distiller.py
import pytest
from unittest.mock import MagicMock, patch
from src.continuation.author_style_profile import (
    AuthorStyleProfile, SyntaxProfile, LexiconProfile,
    NarrativePatternProfile, AtmosphereProfile,
)
from src.continuation.author_style_distiller import AuthorStyleDistiller


class TestAuthorStyleDistiller:

    SAMPLE_NOVEL = """第一章 初遇

夜色如墨。江停站在窗前，目光落在远处的灯火上。

"江队。"严峫的声音从身后传来，带着一点不耐烦的调子，"你看看这个。"

江停转过身，接过那份档案。他的手指很稳，眼神却微微沉了沉。

"什么时候的事？"

"昨天晚上。"严峫靠在门框上，嘴角挂着一丝冷笑。"现场什么都没留下。"

江停没有说话。他把档案放在桌上，重新望向窗外。夜色很沉，和他此刻的心情一样。

严峫走过来，站在他旁边。"你怎么看？"

"不是意外。"江停的声音很轻，却很确定。

严峫挑了挑眉，等待着下文。但江停只是摇了摇头，没有再说什么。窗外有风吹过，带着潮湿的气息。要下雨了。

夜色更浓了。"""

    def test_distill_returns_profile(self):
        """蒸馏器应返回完整的 AuthorStyleProfile。"""
        mock_llm = MagicMock()
        # Mock LLM 返回叙事和氛围分析
        mock_llm.chat_json.side_effect = [
            {  # 叙事模式分析
                "pov_frequency": "以第三人称有限视角为主，偶尔切换",
                "inner_monologue_density": 0.3,
                "cliffhanger_style": "悬念式环境描写收尾",
                "scene_transition_style": "直接切换，省略过渡",
                "description_density": 0.2,
            },
            {  # 氛围基调分析
                "emotional_tendency": "冷峻",
                "violence_scale": "含蓄",
                "intimacy_scale": "含蓄",
                "overall_tone": "冷峻克制，暗流涌动",
            },
            {  # 词汇分析
                "top_words": ["夜色", "目光", "声音", "手指", "窗外"],
                "signature_words": ["冷", "沉", "暗", "稳", "浓"],
                "taboo_modern_words": ["OK", "手机"],
                "action_verb_style": "动作描写简洁，用'微微''沉沉''轻轻'等叠词修饰",
            },
        ]

        distiller = AuthorStyleDistiller(mock_llm)
        profile = distiller.distill(self.SAMPLE_NOVEL, sample_chapters=1)

        assert isinstance(profile, AuthorStyleProfile)
        # 统计分析不应为空
        assert profile.syntax.avg_sentence_length > 0
        assert profile.syntax.dialogue_ratio > 0
        # LLM 分析结果应有值
        assert profile.narrative.cliffhanger_style != ""
        assert profile.atmosphere.emotional_tendency != ""
        # exemplars 应有段落
        assert len(profile.exemplars) > 0

    def test_distill_calculates_syntax(self):
        """应正确计算句法统计特征。"""
        # 不依赖 LLM 的部分应该由本地计算完成
        distiller = AuthorStyleDistiller(MagicMock())
        profile = distiller.distill(self.SAMPLE_NOVEL, sample_chapters=1)

        # 句长统计
        assert profile.syntax.avg_sentence_length > 0
        assert profile.syntax.sentence_length_range[0] > 0
        assert profile.syntax.sentence_length_range[1] > profile.syntax.sentence_length_range[0]

    def test_distill_extracts_exemplars(self):
        """应提取 3-5 段风格锚点段落。"""
        mock_llm = MagicMock()
        mock_llm.chat_json.side_effect = [
            {"pov_frequency": "", "inner_monologue_density": 0, "cliffhanger_style": "",
             "scene_transition_style": "", "description_density": 0},
            {"emotional_tendency": "", "violence_scale": "", "intimacy_scale": "", "overall_tone": ""},
            {"top_words": [], "signature_words": [], "taboo_modern_words": [], "action_verb_style": ""},
        ]
        distiller = AuthorStyleDistiller(mock_llm)
        profile = distiller.distill(self.SAMPLE_NOVEL, sample_chapters=1)

        assert 1 <= len(profile.exemplars) <= 5
        for ex in profile.exemplars:
            assert len(ex) > 30  # 每段应该有一定长度

    def test_summary_includes_key_sections(self):
        """风格摘要应包含关键信息。"""
        profile = AuthorStyleProfile(
            novel_title="测试",
            syntax=SyntaxProfile(avg_sentence_length=25.0, common_patterns=["XXX了XX"]),
            lexicon=LexiconProfile(signature_words=["冷", "暗"], taboo_modern_words=["OK"]),
            narrative=NarrativePatternProfile(
                cliffhanger_style="环境描写收尾",
                inner_monologue_density=0.3,
            ),
            atmosphere=AtmosphereProfile(
                emotional_tendency="冷峻",
                overall_tone="冷峻克制",
                violence_scale="含蓄",
                intimacy_scale="含蓄",
            ),
        )

        summary = profile.summary()
        assert "测试" in summary
        assert "25" in summary   # 平均句长
        assert "冷峻" in summary  # 氛围
        assert "OK" in summary   # 禁用词

    def test_empty_novel_returns_empty_profile(self):
        """空文本应返回空 profile 不崩溃。"""
        distiller = AuthorStyleDistiller(MagicMock())
        profile = distiller.distill("", sample_chapters=1)
        assert isinstance(profile, AuthorStyleProfile)
```

- [ ] **Step 2: 运行测试验证失败**

Run: `python -m pytest tests/test_author_style_distiller.py -v`
Expected: FAIL (AuthorStyleDistiller not implemented)

- [ ] **Step 3: 实现 `author_style_distiller.py`**

```python
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
```

- [ ] **Step 4: 运行测试**

Run: `python -m pytest tests/test_author_style_distiller.py -v`
Expected: 5 tests pass

- [ ] **Step 5: 提交**

```bash
git add src/continuation/author_style_distiller.py tests/test_author_style_distiller.py
git commit -m "feat: add AuthorStyleDistiller with syntax/lexicon/narrative/atmosphere analysis

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Plot Architect Agent（剧情架构师）

**Files:**
- Create: `skills/plot_architect.md`
- Create: `src/continuation/plot_architect.py`

**Interfaces:**
- Consumes: `BaseAgent` from `src/agents/base_agent.py`, `KnowledgeGraphService` from `src/services/kg_service.py`
- Consumes: `AuthorStyleProfile` from Task 1, `CharacterProfile` from `src/character_profile_models.py`
- Produces: `PlotArchitect.run(instruction)` → dict（章节大纲）

- [ ] **Step 1: 创建 skill 文件**

Create `skills/plot_architect.md`:

```markdown
---
name: plot_architect
description: 基于 KG 上下文的续写大纲规划，管理剧情弧线和角色节拍
---

## Role
你是专业的剧情架构师 (Plot Architect)。
你的职责是：在续写新篇章之前，基于知识图谱中的角色状态、未解决伏笔和活跃冲突，规划出合理的章节大纲。

## 核心资源
- **知识图谱**：角色状态、关系网、事件因果链、未解决伏笔
- **角色 Profile**：每个主要角色的 Voice / Boundary / Policy Anchors
- **文风 Profile**：原作的叙事节奏和氛围基调
- **前一章结尾**：保证叙事连续性

## 工具使用指南
1. **analyze_hanging_threads()** — 第一步：从 KG 因果链中提取未解决的伏笔和活跃冲突
2. **sketch_character_beats(character_names)** — 第二步：为主要角色规划本章的情绪弧线和关键行动
3. **plan_structure(arc_spec)** — 第三步：生成章节结构（起承转合 + 章尾钩子）

## 规划原则
1. 优先推进已有的未解决伏笔，不要无中生有
2. 每个主要角色都要有"节拍"——情绪变化 + 行为推进
3. 章节结构要遵循原作的叙事节奏（参考文风 Profile）
4. 章尾必须设置悬念钩子
5. 大纲要具体可执行，不要太抽象
6. 角色行为必须符合其 Voice 和 Boundary
```

- [ ] **Step 2: 实现 `plot_architect.py`**

```python
# -*- coding: utf-8 -*-
"""PlotArchitect —— 剧情架构师 Agent。

继承 BaseAgent，通过 ReAct 循环管理:
  - 伏笔分析 (analyze_hanging_threads)
  - 角色节拍规划 (sketch_character_beats)
  - 章节结构设计 (plan_structure)

输入: KG 上下文 + 上一章结尾 + 用户指令 + 文风 Profile
输出: 章节大纲 JSON
"""

import json
from typing import TYPE_CHECKING

from agentflow.runtime.toolkit import tool

from ..agents.base_agent import BaseAgent

if TYPE_CHECKING:
    from ..context import GlobalContext, ServiceRegistry
    from ..llm import UnifiedLLM


class PlotArchitect(BaseAgent):
    """剧情架构师 Agent。

    继承 BaseAgent，通过 skill 文件 + 动态前缀注入上下文。
    3 个 ReAct 工具：analyze_hanging_threads, sketch_character_beats, plan_structure。
    """

    SKILL_NAME = "plot_architect"

    def __init__(self, ctx, services, llm, memory=None):
        super().__init__(ctx, services, llm, memory)
        self._kg = services.kg
        # 运行时注入的上下文（由 pipeline 在 run 前设置）
        self._outline_context: dict = {}

    def set_context(
        self,
        previous_chapter_ending: str,
        style_profile,
        character_profiles: dict,
        last_chapter: int,
        user_instruction: str = "",
    ):
        """设置 Plot Architect 的运行时上下文。

        在 Agent 构建前由 Pipeline 调用。

        Args:
            previous_chapter_ending: 前一章结尾原文（~3000字）
            style_profile: AuthorStyleProfile
            character_profiles: {name: CharacterProfile} 角色蒸馏 Profile
            last_chapter: 当前最后一章的章节号
            user_instruction: 用户的初始指令（可选）
        """
        self._outline_context = {
            "previous_chapter_ending": previous_chapter_ending,
            "style_summary": style_profile.summary() if style_profile else "",
            "character_profiles": character_profiles,
            "last_chapter": last_chapter,
            "user_instruction": user_instruction,
        }

    def _build_dynamic_prefix(self) -> str:
        """构建注入 user message 的动态前缀。

        放在 system prompt 缓存之外，包含变化的数据（前一章结尾等）。
        """
        ctx = self._outline_context
        if not ctx:
            return ""

        lines = [
            f"## 续写上下文",
            f"当前已写至第 {ctx['last_chapter']} 章。你需要为第 {ctx['last_chapter'] + 1} 章规划大纲。",
        ]

        if ctx.get("user_instruction"):
            lines.append(f"\n用户指令: {ctx['user_instruction']}")

        if ctx.get("style_summary"):
            lines.append(f"\n{ctx['style_summary']}")

        # 角色 Profile 摘要
        char_profiles = ctx.get("character_profiles", {})
        if char_profiles:
            lines.append("\n## 主要角色约束")
            for name, profile in char_profiles.items():
                lines.append(f"\n### {name}")
                if hasattr(profile, 'voice') and profile.voice:
                    v = profile.voice
                    lines.append(f"- Voice: {v.summary or '无'}")
                if hasattr(profile, 'boundary') and profile.boundary:
                    b = profile.boundary
                    if b.hard_rules:
                        lines.append(f"- 硬底线: {', '.join(b.hard_rules[:3])}")

        lines.append(f"\n## 前一章结尾（叙事衔接）")
        ending = ctx.get("previous_chapter_ending", "")
        lines.append(ending[-3000:] if len(ending) > 3000 else ending)

        return "\n".join(lines) + "\n\n用户任务: 为下一章规划大纲。请依次调用 analyze_hanging_threads → sketch_character_beats → plan_structure。"

    async def run(self, task: str = ""):
        """运行 Plot Architect 的 ReAct 循环。"""
        prefix = self._build_dynamic_prefix()
        if prefix:
            task = prefix + task
        return await super().run(task)

    def _build_tools(self) -> list:
        ctx = self._ctx
        kg = self._kg
        llm = self._llm
        outline_ctx = self._outline_context

        @tool
        def analyze_hanging_threads() -> str:
            """从知识图谱中提取所有未解决的伏笔和活跃冲突。

            查询 KG 的因果关系链，找出 effect 尚未在已覆盖章节中实现的事件。
            同时提取敌对角色的未解决冲突。

            Returns:
                JSON 格式的伏笔和冲突列表
            """
            graph = ctx.novel.story_graph if ctx.novel else None
            if not graph:
                return json.dumps({"hanging_threads": [], "active_conflicts": [],
                                   "message": "KG 不可用"}, ensure_ascii=False)

            last_ch = outline_ctx.get("last_chapter", 0)

            # 因果链中的未解决事件
            hanging = []
            for edge in graph.event_relation_edges:
                if edge.relation_type == "causes":
                    ev = graph.get_event_node(edge.from_event.split(":", 1)[-1])
                    if ev:
                        ev_end = ev.chapter_end or ev.chapter_start
                        # 如果事件的 effect 还未在新章节中体现
                        if ev_end <= last_ch and ev.effect:
                            hanging.append({
                                "event": ev.name,
                                "chapter": ev_end,
                                "effect": ev.effect,
                                "status": "pending",
                            })

            # 敌对关系冲突
            conflicts = []
            for pair in kg.enemy_pairs(graph):
                rel = graph.get_relationship_edge(pair[0], pair[1])
                if rel:
                    conflicts.append({
                        "characters": list(pair),
                        "tension": rel.current_tension or "?",
                        "shared_history": rel.shared_history or "",
                    })

            return json.dumps({
                "hanging_threads": hanging[:10],
                "active_conflicts": conflicts[:5],
                "total_hanging": len(hanging),
            }, ensure_ascii=False)

        @tool
        def sketch_character_beats(character_names: str) -> str:
            """为主要角色规划本章的情绪弧线和关键行动。

            每个角色需要定义:
            - arc: 本章情绪变化轨迹（如 "从犹豫到决断"）
            - key_action: 本章该角色的关键行动
            - emotional_beat: 关键情感时刻

            Args:
                character_names: 逗号分隔的角色名列表（如 "江停,严峫"）

            Returns:
                JSON 格式的角色节拍
            """
            names = [n.strip() for n in character_names.split(",") if n.strip()]
            graph = ctx.novel.story_graph if ctx.novel else None

            char_info = {}
            if graph:
                for name in names[:8]:  # 最多 8 个角色
                    person = kg.get_person(graph, name)
                    if person:
                        relations = kg.get_relations(graph, name)
                        char_info[name] = {
                            "role": person.role_type,
                            "importance": person.importance,
                            "status": person.status,
                            "faction": person.faction,
                            "relations": [
                                {
                                    "with": r.to_char if r.from_char == name else r.from_char,
                                    "type": r.relation_type,
                                    "intimacy": r.intimacy,
                                }
                                for r in relations[:5]
                            ],
                        }

            # 使用 LLM 规划节拍
            try:
                result = llm.chat_json(
                    system_prompt="你是专业剧情规划师。为每个角色设计本章的情绪弧线和关键行动。只返回 JSON。",
                    user_prompt=(
                        f"角色信息:\n{json.dumps(char_info, ensure_ascii=False, indent=2)}\n\n"
                        f"规划 {len(names)} 个角色在本章的情绪变化轨迹和关键行动。\n"
                        f"返回 JSON: {{characters: {{角色名: {{arc, key_action, emotional_beat}}}} }}"
                    ),
                    temperature=0.5,
                    max_tokens=2048,
                )
                if isinstance(result, dict):
                    return json.dumps(result, ensure_ascii=False)
            except Exception:
                pass

            # Fallback
            fallback = {
                name: {"arc": "持续推进", "key_action": "参与关键事件",
                       "emotional_beat": "对事件做出反应"}
                for name in names
            }
            return json.dumps({"characters": fallback}, ensure_ascii=False)

        @tool
        def plan_structure(arc_spec: str) -> str:
            """生成章节结构：起承转合 + 章尾悬念钩子。

            Args:
                arc_spec: 角色节拍和伏笔分析的 JSON 摘要

            Returns:
                章节结构 JSON（opening, rising, climax, hook, 预估字数）
            """
            style = outline_ctx.get("style_summary", "")
            prev_ending = outline_ctx.get("previous_chapter_ending", "")
            instruction = outline_ctx.get("user_instruction", "")
            last_ch = outline_ctx.get("last_chapter", 0)

            try:
                result = llm.chat_json(
                    system_prompt=(
                        "你是专业的小说章节结构设计师。"
                        "基于给定的伏笔、角色节拍和文风约束，设计一章完整的叙事结构。"
                        "包括: opening(开篇锚定), rising(推进), climax(高潮), hook(章尾钩子)。"
                        "只返回 JSON。"
                    ),
                    user_prompt=(
                        f"## 文风约束\n{style}\n\n"
                        f"## 角色节拍 & 伏笔\n{arc_spec}\n\n"
                        f"## 前一章结尾\n{prev_ending[-1500:]}\n\n"
                        + (f"## 用户指令\n{instruction}\n\n" if instruction else "")
                        + f"为第 {last_ch + 1} 章设计结构。返回 JSON:\n"
                          f'{{"chapter_number": {last_ch + 1}, "title": "...", '
                          f'"synopsis": "...", "structure": {{"opening": "...", '
                          f'"rising": "...", "climax": "...", "hook": "..."}}, '
                          f'"plot_threads_advanced": ["..."], '
                          f'"plot_threads_introduced": ["..."], '
                          f'"tone": "...", "target_word_count": 3000}}'
                    ),
                    temperature=0.6,
                    max_tokens=2048,
                )
                if isinstance(result, dict):
                    result.setdefault("chapter_number", last_ch + 1)
                    result.setdefault("status", "ok")
                    return json.dumps(result, ensure_ascii=False)
            except Exception:
                pass

            return json.dumps({
                "chapter_number": last_ch + 1,
                "title": "续",
                "synopsis": "继续推进故事",
                "structure": {
                    "opening": "衔接上一章结尾",
                    "rising": "推进现有冲突",
                    "climax": "关键转折",
                    "hook": "悬念钩子",
                },
                "plot_threads_advanced": [],
                "plot_threads_introduced": [],
                "tone": "保持原作风格",
                "target_word_count": 3000,
                "status": "ok",
            }, ensure_ascii=False)

        return [
            analyze_hanging_threads,
            sketch_character_beats,
            plan_structure,
        ]
```

- [ ] **Step 3: 提交**

```bash
git add skills/plot_architect.md src/continuation/plot_architect.py
git commit -m "feat: add Plot Architect Agent with hanging thread analysis and structure planning

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Chapter Writer（章节写手）— 流式核心

**Files:**
- Create: `src/continuation/chapter_writer.py`

**Interfaces:**
- Consumes: `StoryFragment` from Task 1, `AuthorStyleProfile` from Task 1, `UnifiedLLM` from `src/llm.py`
- Consumes: KnowledgeGraphService, CharacterProfile
- Produces: `ChapterWriter.stream(outline)` → AsyncGenerator[StoryFragment, None]
- Produces: `ChapterWriter.inject(instruction)` → 触发流中断重连

- [ ] **Step 1: 实现 `chapter_writer.py`**

```python
# -*- coding: utf-8 -*-
"""ChapterWriter —— 章节写手（流式核心）。

不走 AgentFlow ReAct 循环。直接使用 httpx 异步流式请求 LLM API，
逐行解析输出为 StoryFragment，通过 async generator yield。

流式控制:
  - 正常流: 构建 prompt → stream LLM → 逐行解析 → yield StoryFragment
  - 注入中断: 收到 inject signal → abort 当前 stream → 拼接上下文 → 重新 stream
"""

import asyncio
import json
import os
from typing import AsyncGenerator, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..context import GlobalContext, ServiceRegistry
    from ..llm import UnifiedLLM


class ChapterWriter:
    """章节写手 —— 流式续写核心。

    不走 AgentFlow，通过 httpx 直接调用 LLM streaming API。
    以 StoryFragment 为单位逐条输出。

    用法:
        writer = ChapterWriter(ctx, services, llm)
        async for fragment in writer.stream(outline):
            send_sse(fragment)
    """

    def __init__(self, ctx: "GlobalContext", services: "ServiceRegistry",
                 llm: "UnifiedLLM"):
        self._ctx = ctx
        self._services = services
        self._llm = llm
        self._kg = services.kg

        # 注入控制
        self._inject_event = asyncio.Event()
        self._inject_instruction: str = ""
        self._aborted = False

        # 已生成的 fragments（用于 inject 重建上下文）
        self._generated_fragments: list = []

        # 运行时上下文
        self._outline: dict = {}
        self._style_profile = None
        self._previous_chapter_ending: str = ""
        self._character_profiles: dict = {}

    def set_context(
        self,
        outline: dict,
        style_profile,
        previous_chapter_ending: str,
        character_profiles: dict,
    ):
        """设置 Writer 运行时上下文。"""
        self._outline = outline
        self._style_profile = style_profile
        self._previous_chapter_ending = previous_chapter_ending
        self._character_profiles = character_profiles

    async def inject(self, instruction: str):
        """注入用户指令。触发当前流中断并重连。

        Args:
            instruction: 用户的自然语言指令
        """
        self._inject_instruction = instruction
        self._inject_event.set()
        self._aborted = True

    async def stream(self, outline: dict) -> AsyncGenerator["StoryFragment", None]:
        """流式生成章节内容。

        每次 yield 一个 StoryFragment。收到 inject signal 后
        abort 当前请求、拼接上下文、重新 stream。

        Args:
            outline: Plot Architect 生成的章节大纲

        Yields:
            StoryFragment: 逐个片段
        """
        from .fragment import StoryFragment

        # 首次生成
        async for fragment in self._do_stream(outline, ""):
            yield fragment

        # 处理注入循环
        while self._aborted:
            self._aborted = False
            instruction = self._inject_instruction
            self._inject_instruction = ""
            self._inject_event.clear()

            # 用已生成的文本 + 新指令作为上下文重新 stream
            continuation_context = self._build_continuation_context(instruction)
            async for fragment in self._do_stream(outline, continuation_context):
                yield fragment

    async def _do_stream(
        self, outline: dict, extra_context: str
    ) -> AsyncGenerator["StoryFragment", None]:
        """执行一次 streaming 请求。

        Args:
            outline: 章节大纲
            extra_context: 额外上下文（注入指令 + 已生成文本）
        """
        from .fragment import StoryFragment

        # 构建消息
        system_prompt = self._build_writer_system_prompt()
        user_prompt = self._build_writer_user_prompt(outline, extra_context)

        # 调用 LLM streaming API
        # 使用 httpx 异步流式请求
        import httpx

        api_key = os.getenv("AGENTFLOW_API_KEY", "")
        base_url = os.getenv("AGENTFLOW_BASE_URL", "https://api.deepseek.com/v1")
        model = os.getenv("AGENTFLOW_MODEL", "deepseek-chat")
        proxy = os.getenv("AGENTFLOW_PROXY", "")

        url = f"{base_url.rstrip('/')}/chat/completions"

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.7,
            "max_tokens": 4096,
            "stream": True,
        }

        line_buffer = ""

        client_kwargs = {"timeout": httpx.Timeout(300.0, connect=30.0)}
        if proxy:
            client_kwargs["proxy"] = proxy

        async with httpx.AsyncClient(**client_kwargs) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                async for line in response.aiter_lines():
                    # 检查是否需要 abort
                    if self._aborted:
                        # 不等待 response.close()，直接 break
                        break

                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]  # 去掉 "data: " 前缀
                    if data_str == "[DONE]":
                        break

                    try:
                        chunk = json.loads(data_str)
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content", "")
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue

                    if not content:
                        continue

                    line_buffer += content

                    # 按换行拆分，尝试解析完整 fragment
                    while "\n" in line_buffer:
                        line_part, line_buffer = line_buffer.split("\n", 1)
                        fragment = StoryFragment.parse_stream_line(line_part)
                        if fragment:
                            self._generated_fragments.append(fragment)
                            yield fragment

                # 处理 buffer 中剩余的文本
                if line_buffer.strip() and not self._aborted:
                    fragment = StoryFragment.parse_stream_line(line_buffer.strip())
                    if fragment:
                        self._generated_fragments.append(fragment)
                        yield fragment

    def _build_continuation_context(self, instruction: str) -> str:
        """构建注入后重连的上下文。"""
        parts = [f"[用户指令] {instruction}\n"]
        parts.append("[已生成内容] 请从以下内容的结尾处自然衔接继续写:\n")

        # 取最后 10 个 fragment 作为上下文
        recent = self._generated_fragments[-10:] if len(self._generated_fragments) > 10 else self._generated_fragments
        for f in recent:
            if f.character:
                parts.append(f"[{f.type}] {f.character}: {f.text}")
            else:
                parts.append(f"[{f.type}] {f.text}")

        parts.append("\n继续写（不要重复上面已有的内容，从下一个自然段开始）:")
        return "\n".join(parts)

    def _build_writer_system_prompt(self) -> str:
        """构建 Chapter Writer 的 system prompt。"""
        parts = [
            "## 角色",
            "你是专业小说续写者。你需要根据大纲、文风约束和角色设定，以结构化片段格式续写小说内容。",
            "",
            "## 输出格式",
            "严格以 StoryFragment JSON 格式逐行输出，每行一个完整的 JSON 对象:",
            "",
            '  {"type": "narration", "text": "旁白/叙述文本..."}',
            '  {"type": "dialogue", "character": "角色名", "text": "对话内容..."}',
            '  {"type": "action", "character": "角色名", "text": "动作描写..."}',
            '  {"type": "inner_thought", "character": "角色名", "text": "内心独白..."}',
            '  {"type": "divider", "text": "", "divider_label": "时间/地点标签"}',
            "",
            "## 规则",
            "1. 每行一个完整的 JSON，行末不要有逗号",
            "2. dialogue 和 inner_thought 的 text 中不要包含引号",
            "3. action 是小字附加在角色名下，text 要简短（<30字）",
            "4. narration 用于场景描写和第三人称旁白",
            "5. 对话和动作交替推进故事，不要连续输出太长的 narration",
            "6. 保持原作叙事风格和角色性格一致性",
            "7. 不要输出 JSON 以外的任何内容（不要解释、不要评论）",
        ]

        # 注入文风约束
        if self._style_profile:
            parts.append("\n" + self._style_profile.summary())
            exemplars_text = self._style_profile.exemplars_text()
            if exemplars_text:
                parts.append("\n" + exemplars_text)

        # 注入角色约束
        if self._character_profiles:
            parts.append("\n## 角色行为约束")
            for name, profile in self._character_profiles.items():
                parts.append(f"\n### {name}")
                if hasattr(profile, 'voice') and profile.voice:
                    v = profile.voice
                    if v.summary:
                        parts.append(f"- Voice: {v.summary}")
                    if v.taboo_words:
                        parts.append(f"- 禁用词: {', '.join(v.taboo_words)}")
                if hasattr(profile, 'boundary') and profile.boundary:
                    b = profile.boundary
                    if b.hard_rules:
                        parts.append(f"- 硬底线: {', '.join(b.hard_rules)}")
                if hasattr(profile, 'policy_anchors') and profile.policy_anchors:
                    anchors = profile.policy_anchors
                    if anchors:
                        parts.append("- 行为参考:")
                        for a in anchors[:3]:
                            if hasattr(a, 'situation') and hasattr(a, 'action'):
                                parts.append(f"  - {a.situation} → {a.action}")

        return "\n".join(parts)

    def _build_writer_user_prompt(self, outline: dict, extra_context: str = "") -> str:
        """构建 user prompt（含大纲 + 前一章结尾 + 额外上下文）。"""
        parts = [
            "## 写作文本",
            f"章节: 第 {outline.get('chapter_number', '?')} 章「{outline.get('title', '')}」",
            f"梗概: {outline.get('synopsis', '')}",
        ]

        structure = outline.get("structure", {})
        if structure:
            parts.append(f"开篇: {structure.get('opening', '')}")
            parts.append(f"推进: {structure.get('rising', '')}")
            parts.append(f"高潮: {structure.get('climax', '')}")
            parts.append(f"钩子: {structure.get('hook', '')}")

        plot_advanced = outline.get("plot_threads_advanced", [])
        if plot_advanced:
            parts.append(f"推进伏笔: {', '.join(plot_advanced)}")

        if self._previous_chapter_ending:
            ending = self._previous_chapter_ending
            parts.append(f"\n## 前一章结尾\n{ending[-2000:] if len(ending) > 2000 else ending}")

        if extra_context:
            parts.append(f"\n{extra_context}")

        parts.append("\n现在从上一章结尾的自然衔接点开始续写。直接输出 StoryFragment JSON 序列。")
        return "\n".join(parts)
```

- [ ] **Step 2: 提交**

```bash
git add src/continuation/chapter_writer.py
git commit -m "feat: add Chapter Writer with streaming API and inject support

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Consistency Reviewer & Revision Editor

**Files:**
- Create: `skills/consistency_reviewer.md`
- Create: `src/continuation/consistency_reviewer.py`
- Create: `skills/revision_editor.md`
- Create: `src/continuation/revision_editor.py`

**Interfaces:**
- Consumes: `BaseAgent`, `StoryFragment`, KnowledgeGraphService
- Produces: `ConsistencyReviewer.run(draft_fragments)` → dict（问题列表）
- Produces: `RevisionEditor.run(draft_fragments, issues)` → dict（修订后 fragments + 变更记录）

- [ ] **Step 1: 创建 skill 文件 `skills/consistency_reviewer.md`**

```markdown
---
name: consistency_reviewer
description: 对照知识图谱检查续写草稿的一致性——角色 OOC、时间线、设定矛盾
---

## Role
你是专业的一致性审校编辑 (Consistency Reviewer)。
你的职责是：对照知识图谱中的角色设定、事件时间线和关系数据，检查续写草稿是否存在一致性问题。

## 核心资源
- **知识图谱**：角色状态（生死/位置）、关系网、事件时间线
- **角色 Profile**：Voice（说话风格）、Boundary（行为底线）、Policy Anchors（行为锚点）
- **文风 Profile**：原作的叙事风格和氛围

## 工具使用指南
1. **check_character_consistency(draft)** — 检查角色是否 OOC（对话风格、行为底线）
2. **check_timeline(draft)** — 检查事件时间线是否与 KG 一致
3. **check_setting_consistency(draft)** — 检查是否与已有设定矛盾

## 审校原则
1. 标注问题严重度：critical(角色已死却出现) > high(严重OOC) > medium(轻微的设定偏差) > low(建议性优化)
2. 每个问题必须附带具体建议（建议修改后的文本）
3. 不要过度审校——允许合理的角色成长和情节发展
4. 评分 0-10，8 分以上为良好
```

- [ ] **Step 2: 创建 skill 文件 `skills/revision_editor.md`**

```markdown
---
name: revision_editor
description: 根据审校问题列表对草稿进行局部修订，只修改有问题的片段
---

## Role
你是专业的修订编辑 (Revision Editor)。
你的职责是：根据审校报告中的问题列表，对续写草稿进行局部修订。

## 修订原则
1. **最小改动**: 只修改有问题的 fragment，不重写整章
2. **保持流畅**: 修订后叙事过渡自然
3. **遵循建议**: 优先采纳审校报告中给出的修改建议
4. **记录变更**: 每处修改都要记录 original → revised → reason

## 工具使用
直接基于审校报告中的问题逐条修订。修改对应的 fragment 文本。
```

- [ ] **Step 3: 实现 `consistency_reviewer.py`**

```python
# -*- coding: utf-8 -*-
"""ConsistencyReviewer —— 一致性审校 Agent。

继承 BaseAgent，通过 ReAct 循环检查草稿的一致性:
  - 角色 OOC (check_character_consistency)
  - 时间线 (check_timeline)
  - 设定矛盾 (check_setting_consistency)
"""

import json
from typing import TYPE_CHECKING

from agentflow.runtime.toolkit import tool

from ..agents.base_agent import BaseAgent

if TYPE_CHECKING:
    from ..context import GlobalContext, ServiceRegistry
    from ..llm import UnifiedLLM


class ConsistencyReviewer(BaseAgent):
    """一致性审校 Agent。

    对照 KG + 角色 Profile 检查草稿的一致性。
    """

    SKILL_NAME = "consistency_reviewer"

    def __init__(self, ctx, services, llm, memory=None):
        super().__init__(ctx, services, llm, memory)
        self._kg = services.kg
        self._draft_fragments: list = []
        self._character_profiles: dict = {}
        self._style_profile = None

    def set_context(self, draft_fragments: list, character_profiles: dict,
                    style_profile=None):
        """设置审校上下文。"""
        self._draft_fragments = draft_fragments
        self._character_profiles = character_profiles
        self._style_profile = style_profile

    def _build_dynamic_prefix(self) -> str:
        """构建动态前缀。"""
        # 简化为 fragment 文本
        draft_text = "\n".join(
            f"[{i}] [{f.type}] " + (f"{f.character}: " if f.character else "") + f.text
            for i, f in enumerate(self._draft_fragments)
        )
        return f"## 待审校草稿\n{draft_text[:6000]}\n\n请依次调用 check_character_consistency → check_timeline → check_setting_consistency"

    async def run(self, task: str = ""):
        prefix = self._build_dynamic_prefix()
        if prefix:
            task = prefix + task
        return await super().run(task)

    def _build_tools(self) -> list:
        ctx = self._ctx
        kg = self._kg
        llm = self._llm
        fragments = self._draft_fragments
        char_profiles = self._character_profiles

        @tool
        def check_character_consistency(draft_text: str = "") -> str:
            """检查草稿中角色是否 OOC。

            对照每个角色的 Voice 和 Boundary，检查对话风格和行为是否一致。

            Returns:
                JSON 格式的 OOC 问题列表
            """
            if not char_profiles:
                return json.dumps({"issues": [], "message": "无角色 Profile 数据"}, ensure_ascii=False)

            # 构建角色约束摘要
            char_specs = {}
            for name, profile in char_profiles.items():
                spec = {}
                if hasattr(profile, 'voice') and profile.voice:
                    spec["voice_summary"] = profile.voice.summary or ""
                    spec["taboo_words"] = profile.voice.taboo_words or []
                if hasattr(profile, 'boundary') and profile.boundary:
                    spec["hard_rules"] = profile.boundary.hard_rules or []
                char_specs[name] = spec

            # 提取草稿中角色对话
            char_dialogues = {}
            for i, f in enumerate(fragments):
                if f.type in ("dialogue", "inner_thought", "action") and f.character:
                    char_dialogues.setdefault(f.character, []).append(
                        f"[{i}] [{f.type}] {f.text}"
                    )

            if not char_dialogues:
                return json.dumps({"issues": []}, ensure_ascii=False)

            try:
                result = llm.chat_json(
                    system_prompt="你是角色一致性检查器。检查角色的对话/行为是否与其设定一致。只返回 JSON。",
                    user_prompt=(
                        f"## 角色设定\n{json.dumps(char_specs, ensure_ascii=False, indent=2)}\n\n"
                        f"## 草稿中的角色表现\n{json.dumps(char_dialogues, ensure_ascii=False, indent=2)}\n\n"
                        f"检查每个角色是否 OOC，返回 JSON:\n"
                        f'{{"issues": [{{"type": "character_ooc", "severity": "medium", '
                        f'"location": "片段序号", "character": "角色名", '
                        f'"description": "问题描述", "suggestion": "修改建议"}}]}}'
                    ),
                    temperature=0.3,
                    max_tokens=2048,
                )
                if isinstance(result, dict):
                    return json.dumps(result, ensure_ascii=False)
            except Exception:
                pass

            return json.dumps({"issues": []}, ensure_ascii=False)

        @tool
        def check_timeline(draft_text: str = "") -> str:
            """检查草稿中的时间线是否与 KG 中的事件顺序一致。

            Returns:
                JSON 格式的时间线问题列表
            """
            graph = ctx.novel.story_graph if ctx.novel else None
            if not graph:
                return json.dumps({"issues": [], "message": "KG 不可用"}, ensure_ascii=False)

            # 提取 KG 中最近的事件时间线
            events = graph.event_nodes
            timeline = [
                {"name": ev.name, "chapter_start": ev.chapter_start,
                 "chapter_end": ev.chapter_end or ev.chapter_start}
                for ev in events[-20:]
            ]

            draft_snippet = "\n".join(
                f"[{i}] [{f.type}] {f.text[:100]}"
                for i, f in enumerate(fragments[:30])
            )

            try:
                result = llm.chat_json(
                    system_prompt="你是时间线一致性检查器。检查续写内容是否与已有事件时间线矛盾。只返回 JSON。",
                    user_prompt=(
                        f"## 已有事件时间线\n{json.dumps(timeline, ensure_ascii=False, indent=2)}\n\n"
                        f"## 草稿内容\n{draft_snippet}\n\n"
                        f"检查草稿中是否出现时间线矛盾（如: 已死角色出现、事件顺序颠倒）。"
                        f"返回 JSON: {{\"issues\": [...]}}"
                    ),
                    temperature=0.2,
                    max_tokens=1024,
                )
                if isinstance(result, dict):
                    return json.dumps(result, ensure_ascii=False)
            except Exception:
                pass

            return json.dumps({"issues": []}, ensure_ascii=False)

        @tool
        def check_setting_consistency(draft_text: str = "") -> str:
            """检查草稿是否与已有设定矛盾。

            Returns:
                JSON 格式的设定问题列表
            """
            graph = ctx.novel.story_graph if ctx.novel else None
            if not graph:
                return json.dumps({"issues": []}, ensure_ascii=False)

            # 提取关键设定: 角色状态、组织从属、地点关系
            persons = kg.get_all_persons(graph)[:15]
            settings = [
                {"name": p.name, "status": p.status, "faction": p.faction,
                 "importance": p.importance}
                for p in persons
            ]

            draft_snippet = "\n".join(
                f"[{i}] [{f.type}] {f.text[:150]}"
                for i, f in enumerate(fragments[:30])
            )

            try:
                result = llm.chat_json(
                    system_prompt="你是设定一致性检查器。检查续写内容是否与已有设定矛盾。只返回 JSON。",
                    user_prompt=(
                        f"## 已有设定\n{json.dumps(settings, ensure_ascii=False, indent=2)}\n\n"
                        f"## 草稿内容\n{draft_snippet}\n\n"
                        f"检查是否有设定矛盾。返回 JSON: {{\"issues\": [...]}}"
                    ),
                    temperature=0.2,
                    max_tokens=1024,
                )
                if isinstance(result, dict):
                    return json.dumps(result, ensure_ascii=False)
            except Exception:
                pass

            return json.dumps({"issues": []}, ensure_ascii=False)

        return [
            check_character_consistency,
            check_timeline,
            check_setting_consistency,
        ]
```

- [ ] **Step 4: 实现 `revision_editor.py`**

```python
# -*- coding: utf-8 -*-
"""RevisionEditor —— 修订编辑 Agent。

根据审校问题列表对草稿做局部修订。只修改有问题的 fragment，不重写整章。
"""

import json
from typing import TYPE_CHECKING

from ..agents.base_agent import BaseAgent
from .fragment import StoryFragment

if TYPE_CHECKING:
    from ..context import GlobalContext, ServiceRegistry
    from ..llm import UnifiedLLM


class RevisionEditor(BaseAgent):
    """修订编辑 Agent。

    根据审校结果做局部修订。不走 ReAct，直接用 LLM 做针对性修改。
    """

    SKILL_NAME = "revision_editor"

    def __init__(self, ctx, services, llm, memory=None):
        super().__init__(ctx, services, llm, memory)
        self._llm_client = llm

    async def run(self, task: str = ""):
        """修订草稿。

        task 格式: JSON 字符串 {"draft": [...], "issues": [...]}
        """
        try:
            data = json.loads(task) if isinstance(task, str) else task
        except json.JSONDecodeError:
            return json.dumps({"revised_fragments": [], "changes": [],
                               "error": "Invalid input"}, ensure_ascii=False)

        draft = data.get("draft", [])
        issues = data.get("issues", [])

        if not issues:
            # 没有问题，直接返回原稿
            return json.dumps({
                "revised_fragments": draft,
                "changes": [],
                "status": "ok",
            }, ensure_ascii=False)

        # 将 fragment 转换为文本格式
        draft_text = "\n".join(
            f"[{i}] {{{f.get('type', '?')}}} "
            + (f"{f.get('character', '')}: " if f.get('character') else "")
            + f.get('text', '')
            for i, f in enumerate(draft)
        )

        try:
            result = self._llm_client.chat_json(
                system_prompt=(
                    "你是专业的修订编辑。根据问题列表修改草稿中的对应片段。"
                    "只修改有问题的 fragment，其他地方保持不变。"
                    "返回整个 fragment 列表（含未修改的）。只返回 JSON。"
                ),
                user_prompt=(
                    f"## 原草稿\n{draft_text[:5000]}\n\n"
                    f"## 问题列表\n{json.dumps(issues, ensure_ascii=False, indent=2)}\n\n"
                    f"返回 JSON:\n"
                    f'{{"revised_fragments": [{{"type": "...", "text": "...", '
                    f'"character": "..."}}], '
                    f'"changes": [{{"fragment_index": 0, "original": "...", '
                    f'"revised": "...", "reason": "..."}}]}}'
                ),
                temperature=0.3,
                max_tokens=4096,
            )
            if isinstance(result, dict):
                return json.dumps(result, ensure_ascii=False)
        except Exception:
            pass

        return json.dumps({
            "revised_fragments": draft,
            "changes": [],
            "status": "revision_failed",
        }, ensure_ascii=False)
```

- [ ] **Step 5: 提交**

```bash
git add skills/consistency_reviewer.md skills/revision_editor.md \
        src/continuation/consistency_reviewer.py src/continuation/revision_editor.py
git commit -m "feat: add Consistency Reviewer and Revision Editor agents

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Continuation Pipeline（流水线编排器）

**Files:**
- Create: `src/continuation/pipeline.py`

**Interfaces:**
- Consumes: `PlotArchitect`, `ChapterWriter`, `ConsistencyReviewer`, `RevisionEditor` from Tasks 3-5
- Consumes: `PipelineEvent` from Task 1, `GlobalContext`, `AuthorStyleDistiller`
- Produces: `ContinuationPipeline.run(instruction)` → AsyncGenerator[PipelineEvent, None]
- Produces: `ContinuationPipeline.inject(instruction)` → 转发到 Writer

- [ ] **Step 1: 实现 `pipeline.py`**

```python
# -*- coding: utf-8 -*-
"""ContinuationPipeline —— 续写流水线编排器。

串联 4 个 Agent:
  ① Plot Architect → 生成大纲
  ② Chapter Writer → 流式写作（核心）
  ③ Consistency Reviewer → 审校（异步，不阻塞前端）
  ④ Revision Editor → 修订（异步，不阻塞前端）

管理 SSE 事件总线，通过 AsyncGenerator 将事件推送到 HTTP handler。
"""

import asyncio
import json
from typing import AsyncGenerator, Optional, TYPE_CHECKING

from .fragment import PipelineEvent, StoryFragment
from .plot_architect import PlotArchitect
from .chapter_writer import ChapterWriter
from .consistency_reviewer import ConsistencyReviewer
from .revision_editor import RevisionEditor

if TYPE_CHECKING:
    from ..context import GlobalContext, ServiceRegistry
    from ..llm import UnifiedLLM


class ContinuationPipeline:
    """续写流水线编排器。

    用法:
        pipeline = ContinuationPipeline(ctx, services, llm)
        pipeline.load_novel(novel_path)

        async for event in pipeline.run("让江停更主动"):
            send_sse(event)
    """

    def __init__(self, ctx: "GlobalContext", services: "ServiceRegistry",
                 llm: "UnifiedLLM"):
        self._ctx = ctx
        self._services = services
        self._llm = llm
        self._kg = services.kg

        # 4 个 Agent
        self.architect: Optional[PlotArchitect] = None
        self.writer: Optional[ChapterWriter] = None
        self.reviewer: Optional[ConsistencyReviewer] = None
        self.editor: Optional[RevisionEditor] = None

        # 状态
        self._phase: str = "idle"
        self._chapter: int = 0
        self._fragment_count: int = 0

        # 缓存数据
        self._style_profile = None
        self._character_profiles: dict = {}
        self._previous_chapter_ending: str = ""

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def chapter(self) -> int:
        return self._chapter

    @property
    def fragment_count(self) -> int:
        return self._fragment_count

    def load_novel(self, novel_path: str):
        """加载小说并准备续写上下文。

        包括:
          1. 解析章节
          2. 提取/加载 KG
          3. 蒸馏角色 Profile（已有）
          4. 蒸馏文风 Profile（新增）
          5. 获取最后一章结尾

        Args:
            novel_path: 小说文件路径（如 novels/poyun.txt）
        """
        from ..chapter_parser import parse_novel_chapters
        from ..models import Novel
        from ..services.project_service import ProjectService as PS
        from ..character_distiller import CharacterDistiller
        from .author_style_distiller import AuthorStyleDistiller

        # 1. 加载文本 & 解析章节
        text = PS.read_text_file(novel_path)
        base_name = novel_path.replace("\\", "/").split("/")[-1].rsplit(".", 1)[0]
        chapters = parse_novel_chapters(text, base_name)

        # 2. 创建 Novel → 提取 KG
        project_dir = self._services.project.create_project_dir(base_name)
        self._ctx.novel = Novel(
            title=base_name,
            file_path=novel_path,
            chapters=chapters,
            output_dir=project_dir,
        )

        self._ctx.novel.story_graph = self._services.kg.extract_incremental(
            chapters,
            batch_size=int(__import__('os').getenv("KG_BATCH_SIZE", "10")),
        )
        self._services.project.save_novel(self._ctx.novel)

        graph = self._ctx.novel.story_graph
        self._chapter = len(chapters)

        # 3. 蒸馏文风 Profile
        distiller = AuthorStyleDistiller(self._llm)
        # 检查是否有缓存的 style profile
        cached_style = self._ctx.novel.output_dir and self._load_cached_style(
            self._ctx.novel.output_dir
        )
        if cached_style:
            self._style_profile = cached_style
        else:
            self._style_profile = distiller.distill(text)
            if self._ctx.novel.output_dir:
                self._save_cached_style(self._ctx.novel.output_dir, self._style_profile)

        # 4. 蒸馏主要角色 Profile（importance >= 6）
        char_distiller = CharacterDistiller(self._llm, self._kg)
        persons = self._kg.get_all_persons(graph)
        important = [p for p in persons if p.importance >= 5]
        for person in important[:8]:
            try:
                profile = char_distiller.distill_character(
                    person.name, text, graph,
                )
                self._character_profiles[person.name] = profile
            except Exception:
                pass

        # 5. 提取最后一章结尾
        if chapters:
            last_ch = chapters[-1]
            self._previous_chapter_ending = last_ch.content[-3000:] if len(last_ch.content) > 3000 else last_ch.content

        # 6. 初始化 Agent
        self._init_agents()

    def _init_agents(self):
        """初始化 4 个 Agent。"""
        self.architect = PlotArchitect(self._ctx, self._services, self._llm)
        self.writer = ChapterWriter(self._ctx, self._services, self._llm)
        self.reviewer = ConsistencyReviewer(self._ctx, self._services, self._llm)
        self.editor = RevisionEditor(self._ctx, self._services, self._llm)

    async def run(self, instruction: str = "") -> AsyncGenerator[PipelineEvent, None]:
        """运行续写流水线。

        Args:
            instruction: 用户初始指令（可选）

        Yields:
            PipelineEvent: 每个事件通过 SSE 推送
        """
        if not self.architect or not self.writer:
            raise RuntimeError("请先调用 load_novel() 加载小说")

        # —— 阶段 1: 大纲 ——
        self._phase = "planning"
        yield PipelineEvent("phase", {"phase": "planning"})

        self.architect.set_context(
            previous_chapter_ending=self._previous_chapter_ending,
            style_profile=self._style_profile,
            character_profiles=self._character_profiles,
            last_chapter=self._chapter,
            user_instruction=instruction,
        )

        architect_result_raw = await self.architect.run(
            f"为第 {self._chapter + 1} 章规划大纲"
        )

        # 解析 Plot Architect 的输出（可能是 ReAct 的自然文本终止）
        outline = self._parse_outline(architect_result_raw)
        yield PipelineEvent("outline", outline)

        # —— 阶段 2: 写作 ——
        self._phase = "writing"
        yield PipelineEvent("phase", {"phase": "writing"})

        self.writer.set_context(
            outline=outline,
            style_profile=self._style_profile,
            previous_chapter_ending=self._previous_chapter_ending,
            character_profiles=self._character_profiles,
        )

        draft_fragments = []
        async for fragment in self.writer.stream(outline):
            draft_fragments.append(fragment)
            self._fragment_count += 1
            yield PipelineEvent("fragment", fragment.to_dict())

        # —— 阶段 3: 审校 ——
        self._phase = "reviewing"
        yield PipelineEvent("phase", {"phase": "reviewing"})

        self.reviewer.set_context(
            draft_fragments=draft_fragments,
            character_profiles=self._character_profiles,
            style_profile=self._style_profile,
        )
        review_result_raw = await self.reviewer.run("审校草稿")
        issues = self._parse_review(review_result_raw)
        yield PipelineEvent("review", issues)

        # —— 阶段 4: 修订 ——
        self._phase = "revising"
        yield PipelineEvent("phase", {"phase": "revising"})

        if issues.get("issues"):
            revision_input = json.dumps({
                "draft": [f.to_dict() for f in draft_fragments],
                "issues": issues["issues"],
            })
            revision_result_raw = await self.editor.run(revision_input)
            revised = self._parse_revision(revision_result_raw)
            yield PipelineEvent("complete", revised)
        else:
            yield PipelineEvent("complete", {
                "fragments": [f.to_dict() for f in draft_fragments],
                "changes": [],
            })

        # 完成
        self._phase = "idle"
        yield PipelineEvent("done", {})

    async def inject(self, instruction: str):
        """接收用户注入指令，转发到 Chapter Writer。

        Args:
            instruction: 用户自然语言指令
        """
        if self.writer:
            await self.writer.inject(instruction)

    def _parse_outline(self, raw: str) -> dict:
        """从 Plot Architect 的 ReAct 输出中解析大纲。"""
        # AgentFlow 自然终止时返回的是 LLM 输出的文本
        # 可能是纯 JSON 或包含 JSON 的文本
        if isinstance(raw, dict):
            return raw

        text = str(raw).strip()
        # 尝试直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 尝试从文本中提取 JSON 块
        import re
        json_match = re.search(r'\{.*"chapter_number".*\}', text, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                pass

        # Fallback
        return {
            "chapter_number": self._chapter + 1,
            "title": "",
            "synopsis": "继续推进故事",
            "structure": {},
            "tone": "保持原作风格",
            "status": "parsed_fallback",
        }

    def _parse_review(self, raw: str) -> dict:
        """从 Reviewer 输出中解析问题列表。"""
        if isinstance(raw, dict):
            return raw
        try:
            return json.loads(str(raw).strip())
        except json.JSONDecodeError:
            pass
        return {"issues": [], "overall_score": 0}

    def _parse_revision(self, raw: str) -> dict:
        """从 Editor 输出中解析修订结果。"""
        if isinstance(raw, dict):
            return raw
        try:
            return json.loads(str(raw).strip())
        except json.JSONDecodeError:
            pass
        return {"revised_fragments": [], "changes": [], "status": "parse_failed"}

    def _load_cached_style(self, project_dir: str) -> Optional[object]:
        """从项目目录加载缓存的文风 Profile。"""
        import os
        from .author_style_profile import AuthorStyleProfile
        cache_path = os.path.join(project_dir, "author_style_profile.json")
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    return AuthorStyleProfile.from_dict(json.load(f))
            except Exception:
                pass
        return None

    def _save_cached_style(self, project_dir: str, profile):
        """缓存文风 Profile 到项目目录。"""
        import os
        cache_path = os.path.join(project_dir, "author_style_profile.json")
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(profile.to_dict(), f, ensure_ascii=False, indent=2)
        except Exception:
            pass
```

- [ ] **Step 2: 提交**

```bash
git add src/continuation/pipeline.py
git commit -m "feat: add ContinuationPipeline orchestrating 4 agents for streaming write flow

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: Server Write Handlers（续写 API）

**Files:**
- Create: `src/server/write_handlers.py`
- Modify: `src/server/__init__.py`

**Interfaces:**
- Consumes: `ContinuationPipeline` from Task 6, `GlobalContext`
- Produces: `POST /api/write/start` (SSE), `POST /api/write/inject`, `GET /api/write/state`

- [ ] **Step 1: 实现 `write_handlers.py`**

```python
# -*- coding: utf-8 -*-
"""续写 API Handlers —— REST + SSE。

端点:
  POST /api/write/start   — 启动续写，返回 SSE 流
  POST /api/write/inject  — 注入用户指令
  GET  /api/write/state   — 查询续写状态
"""

import asyncio
import json
import os
import threading
import queue

import tornado.web

from ..continuation.pipeline import ContinuationPipeline


# 全局 pipeline（单例，同一时间只有一个续写会话）
_active_pipeline: ContinuationPipeline = None
_pipeline_lock = threading.Lock()


def _get_or_create_pipeline(ctx, services, llm, novel_path: str) -> ContinuationPipeline:
    """获取或创建活跃的 pipeline 实例。

    同一时间只允许一个续写会话运行。新的 start 会替换旧的 pipeline。
    """
    global _active_pipeline
    with _pipeline_lock:
        if _active_pipeline is not None and _active_pipeline.phase == "idle":
            # 复用已有的 pipeline（已加载过小说）
            pass
        else:
            _active_pipeline = ContinuationPipeline(ctx, services, llm)

        # 加载小说（如果未加载或加载的是不同小说）
        if _active_pipeline._chapter == 0:
            _active_pipeline.load_novel(novel_path)

    return _active_pipeline


class WriteStartHandler(tornado.web.RequestHandler):
    """POST /api/write/start — 启动续写，返回 SSE 事件流。"""

    def set_default_headers(self):
        self.set_header("Access-Control-Allow-Origin", "*")
        self.set_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.set_header("Access-Control-Allow-Headers", "Content-Type")

    def options(self):
        self.set_status(204)
        self.finish()

    async def post(self):
        body = json.loads(self.request.body or "{}")
        novel_path = body.get("novel_path", "")
        instruction = body.get("instruction", "")

        if not novel_path:
            self.set_status(400)
            self.write({"error": "novel_path is required"})
            return

        app = self.application
        ctx = app.settings.get("global_context")
        services = app.settings.get("services")
        llm = app.settings.get("llm")

        if not ctx or not services or not llm:
            self.set_status(500)
            self.write({"error": "Server not initialized"})
            return

        # SSE 响应头
        self.set_header("Content-Type", "text/event-stream")
        self.set_header("Cache-Control", "no-cache")
        self.set_header("Connection", "keep-alive")
        self.set_header("X-Accel-Buffering", "no")  # 禁用 nginx 缓冲

        try:
            pipeline = _get_or_create_pipeline(ctx, services, llm, novel_path)

            async for event in pipeline.run(instruction):
                sse_text = event.to_sse()
                self.write(sse_text)
                self.flush()

        except Exception as e:
            error_event = {
                "event": "error",
                "data": json.dumps({"message": str(e)}, ensure_ascii=False),
            }
            self.write(f"event: error\ndata: {json.dumps({'message': str(e)}, ensure_ascii=False)}\n\n")
            self.flush()
        finally:
            self.finish()


class WriteInjectHandler(tornado.web.RequestHandler):
    """POST /api/write/inject — 注入用户指令。"""

    def set_default_headers(self):
        self.set_header("Access-Control-Allow-Origin", "*")
        self.set_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.set_header("Access-Control-Allow-Headers", "Content-Type")

    def options(self):
        self.set_status(204)
        self.finish()

    async def post(self):
        body = json.loads(self.request.body or "{}")
        instruction = body.get("instruction", "").strip()

        if not instruction:
            self.set_status(400)
            self.write({"error": "instruction is required"})
            return

        if _active_pipeline is None:
            self.set_status(400)
            self.write({"error": "No active writing session. Start one first."})
            return

        # 异步注入指令
        try:
            await _active_pipeline.inject(instruction)
            self.write({"status": "ok", "message": f"指令已注入: {instruction[:50]}"})
        except Exception as e:
            self.set_status(500)
            self.write({"error": str(e)})


class WriteStateHandler(tornado.web.RequestHandler):
    """GET /api/write/state — 查询当前续写状态。"""

    def set_default_headers(self):
        self.set_header("Access-Control-Allow-Origin", "*")
        self.set_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.set_header("Access-Control-Allow-Headers", "Content-Type")

    def options(self):
        self.set_status(204)
        self.finish()

    def get(self):
        if _active_pipeline is None:
            self.write({"phase": "idle", "message": "No active session"})
            return

        self.write({
            "phase": _active_pipeline.phase,
            "chapter": _active_pipeline.chapter,
            "fragment_count": _active_pipeline.fragment_count,
        })
```

- [ ] **Step 2: 修改 `src/server/__init__.py`**

Register the new handlers:

```python
# -*- coding: utf-8 -*-
"""Web 服务器 —— Tornado 应用。

用法:
    from src.server import create_app
    app = create_app(ctx, services, llm)
    app.listen(8000)
    tornado.ioloop.IOLoop.current().start()
"""

import os
import tornado.web

from .session_manager import SessionManager
from .handlers import (
    set_session_manager,
    StartHandler,
    MessageHandler,
    ChoiceHandler,
    StateHandler,
    StreamHandler,
    HealthHandler,
)
from .write_handlers import (
    WriteStartHandler,
    WriteInjectHandler,
    WriteStateHandler,
)


def create_app(ctx, services, llm) -> tornado.web.Application:
    """创建 Tornado 应用。

    Args:
        ctx: GlobalContext
        services: ServiceRegistry
        llm: UnifiedLLM

    Returns:
        tornado.web.Application
    """

    # 全局 SessionManager（roleplay 用）
    session_mgr = SessionManager()
    set_session_manager(session_mgr)

    settings = {
        "global_context": ctx,
        "services": services,
        "llm": llm,
        "debug": True,
    }

    app = tornado.web.Application(
        [
            # Roleplay API（保持不动）
            (r"/api/health", HealthHandler),
            (r"/api/play/start", StartHandler),
            (r"/api/play/message", MessageHandler),
            (r"/api/play/choice", ChoiceHandler),
            (r"/api/play/state", StateHandler),
            (r"/api/play/stream", StreamHandler),
            # 续写 API（新增）
            (r"/api/write/start", WriteStartHandler),
            (r"/api/write/inject", WriteInjectHandler),
            (r"/api/write/state", WriteStateHandler),
        ],
        **settings,
    )

    return app


def start_server(ctx, services, llm, port: int = 8000):
    """启动 Web 服务器。

    Args:
        ctx: GlobalContext
        services: ServiceRegistry
        llm: UnifiedLLM
        port: 监听端口
    """
    import tornado.ioloop

    app = create_app(ctx, services, llm)
    app.listen(port)
    print(f"\n{'='*50}")
    print(f"  📖 Novel2Comic Web 服务")
    print(f"  🎭 Roleplay API: http://localhost:{port}/api/play/*")
    print(f"  ✍️  续写 API: http://localhost:{port}/api/write/*")
    print(f"  按 Ctrl+C 停止服务")
    print(f"{'='*50}\n")
    tornado.ioloop.IOLoop.current().start()
```

- [ ] **Step 3: 提交**

```bash
git add src/server/write_handlers.py src/server/__init__.py
git commit -m "feat: add write API handlers (SSE streaming, inject, state)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: CLI 集成

**Files:**
- Modify: `src/cli/cli.py`

**Interfaces:**
- 新增 `write` 子命令：`python main.py write --novel novels/poyun.txt [--instruction "..."]`

- [ ] **Step 1: 修改 `src/cli/cli.py`**——在 `main()` 函数中添加 `write` 子命令

```python
# 在 main() 函数的子命令定义部分添加:

# write — 续写模式
wr = subparsers.add_parser("write", help="AI 自主续写模式")
wr.add_argument("--novel", type=str, required=True, help="小说文件路径")
wr.add_argument("--instruction", type=str, default="",
                help="初始续写方向指令（可选，如'让江停更主动'）")
```

```python
# 在 main() 函数的调度部分添加:

if args.command == "write":
    asyncio.run(run_write(args))
```

```python
# 新增 run_write 函数

async def run_write(args):
    """续写模式 —— 终端流式输出。"""
    api_key = _require_api_key()
    base_url = os.getenv("AGENTFLOW_BASE_URL", "https://api.deepseek.com/v1")
    model = os.getenv("AGENTFLOW_MODEL", "deepseek-chat")
    tool_model = os.getenv("AGENTFLOW_TOOL_MODEL", model)
    proxy = os.getenv("AGENTFLOW_PROXY", "")

    ctx, services, llm = _build_context_and_services(
        api_key, base_url, model, proxy, tool_model,
    )
    _load_novel(args.novel, services, ctx)

    from ..continuation.pipeline import ContinuationPipeline

    pipeline = ContinuationPipeline(ctx, services, llm)
    pipeline.load_novel(args.novel)

    print(f"\n[续写] 小说: {ctx.novel.title}")
    print(f"[续写] 当前进度: {pipeline.chapter} 章")
    print(f"[续写] 正在规划第 {pipeline.chapter + 1} 章...")
    print()

    instruction = getattr(args, "instruction", "")

    try:
        async for event in pipeline.run(instruction):
            if event.event_type == "phase":
                phase = event.data.get("phase", "")
                labels = {
                    "planning": "📋 正在规划大纲...",
                    "writing": "✍️ 正在写作...",
                    "reviewing": "🔍 正在一致性审校...",
                    "revising": "📝 正在修订...",
                }
                if phase in labels:
                    print(f"\n{labels[phase]}")
            elif event.event_type == "outline":
                outline = event.data
                print(f"  章标题: {outline.get('title', '?')}")
                print(f"  梗概: {outline.get('synopsis', '?')[:120]}...")
            elif event.event_type == "fragment":
                frag = event.data
                self._print_fragment_terminal(frag)
            elif event.event_type == "review":
                issues = event.data.get("issues", [])
                score = event.data.get("overall_score", "?")
                print(f"\n  审校完成: {len(issues)} 个问题 | 评分: {score}")
            elif event.event_type == "complete":
                print(f"\n{'='*50}")
                print(f"  ✅ 续写完成")
                print(f"{'='*50}")
            elif event.event_type == "error":
                print(f"\n❌ 错误: {event.data.get('message', '')}")
    except KeyboardInterrupt:
        print("\n[续写] 用户中断")
    except Exception as e:
        print(f"\n❌ 错误: {e}")


def _print_fragment_terminal(self, frag: dict):
    """在终端中显示一个 fragment。"""
    ftype = frag.get("type", "narration")
    text = frag.get("text", "")
    character = frag.get("character", "")

    if ftype == "narration":
        print(f"\n  {text}")
    elif ftype == "dialogue":
        print(f"\n  [{character}] {text}")
    elif ftype == "action":
        print(f"    ({text})")
    elif ftype == "inner_thought":
        print(f"\n  [{character}] ┆ {text} ┆")
    elif ftype == "divider":
        label = frag.get("divider_label", "")
        print(f"\n  {'─' * 20} {label} {'─' * 20}")
```

由于 `_print_fragment_terminal` 是独立函数（不是方法），做以下修正——将其改为模块级函数：

```python
def _print_fragment_terminal(frag: dict):
    """在终端中显示一个 fragment。"""
    ftype = frag.get("type", "narration")
    text = frag.get("text", "")
    character = frag.get("character", "")

    if ftype == "narration":
        print(f"\n  {text}")
    elif ftype == "dialogue":
        print(f"\n  [{character}] {text}")
    elif ftype == "action":
        print(f"    ({text})")
    elif ftype == "inner_thought":
        print(f"\n  [{character}] ┆ {text} ┆")
    elif ftype == "divider":
        label = frag.get("divider_label", "")
        print(f"\n  {'─' * 20} {label} {'─' * 20}")
```

并在 `run_write` 中将 `self._print_fragment_terminal(frag)` 改为 `_print_fragment_terminal(frag)`。

- [ ] **Step 2: 提交**

```bash
git add src/cli/cli.py
git commit -m "feat: add 'write' CLI command for continuation mode

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 9: 前端重写 —— 聊天小说 UI

**Files:**
- Modify: `frontend/index.html` — 完全重写

**Interfaces:**
- 连接 `POST /api/write/start` SSE 流
- 发送 `POST /api/write/inject` 用户指令
- 查询 `GET /api/write/state` 状态

- [ ] **Step 1: 重写 `frontend/index.html`**

由于前端文件较长（完整的聊天小说 UI），核心实现要点：

1. **启动画面**：输入小说路径 + 可选初始指令 → 点击"开始续写"
2. **阅读视图**：
   - 顶部栏：小说名 + 章节号 + 状态标签
   - 中间区域：fragment 流（可滚动）
   - 底部：指令输入栏 + 发送按钮
3. **SSE 连接**：`EventSource` 或 `fetch` + `ReadableStream`
4. **Fragment 渲染**：对应 5 种类型的 CSS 样式
5. **光标动画**：最新 fragment 有打字机效果
6. **注入**：POST /api/write/inject 发送指令

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>📖 续写引擎</title>
<style>
  :root {
    --bg: #0d1117;
    --bg-panel: #161b22;
    --bg-input: #21262d;
    --border: #30363d;
    --text: #c9d1d9;
    --text-dim: #8b949e;
    --accent: #d4a853;
    --accent-glow: #f0c060;
    --blue: #58a6ff;
    --purple: #bc8cff;
    --green: #3fb950;
    --danger: #f85149;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: Georgia, 'Noto Serif SC', 'Source Han Serif SC', serif;
    background: var(--bg); color: var(--text);
    min-height: 100vh; line-height: 1.7;
  }

  /* ===== 启动画面 ===== */
  #start-screen {
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    min-height: 100vh; padding: 2rem;
  }
  #start-screen h1 { font-size: 2.5rem; color: var(--accent); margin-bottom: 0.5rem; }
  #start-screen .subtitle { color: var(--text-dim); margin-bottom: 2rem; }
  #start-form {
    background: var(--bg-panel); border: 1px solid var(--border);
    border-radius: 12px; padding: 2rem; width: 100%; max-width: 480px;
  }
  #start-form label { display: block; color: var(--text-dim); font-size: 0.85rem; margin: 1rem 0 0.3rem; }
  #start-form input, #start-form textarea {
    width: 100%; padding: 0.6rem 0.8rem; background: var(--bg-input);
    border: 1px solid var(--border); border-radius: 6px;
    color: var(--text); font-family: inherit; font-size: 1rem;
    resize: vertical;
  }
  #start-form input:focus, #start-form textarea:focus {
    outline: none; border-color: var(--accent);
  }
  #start-form button {
    width: 100%; margin-top: 1.5rem; padding: 0.8rem;
    background: var(--accent); color: #0d1117;
    border: none; border-radius: 8px; font-size: 1.1rem;
    font-family: inherit; font-weight: bold; cursor: pointer;
  }

  /* ===== 阅读视图 ===== */
  #reader-screen { display: none; flex-direction: column; height: 100vh; }
  #top-bar {
    display: flex; align-items: center; justify-content: space-between;
    padding: 0.6rem 1.2rem; background: var(--bg-panel);
    border-bottom: 1px solid var(--border);
    position: sticky; top: 0; z-index: 10;
  }
  #top-bar .title { font-size: 1.1rem; font-weight: bold; color: var(--accent); }
  #top-bar .status { font-size: 0.85rem; color: var(--text-dim); }
  #top-bar .status.active { color: var(--green); }
  #fragment-container {
    flex: 1; overflow-y: auto; padding: 1.5rem;
    max-width: 700px; margin: 0 auto; width: 100%;
    scroll-behavior: smooth;
  }
  #bottom-bar {
    padding: 0.8rem 1.2rem; background: var(--bg-panel);
    border-top: 1px solid var(--border);
    display: flex; gap: 0.8rem;
  }
  #inject-input {
    flex: 1; padding: 0.6rem 0.8rem; background: var(--bg-input);
    border: 1px solid var(--border); border-radius: 8px;
    color: var(--text); font-family: inherit; font-size: 0.95rem;
  }
  #inject-input:focus { outline: none; border-color: var(--accent); }
  #inject-btn {
    padding: 0.6rem 1.2rem; background: var(--blue);
    color: white; border: none; border-radius: 8px;
    cursor: pointer; font-family: inherit; font-weight: bold;
  }
  #inject-btn:disabled { opacity: 0.5; cursor: not-allowed; }

  /* ===== Fragment 样式 ===== */
  .frag-narration {
    text-align: center; color: var(--text-dim);
    font-style: italic; margin: 1rem 0; padding: 0.5rem 1rem;
    border-top: 1px solid var(--border); border-bottom: 1px solid var(--border);
    font-size: 0.95rem;
  }
  .frag-divider {
    text-align: center; margin: 1.5rem 0;
    color: var(--text-dim); font-size: 0.85rem;
  }
  .frag-divider::before, .frag-divider::after {
    content: ' ──── '; color: var(--border);
  }
  .frag-dialogue-block { margin: 1rem 0; }
  .frag-dialogue-block.right { text-align: right; }
  .frag-dialogue-block.left { text-align: left; }
  .frag-character-label {
    font-size: 0.8rem; color: var(--blue); margin-bottom: 0.2rem;
  }
  .frag-dialogue-block.right .frag-character-label { color: var(--purple); }
  .frag-dialogue-bubble {
    display: inline-block; max-width: 80%; padding: 0.5rem 1rem;
    border-radius: 12px; background: var(--bg-input);
    border: 1px solid var(--border); font-size: 1rem;
  }
  .frag-dialogue-block.right .frag-dialogue-bubble { background: #1a2332; border-color: var(--purple); }
  .frag-inner-thought {
    display: inline-block; max-width: 80%; padding: 0.5rem 1rem;
    border-radius: 12px; border: 1.5px dashed var(--border);
    background: transparent; font-style: italic;
    color: var(--text-dim); font-size: 0.95rem;
  }
  .frag-action {
    font-size: 0.8rem; color: var(--text-dim);
    margin-top: 0.15rem;
  }
  .frag-action::before { content: '↳ '; }

  /* 打字机光标 */
  .frag-typing::after {
    content: ' ▌'; animation: blink 1s infinite;
  }
  @keyframes blink { 50% { opacity: 0; } }
</style>
</head>
<body>

<!-- 启动画面 -->
<div id="start-screen">
  <h1>📖 续写引擎</h1>
  <p class="subtitle">AI 自主续写 · 聊天小说阅读体验</p>
  <form id="start-form" onsubmit="startWriting(event)">
    <label>小说路径</label>
    <input id="novel-path" type="text" value="novels/poyun.txt"
           placeholder="如: novels/poyun.txt" required>
    <label>初始指令（可选）</label>
    <textarea id="initial-instruction" rows="2"
              placeholder="如: 让江停更主动一些"></textarea>
    <button type="submit">🚀 开始续写</button>
  </form>
</div>

<!-- 阅读画面 -->
<div id="reader-screen">
  <div id="top-bar">
    <span class="title" id="reader-title">📖 加载中...</span>
    <span class="status" id="status-label">就绪</span>
  </div>
  <div id="fragment-container"></div>
  <div id="bottom-bar">
    <input id="inject-input" type="text" placeholder="💬 输入指令调整续写方向..."
           onkeydown="if(event.key==='Enter') injectInstruction()">
    <button id="inject-btn" onclick="injectInstruction()">发送</button>
  </div>
</div>

<script>
let abortController = null;
let readerRef = null;
let fragmentCount = 0;

async function startWriting(event) {
  event.preventDefault();
  const novelPath = document.getElementById('novel-path').value.trim();
  const instruction = document.getElementById('initial-instruction').value.trim();

  if (!novelPath) return;

  // 切换到阅读视图
  document.getElementById('start-screen').style.display = 'none';
  document.getElementById('reader-screen').style.display = 'flex';
  document.getElementById('reader-title').textContent = '📖 ' + novelPath.split('/').pop();
  document.getElementById('status-label').textContent = '规划中...';
  document.getElementById('status-label').className = 'status active';
  document.getElementById('fragment-container').innerHTML = '';

  // 发起 SSE 请求
  abortController = new AbortController();
  fragmentCount = 0;

  try {
    const resp = await fetch('/api/write/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ novel_path: novelPath, instruction }),
      signal: abortController.signal,
    });

    readerRef = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await readerRef.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';  // 保留未完成的行

      let currentEvent = '';
      for (const line of lines) {
        if (line.startsWith('event: ')) {
          currentEvent = line.slice(7).trim();
        } else if (line.startsWith('data: ')) {
          const data = JSON.parse(line.slice(6));
          handleSSEEvent(currentEvent, data);
        }
      }
    }
  } catch (err) {
    if (err.name !== 'AbortError') {
      console.error('SSE error:', err);
      document.getElementById('status-label').textContent = '连接错误';
      document.getElementById('status-label').className = 'status';
    }
  }
}

function handleSSEEvent(eventType, data) {
  const statusEl = document.getElementById('status-label');

  switch (eventType) {
    case 'phase':
      const labels = { planning: '📋 规划中...', writing: '✍️ 写作中...',
                       reviewing: '🔍 审校中...', revising: '📝 修订中...' };
      statusEl.textContent = labels[data.phase] || data.phase;
      break;

    case 'outline':
      const title = data.title || ('第' + data.chapter_number + '章');
      document.getElementById('reader-title').textContent =
        '📖 ' + title + ' · 第' + data.chapter_number + '章';
      // 章节分隔
      appendFragment({ type: 'divider', text: '', divider_label: '第' + data.chapter_number + '章' });
      break;

    case 'fragment':
      appendFragment(data);
      fragmentCount++;
      break;

    case 'review':
      const issues = data.issues || [];
      statusEl.textContent = issues.length > 0
        ? `审校: ${issues.length} 个问题`
        : '✅ 审校通过';
      break;

    case 'complete':
      statusEl.textContent = '✅ 续写完成';
      statusEl.className = 'status';
      // 移除最后一个 fragment 的打字机动画
      const lastTyping = document.querySelector('.frag-typing');
      if (lastTyping) lastTyping.classList.remove('frag-typing');
      break;

    case 'done':
      statusEl.textContent = '✓ 就绪';
      statusEl.className = 'status';
      break;

    case 'error':
      statusEl.textContent = '❌ ' + (data.message || '错误');
      statusEl.className = 'status';
      break;
  }
}

function appendFragment(data) {
  const container = document.getElementById('fragment-container');

  // 移除之前 fragment 的打字机动画
  const prevTyping = container.querySelector('.frag-typing');
  if (prevTyping) prevTyping.classList.remove('frag-typing');

  let el;

  switch (data.type) {
    case 'narration':
      el = document.createElement('div');
      el.className = 'frag-narration frag-typing';
      el.textContent = data.text;
      break;

    case 'divider':
      el = document.createElement('div');
      el.className = 'frag-divider';
      el.textContent = data.divider_label || '';
      break;

    case 'dialogue':
    case 'inner_thought':
    case 'action':
      // 查找或创建当前角色的对话块
      el = document.createElement('div');
      el.className = 'frag-dialogue-block frag-typing';

      // 简单的左右交替：偶数序号右，奇数左
      const side = fragmentCount % 2 === 0 ? 'right' : 'left';
      el.classList.add(side);

      if (data.type === 'dialogue') {
        const label = document.createElement('div');
        label.className = 'frag-character-label';
        label.textContent = data.character || '';
        el.appendChild(label);

        const bubble = document.createElement('div');
        bubble.className = 'frag-dialogue-bubble';
        bubble.textContent = data.text;
        el.appendChild(bubble);
      } else if (data.type === 'action') {
        const action = document.createElement('div');
        action.className = 'frag-action';
        action.textContent = data.text;
        el.appendChild(action);
        el.querySelector('.frag-character-label') ||
          (() => {
            const label = document.createElement('div');
            label.className = 'frag-character-label';
            label.textContent = data.character || '';
            el.prepend(label);
          })();
      } else if (data.type === 'inner_thought') {
        const label = document.createElement('div');
        label.className = 'frag-character-label';
        label.textContent = data.character || '';
        el.appendChild(label);

        const thought = document.createElement('div');
        thought.className = 'frag-inner-thought';
        thought.textContent = data.text;
        el.appendChild(thought);
      }
      break;

    default:
      el = document.createElement('div');
      el.textContent = data.text || '';
  }

  container.appendChild(el);
  container.scrollTop = container.scrollHeight;
}

async function injectInstruction() {
  const input = document.getElementById('inject-input');
  const btn = document.getElementById('inject-btn');
  const instruction = input.value.trim();

  if (!instruction) return;

  btn.disabled = true;
  input.value = '';

  try {
    const resp = await fetch('/api/write/inject', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ instruction }),
    });
    const result = await resp.json();
    if (result.status !== 'ok') {
      console.error('Inject failed:', result.error);
    }
  } catch (err) {
    console.error('Inject error:', err);
  } finally {
    btn.disabled = false;
  }
}
</script>
</body>
</html>
```

- [ ] **Step 2: 提交**

```bash
git add frontend/index.html
git commit -m "feat: rewrite frontend for continuation mode with chat fiction UI

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 10: 端到端集成测试

**Files:**
- Create: `tests/test_continuation_pipeline.py`

- [ ] **Step 1: 编写集成测试**

```python
# tests/test_continuation_pipeline.py
"""续写系统集成测试 —— 使用 mock LLM 验证流水线逻辑。"""

import json
import asyncio
from unittest.mock import MagicMock, patch

import pytest

from src.continuation.fragment import StoryFragment, PipelineEvent
from src.continuation.pipeline import ContinuationPipeline
from src.context import GlobalContext, ServiceRegistry


@pytest.fixture
def mock_ctx_and_services():
    """构建 mock GlobalContext 和 ServiceRegistry。"""
    # Mock Novel with chapters
    mock_novel = MagicMock()
    mock_novel.title = "test_novel"
    mock_novel.chapters = [
        MagicMock(index=1, content="第一章内容。" * 100),
        MagicMock(index=2, content="第二章内容。" * 100),
    ]

    # Mock StoryGraph
    mock_graph = MagicMock()
    mock_graph.total_node_count = 10
    mock_graph.total_edge_count = 20
    mock_graph.person_nodes = []
    mock_graph.event_nodes = []
    mock_graph.event_relation_edges = []
    mock_graph.relationship_edges = []
    mock_novel.story_graph = mock_graph

    # Mock KG Service
    mock_kg = MagicMock()
    mock_kg.get_all_persons.return_value = []
    mock_kg.enemy_pairs.return_value = []

    # Mock Project Service
    mock_project = MagicMock()
    mock_project.create_project_dir.return_value = "/tmp/test_project"

    ctx = GlobalContext()
    ctx.novel = mock_novel

    services = ServiceRegistry(kg=mock_kg, project=mock_project)

    return ctx, services


class TestPipelineFlow:
    """测试流水线的事件流转。"""

    @pytest.mark.asyncio
    async def test_pipeline_emits_phase_events(self, mock_ctx_and_services):
        """验证流水线输出 phase 事件序列。"""
        ctx, services = mock_ctx_and_services
        mock_llm = MagicMock()

        # Mock KG extraction
        services.kg.extract_incremental = MagicMock(return_value=ctx.novel.story_graph)

        pipeline = ContinuationPipeline(ctx, services, mock_llm)

        # 跳过 load_novel（太复杂），直接手动设置内部状态
        pipeline._style_profile = MagicMock()
        pipeline._style_profile.summary.return_value = ""
        pipeline._style_profile.exemplars_text.return_value = ""
        pipeline._previous_chapter_ending = "test ending"
        pipeline._chapter = 2

        # Mock agents
        mock_architect = MagicMock()
        mock_architect.run = AsyncMock(return_value=json.dumps({
            "chapter_number": 3, "title": "测试", "synopsis": "测试大纲",
            "structure": {"opening": "", "rising": "", "climax": "", "hook": ""},
            "tone": "测试",
        }))
        mock_writer = MagicMock()
        mock_fragment = StoryFragment(type="narration", text="测试叙述。")
        mock_writer.stream = async_gen([mock_fragment])
        mock_reviewer = MagicMock()
        mock_reviewer.run = AsyncMock(return_value=json.dumps({
            "issues": [], "overall_score": 8.0,
        }))
        mock_editor = MagicMock()

        pipeline.architect = mock_architect
        pipeline.writer = mock_writer
        pipeline.reviewer = mock_reviewer
        pipeline.editor = mock_editor

        events = []
        async for event in pipeline.run("test instruction"):
            events.append(event)

        # 检查事件序列
        event_types = [e.event_type for e in events]
        assert "phase" in event_types
        assert "outline" in event_types
        assert "fragment" in event_types
        assert "review" in event_types
        assert "complete" in event_types
        assert "done" in event_types

    @pytest.mark.asyncio
    async def test_pipeline_inject_forwards_to_writer(self, mock_ctx_and_services):
        """验证 inject 转发到 Writer。"""
        ctx, services = mock_ctx_and_services
        mock_llm = MagicMock()

        pipeline = ContinuationPipeline(ctx, services, mock_llm)
        mock_writer = MagicMock()
        mock_writer.inject = AsyncMock()
        pipeline.writer = mock_writer
        pipeline._phase = "writing"

        await pipeline.inject("测试指令")

        mock_writer.inject.assert_called_once_with("测试指令")


class TestFragmentTypes:
    """验证所有 Fragment 类型正确渲染。"""

    def test_all_fragment_types(self):
        """确保 5 种 Fragment 类型都能正常序列化。"""
        fragments = [
            StoryFragment(type="narration", text="夜色如墨。"),
            StoryFragment(type="dialogue", text="你好。", character="江停"),
            StoryFragment(type="action", text="推开门", character="严峫"),
            StoryFragment(type="inner_thought", text="这件事不对。", character="江停"),
            StoryFragment(type="divider", text="", divider_label="三小时后"),
        ]

        for f in fragments:
            d = f.to_dict()
            assert "type" in d
            assert "text" in d
            sse = f.to_sse()
            assert "\n" not in sse  # SSE 单行约束
            restored = StoryFragment.from_json(sse)
            assert restored.type == f.type
            assert restored.text == f.text


# Helper: async generator from list
async def async_gen(items: list):
    for item in items:
        yield item


# Helper: async mock
class AsyncMock(MagicMock):
    async def __call__(self, *args, **kwargs):
        return super().__call__(*args, **kwargs)
```

- [ ] **Step 2: 运行测试**

Run: `python -m pytest tests/test_continuation_pipeline.py -v`
Expected: all tests pass

- [ ] **Step 3: 提交**

```bash
git add tests/test_continuation_pipeline.py
git commit -m "test: add integration tests for continuation pipeline

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 11: 清理 & 最终验证

- [ ] **Step 1: 移除旧的 `skills/continuation.md`**

```bash
git rm skills/continuation.md
```

- [ ] **Step 2: 运行完整测试套件验证回归**

Run: `python -m pytest tests/ -v`
Expected: 所有现有测试 + 新增测试全部通过

- [ ] **Step 3: 验证 roleplay 模式不受影响**

Run: `python main.py roleplay --help`
Expected: 显示 roleplay 命令的帮助信息（正常）

- [ ] **Step 4: 验证 write 命令可用**

Run: `python main.py write --help`
Expected: 显示 write 命令的帮助信息

- [ ] **Step 5: 提交最终清理**

```bash
git add -A
git commit -m "chore: remove old continuation skill, final cleanup

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```
