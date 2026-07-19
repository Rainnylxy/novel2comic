import json
import pytest
from src.pipeline.fragment import StoryFragment, PipelineEvent
from src.pipeline.fragmentizer import Fragmentizer


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

    def test_pipeline_event_sse_format(self):
        evt = PipelineEvent("phase", {"phase": "writing"})
        sse = evt.to_sse()
        assert sse.startswith("event: phase\n")
        assert "data:" in sse
        assert sse.endswith("\n\n")


class TestFragmentizer:
    def test_narration(self):
        frags = Fragmentizer().process("夜色如墨。")
        assert len(frags) == 1
        assert frags[0].type == "narration"
        assert frags[0].text == "夜色如墨。"

    def test_dialogue(self):
        frags = Fragmentizer().process('严峫道：「说。」')
        assert len(frags) == 1
        assert frags[0].type == "dialogue"
        assert "说" in frags[0].text
        assert frags[0].character == "严峫"

    def test_inner_thought(self):
        frags = Fragmentizer().process("江停心想这个案子不对。")
        assert len(frags) == 1
        assert frags[0].type == "inner_thought"
        assert frags[0].character == "江停"
        assert "这个案子不对" in frags[0].text

    def test_action(self):
        frags = Fragmentizer().process("严峫撑起半边身子，摸索着按下接听键。")
        assert len(frags) == 1
        assert frags[0].type in ("action", "narration")  # 可能被归类为 narration

    def test_divider(self):
        frags = Fragmentizer().process("三小时后。建宁市公安局。")
        found_divider = any(f.type == "divider" for f in frags)
        assert found_divider

    def test_multi_paragraph(self):
        prose = """清晨六点十七分，手机在床头柜上震动起来。

严峫撑起半边身子。

「说。」"""
        frags = Fragmentizer().process(prose)
        assert len(frags) >= 2

    def test_empty_input(self):
        assert Fragmentizer().process("") == []
        assert Fragmentizer().process("   ") == []
