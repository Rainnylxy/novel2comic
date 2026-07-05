# -*- coding: utf-8 -*-
"""Tests for AuthorStyleDistiller."""

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

夜色更浓了。

严峫掐灭了烟头，走过去坐在沙发上。房间里只剩下墙上挂钟的滴答声。他知道江停需要时间思考，这个时候最好保持沉默。窗外开始下雨了，雨点打在玻璃上，发出细碎的声响。

江停终于转过身来，他的表情在忽明忽暗的光线中看不真切。"明天一早去现场。"他的声音依然很轻，却带着不容置疑的坚定。严峫点了点头，没有多问。多年的搭档让他们之间有一种默契，不需要太多言语。"""

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
