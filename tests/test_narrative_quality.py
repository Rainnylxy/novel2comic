# -*- coding: utf-8 -*-
"""叙事质量提升模块的端到端测试。"""

import pytest
from src.pipeline.narrative_card import ChapterNarrativeCard, BatchNarrativeSummary
from src.pipeline.foreshadowing_ledger import ForeshadowingLedger, ForeshadowingEntry
from src.pipeline.story_memory import StoryMemory


class TestNarrativeCard:
    def test_to_dict_and_from_dict(self):
        card = ChapterNarrativeCard(
            chapter_number=1,
            emotion_arc="压抑 → 账单暴露 → 愤怒释放",
            rhythm_type="高压",
            closing_hook_type="悬念钩子",
            highlight_type="反转",
            key_info_released="账单被伪造",
            character_functions={"江停": "对手", "严峫": "催化剂"},
        )
        d = card.to_dict()
        restored = ChapterNarrativeCard.from_dict(d)
        assert restored.chapter_number == 1
        assert restored.emotion_arc == "压抑 → 账单暴露 → 愤怒释放"
        assert restored.character_functions == {"江停": "对手", "严峫": "催化剂"}


class TestBatchNarrativeSummary:
    def test_to_dict_and_from_dict(self):
        summary = BatchNarrativeSummary(
            chapters_range=(1, 10),
            emotion_curve="前3章压抑 → 第4章小爆发",
            rhythm_pattern="高压 30% / 推进 40%",
            hook_preference={"章尾": "悬念式 60%, 事件式 30%"},
            highlight_density=3.3,
            dominant_highlight_types=["反转", "打脸"],
        )
        d = summary.to_dict()
        restored = BatchNarrativeSummary.from_dict(d)
        assert restored.chapters_range == (1, 10)
        assert restored.emotion_curve == "前3章压抑 → 第4章小爆发"
        assert restored.highlight_density == 3.3


class TestForeshadowingLedger:
    def test_full_lifecycle(self):
        ledger = ForeshadowingLedger()

        # 埋
        fid = ledger.add("内鬼身份", 162, 170, ["江停", "严峫"])
        assert fid == "F001"

        # 查
        pending = ledger.get_pending()
        assert len(pending) == 1
        assert pending[0].status == "buried"

        # 推
        ledger.advance(fid, 165, "江停发现线索")
        entry = ledger.get(fid)
        assert entry.status == "advanced"
        assert len(entry.advance_history) == 1

        # 收
        ledger.resolve(fid, 170)
        entry = ledger.get(fid)
        assert entry.status == "resolved"
        assert entry.actual_resolution_chapter == 170

        # 查（已收的不在 pending 里）
        pending = ledger.get_pending()
        assert len(pending) == 0

    def test_get_for_chapter(self):
        ledger = ForeshadowingLedger()
        ledger.add("伏笔A", 1, 5)
        ledger.add("伏笔B", 1, 10)
        ledger.add("伏笔C", 8, 0)

        # 第5章应该返回伏笔A
        ch5 = ledger.get_for_chapter(5)
        assert len(ch5) == 1
        assert ch5[0].id == "F001"

        # 第10章应该返回伏笔B
        ch10 = ledger.get_for_chapter(10)
        assert len(ch10) == 1
        assert ch10[0].id == "F002"

    def test_serialization(self):
        ledger = ForeshadowingLedger()
        ledger.add("测试伏笔", 1, 5)
        ledger.advance("F001", 3, "推进了")

        d = ledger.to_dict()
        restored = ForeshadowingLedger.from_dict(d)
        assert restored.get("F001").description == "测试伏笔"
        assert restored.get("F001").status == "advanced"
        assert restored._next_id == 2

    def test_get_stale(self):
        ledger = ForeshadowingLedger()
        ledger.add("老伏笔", 1, 0)
        # 模拟推进记录为空 + 埋了很久 → stale
        stale = ledger.get_stale(threshold=30)
        assert len(stale) == 1
        assert stale[0].id == "F001"

    def test_summarize(self):
        ledger = ForeshadowingLedger()
        ledger.add("伏笔1", 1, 5)
        ledger.add("伏笔2", 3, 8)
        summary = ledger.summarize()
        assert "F001" in summary
        assert "F002" in summary


class TestStoryMemoryNarrativeIntegration:
    def test_narrative_cards_storage(self):
        sm = StoryMemory()
        card = ChapterNarrativeCard(chapter_number=1, emotion_arc="测试")
        sm.narrative_cards[1] = card
        cards = sm.get_recent_narrative_cards(1)
        assert len(cards) == 1
        assert cards[0].emotion_arc == "测试"

    def test_narrative_context(self):
        sm = StoryMemory()
        sm.narrative_cards[1] = ChapterNarrativeCard(
            chapter_number=1,
            emotion_arc="压抑→触发→释放",
            rhythm_type="高压",
            closing_hook_type="悬念钩子",
        )
        ctx = sm.get_narrative_context(1)
        assert "压抑→触发→释放" in ctx
        assert "高压" in ctx

    def test_foreshadowing_ledger_integration(self):
        sm = StoryMemory()
        sm.foreshadowing_ledger.add("测试", 1, 5)
        pending = sm.foreshadowing_ledger.get_pending()
        assert len(pending) == 1

    def test_serialization_roundtrip(self):
        sm = StoryMemory()
        sm.narrative_cards[1] = ChapterNarrativeCard(
            chapter_number=1,
            emotion_arc="测试→测试→测试",
            rhythm_type="推进",
        )
        sm.foreshadowing_ledger.add("伏笔", 1, 5)
        sm.batch_summaries.append(BatchNarrativeSummary(
            chapters_range=(1, 10),
            emotion_curve="测试曲线",
        ))

        d = sm.to_dict()
        restored = StoryMemory.from_dict(d)

        assert len(restored.narrative_cards) == 1
        assert restored.narrative_cards[1].emotion_arc == "测试→测试→测试"
        assert len(restored.foreshadowing_ledger.get_pending()) == 1
        assert len(restored.batch_summaries) == 1
        assert restored.batch_summaries[0].emotion_curve == "测试曲线"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
