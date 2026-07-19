# -*- coding: utf-8 -*-
"""叙事分析卡 —— 单章 + 批级叙事特征数据结构。"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ChapterNarrativeCard:
    """单章叙事特征卡。

    由 NarrativeDistiller 分析产出，存储于 StoryMemory.narrative_cards。
    """

    chapter_number: int
    emotion_arc: str = ""               # "压抑 → 账单暴露 → 愤怒释放"
    rhythm_type: str = ""               # 高压/推进/关系/低压/信息整理
    closing_hook_type: str = ""         # 事件钩子/信息钩子/情绪钩子/悬念钩子/弱钩子/阶段目标
    highlight_type: str = ""            # 打脸/反转/身份揭露/装逼/感情拉扯/无
    key_info_released: str = ""         # 本章揭露了什么之前读者不知道的信息
    character_functions: dict = field(default_factory=dict)  # {"江停": "对手", "严峫": "催化剂"}

    def to_dict(self) -> dict:
        return {
            "chapter_number": self.chapter_number,
            "emotion_arc": self.emotion_arc,
            "rhythm_type": self.rhythm_type,
            "closing_hook_type": self.closing_hook_type,
            "highlight_type": self.highlight_type,
            "key_info_released": self.key_info_released,
            "character_functions": self.character_functions,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ChapterNarrativeCard":
        return cls(
            chapter_number=d.get("chapter_number", 0),
            emotion_arc=d.get("emotion_arc", ""),
            rhythm_type=d.get("rhythm_type", ""),
            closing_hook_type=d.get("closing_hook_type", ""),
            highlight_type=d.get("highlight_type", ""),
            key_info_released=d.get("key_info_released", ""),
            character_functions=d.get("character_functions", {}),
        )


@dataclass
class BatchNarrativeSummary:
    """批级（10章）叙事聚合卡。

    由 NarrativeDistiller.analyze_batch() 产出。
    """

    chapters_range: tuple = (0, 0)      # (start_ch, end_ch)
    emotion_curve: str = ""             # "前3章压抑 → 第4章小爆发 → ..."
    rhythm_pattern: str = ""            # "高压 40% / 推进 30% / 关系 20% / 低压 10%"
    hook_preference: dict = field(default_factory=dict)   # {"章尾": "悬念式 60%, 事件式 30%"}
    highlight_density: float = 0.0      # 每 N 章一个爽点
    dominant_highlight_types: list = field(default_factory=list)  # ["反转", "打脸"]

    def to_dict(self) -> dict:
        return {
            "chapters_range": list(self.chapters_range),
            "emotion_curve": self.emotion_curve,
            "rhythm_pattern": self.rhythm_pattern,
            "hook_preference": self.hook_preference,
            "highlight_density": self.highlight_density,
            "dominant_highlight_types": self.dominant_highlight_types,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BatchNarrativeSummary":
        r = d.get("chapters_range", [0, 0])
        return cls(
            chapters_range=(r[0], r[1]) if len(r) == 2 else (0, 0),
            emotion_curve=d.get("emotion_curve", ""),
            rhythm_pattern=d.get("rhythm_pattern", ""),
            hook_preference=d.get("hook_preference", {}),
            highlight_density=d.get("highlight_density", 0.0),
            dominant_highlight_types=d.get("dominant_highlight_types", []),
        )
