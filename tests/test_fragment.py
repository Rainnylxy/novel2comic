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
