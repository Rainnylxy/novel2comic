# -*- coding: utf-8 -*-
"""互动小说引擎 —— 数据模型。

UserCharacter: 用户在故事中的化身
StoryState: 游戏存档（亲密度 + 旗标 + 决策日志）
PivotEvent: 可被用户选择改变的关键 KG 事件
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ============================================================
# 用户角色
# ============================================================

@dataclass
class UserCharacter:
    """用户在故事世界中的化身。"""

    name: str = ""
    backstory: str = ""
    identity: str = ""           # 警察 / 记者 / 路人 / 自定义
    traits: dict = field(default_factory=dict)  # {勇敢: 60, 谨慎: 40, ...}
    first_appearance_chapter: int = 0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "backstory": self.backstory,
            "identity": self.identity,
            "traits": self.traits,
            "first_appearance_chapter": self.first_appearance_chapter,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "UserCharacter":
        return cls(
            name=d.get("name", ""),
            backstory=d.get("backstory", ""),
            identity=d.get("identity", ""),
            traits=d.get("traits", {}),
            first_appearance_chapter=d.get("first_appearance_chapter", 0),
        )

    @property
    def summary(self) -> str:
        """一行摘要，供 prompt 使用。"""
        return f"{self.name} — {self.identity}" + (
            f" ({self.backstory[:60]}...)" if self.backstory else ""
        )


# ============================================================
# 故事状态（游戏存档）
# ============================================================

@dataclass
class StoryState:
    """互动小说的完整运行时状态。

    挂载在 DirectorAgent 上。每次抉择后更新。
    """

    user_character: UserCharacter = field(default_factory=UserCharacter)
    chapter: int = 0
    total_chapters: int = 0

    # 亲密度: {角色名: -100 ~ 100}
    intimacy: dict = field(default_factory=dict)

    # 剧情旗标: {"yangxie_saved": true, "traitor_exposed": false}
    plot_flags: dict = field(default_factory=dict)

    # 决策日志
    pivot_decisions: list = field(default_factory=list)
    regular_decisions: list = field(default_factory=list)

    # 节奏控制
    total_turns: int = 0
    last_choice_turn: int = 0

    # 偏离追踪
    story_diverged: bool = False
    active_ending: str = "neutral"

    def apply_choice(self, choice: dict, chosen_index: int):
        """应用一个普通抉择的后果。"""
        option = choice.get("choices", [{}])[chosen_index] if chosen_index < len(choice.get("choices", [])) else {}
        changes = option.get("intimacy_changes", {})
        for name, delta in changes.items():
            current = self.intimacy.get(name, 0)
            self.intimacy[name] = max(-100, min(100, current + delta))

        self.regular_decisions.append({
            "moment": choice.get("moment", ""),
            "chosen": option.get("text", ""),
            "changes": changes,
            "turn": self.total_turns,
        })
        self.last_choice_turn = self.total_turns

    def apply_pivot(self, pivot_result: dict, chosen_index: int):
        """应用一个 Pivot 抉择的后果。"""
        choices = pivot_result.get("choices", [])
        option = choices[chosen_index] if chosen_index < len(choices) else {}

        # 亲密度
        changes = option.get("intimacy_changes", {})
        for name, delta in changes.items():
            current = self.intimacy.get(name, 0)
            self.intimacy[name] = max(-100, min(100, current + delta))

        # 旗标
        flags = option.get("flag_changes", {})
        self.plot_flags.update(flags)

        # 结局倾向
        tendency = option.get("ending_tendency", "")
        if tendency:
            self.active_ending = tendency

        # 偏离标记
        if option.get("divergence") in ("moderate", "major"):
            self.story_diverged = True

        self.pivot_decisions.append({
            "pivot_name": pivot_result.get("pivot_name", ""),
            "chosen": option.get("text", ""),
            "outcome": option.get("event_outcome", ""),
            "changes": changes,
            "flags": flags,
            "turn": self.total_turns,
            "chapter": self.chapter,
        })
        self.last_choice_turn = self.total_turns

    def intimacy_summary(self) -> str:
        """格式化为 prompt 可用的亲密度摘要。"""
        if not self.intimacy:
            return "暂无亲密度数据"
        items = []
        for name, score in sorted(self.intimacy.items(), key=lambda x: -x[1]):
            label = self._intimacy_label(score)
            items.append(f"  {name}: {score:+d} ({label})")
        return "\n".join(items) if items else "暂无"

    @staticmethod
    def _intimacy_label(score: int) -> str:
        if score >= 80:
            return "深度信任"
        elif score >= 50:
            return "友好"
        elif score >= 20:
            return "中立"
        elif score >= -20:
            return "戒备"
        elif score >= -50:
            return "敌意"
        return "敌对"

    def npc_attitude(self, npc_name: str) -> str:
        """根据亲密度生成 NPC 对用户的态度描述。"""
        score = self.intimacy.get(npc_name, 0)
        if score >= 80:
            return f"你对 {self.user_character.name} 非常信任，愿意主动分享信息和感受。"
        elif score >= 50:
            return f"你和 {self.user_character.name} 关系不错，愿意正常交流。"
        elif score >= 20:
            return f"你还在观察 {self.user_character.name}，保持礼貌但有所保留。"
        elif score >= -20:
            return f"你对 {self.user_character.name} 有所戒备，话少，谨慎，不太愿意透露信息。"
        elif score >= -50:
            return f"你不信任 {self.user_character.name}，说话带刺，可能质疑对方的动机。"
        return f"你对 {self.user_character.name} 有强烈的敌意，可能拒绝交流或主动攻击。"

    def plot_flags_summary(self) -> str:
        """格式化为 prompt 可用的旗标摘要。"""
        if not self.plot_flags:
            return "暂无已触发的剧情旗标"
        return "\n".join(f"  {k}: {v}" for k, v in self.plot_flags.items())

    def to_dict(self) -> dict:
        return {
            "user_character": self.user_character.to_dict(),
            "chapter": self.chapter,
            "total_chapters": self.total_chapters,
            "intimacy": self.intimacy,
            "plot_flags": self.plot_flags,
            "pivot_decisions": self.pivot_decisions,
            "regular_decisions": self.regular_decisions,
            "total_turns": self.total_turns,
            "last_choice_turn": self.last_choice_turn,
            "story_diverged": self.story_diverged,
            "active_ending": self.active_ending,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "StoryState":
        uc_data = d.get("user_character", {})
        return cls(
            user_character=UserCharacter.from_dict(uc_data) if uc_data else UserCharacter(),
            chapter=d.get("chapter", 0),
            total_chapters=d.get("total_chapters", 0),
            intimacy=d.get("intimacy", {}),
            plot_flags=d.get("plot_flags", {}),
            pivot_decisions=d.get("pivot_decisions", []),
            regular_decisions=d.get("regular_decisions", []),
            total_turns=d.get("total_turns", 0),
            last_choice_turn=d.get("last_choice_turn", 0),
            story_diverged=d.get("story_diverged", False),
            active_ending=d.get("active_ending", "neutral"),
        )

    def save(self, filepath: str):
        import os
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, filepath: str) -> Optional["StoryState"]:
        import os
        if not os.path.exists(filepath):
            return None
        with open(filepath, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))


# ============================================================
# Pivot 事件
# ============================================================

@dataclass
class PivotEvent:
    """KG 中可被用户选择改变的关键事件。"""

    kg_event: dict = field(default_factory=dict)   # 原小说事件数据
    importance: int = 0
    triggered: bool = False
    resolved: bool = False
    actual_outcome: str = ""

    @property
    def chapter(self) -> int:
        return self.kg_event.get("chapter_start", 0)

    @property
    def name(self) -> str:
        return self.kg_event.get("name", "")

    @property
    def summary(self) -> str:
        return self.kg_event.get("summary", "")

    def to_dict(self) -> dict:
        return {
            "kg_event": self.kg_event,
            "importance": self.importance,
            "triggered": self.triggered,
            "resolved": self.resolved,
            "actual_outcome": self.actual_outcome,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PivotEvent":
        return cls(
            kg_event=d.get("kg_event", {}),
            importance=d.get("importance", 0),
            triggered=d.get("triggered", False),
            resolved=d.get("resolved", False),
            actual_outcome=d.get("actual_outcome", ""),
        )
