# -*- coding: utf-8 -*-
"""ForeshadowingLedger —— 伏笔生命周期管理账本。

追踪每条伏笔从埋设 → 推进 → 回收 → 废弃的完整生命周期。
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ForeshadowingEntry:
    """单条伏笔记录。"""

    id: str                                    # F001
    description: str                           # 伏笔内容（一句话）
    buried_chapter: int                        # 埋设章节
    planned_resolution_chapter: int = 0        # 计划回收章节
    actual_resolution_chapter: int = 0         # 实际回收章节（写后更新）
    status: str = "buried"                     # buried/advanced/resolved/abandoned
    related_characters: list = field(default_factory=list)
    advance_history: list = field(default_factory=list)  # [(chapter, detail), ...]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "description": self.description,
            "buried_chapter": self.buried_chapter,
            "planned_resolution_chapter": self.planned_resolution_chapter,
            "actual_resolution_chapter": self.actual_resolution_chapter,
            "status": self.status,
            "related_characters": self.related_characters,
            "advance_history": self.advance_history,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ForeshadowingEntry":
        return cls(
            id=d.get("id", ""),
            description=d.get("description", ""),
            buried_chapter=d.get("buried_chapter", 0),
            planned_resolution_chapter=d.get("planned_resolution_chapter", 0),
            actual_resolution_chapter=d.get("actual_resolution_chapter", 0),
            status=d.get("status", "buried"),
            related_characters=d.get("related_characters", []),
            advance_history=d.get("advance_history", []),
        )


class ForeshadowingLedger:
    """伏笔账本。

    用法:
        ledger = ForeshadowingLedger()
        fid = ledger.add("内鬼身份", 162, 170, ["江停", "严峫"])
        ledger.advance(fid, 165, "江停发现线索")
        ledger.resolve(fid, 170)
    """

    def __init__(self):
        self._entries: dict[str, ForeshadowingEntry] = {}
        self._next_id: int = 1

    # ── CRUD ──

    def add(self, description: str, buried_chapter: int,
            planned_chapter: int = 0,
            characters: list = None) -> str:
        """添加新伏笔，返回伏笔 ID。"""
        fid = f"F{self._next_id:03d}"
        self._next_id += 1
        self._entries[fid] = ForeshadowingEntry(
            id=fid,
            description=description,
            buried_chapter=buried_chapter,
            planned_resolution_chapter=planned_chapter,
            related_characters=characters or [],
        )
        return fid

    def advance(self, foreshadowing_id: str, chapter: int, detail: str = ""):
        """记录伏笔推进。"""
        entry = self._entries.get(foreshadowing_id)
        if entry:
            entry.advance_history.append((chapter, detail))
            entry.status = "advanced"

    def resolve(self, foreshadowing_id: str, chapter: int):
        """标记伏笔已回收。"""
        entry = self._entries.get(foreshadowing_id)
        if entry:
            entry.status = "resolved"
            entry.actual_resolution_chapter = chapter

    def abandon(self, foreshadowing_id: str, reason: str = ""):
        """标记伏笔废弃（如故事线改变）。"""
        entry = self._entries.get(foreshadowing_id)
        if entry:
            entry.status = "abandoned"

    # ── 查询 ──

    def get(self, foreshadowing_id: str) -> Optional[ForeshadowingEntry]:
        return self._entries.get(foreshadowing_id)

    def get_pending(self) -> list[ForeshadowingEntry]:
        """所有待回收的伏笔。"""
        return [e for e in self._entries.values()
                if e.status in ("buried", "advanced")]

    def get_for_chapter(self, chapter: int) -> list[ForeshadowingEntry]:
        """本章应该推进或回收的伏笔：
        - planned_resolution_chapter 在本章范围内的
        - status=buried/advanced 且埋设距本章 >=5 章（不该刚埋就收）
        """
        candidates = []
        for e in self._entries.values():
            if e.status not in ("buried", "advanced"):
                continue
            if e.planned_resolution_chapter == chapter:
                candidates.append(e)
            elif (chapter - e.buried_chapter) >= 5 and e.planned_resolution_chapter == 0:
                # 未设定计划回收章节但已埋了很久，提醒推进
                candidates.append(e)
        return candidates

    def get_stale(self, threshold: int = 30) -> list[ForeshadowingEntry]:
        """检测断线伏笔：已埋 >threshold 章仍无推进记录的。"""
        return [e for e in self._entries.values()
                if e.status == "buried"
                and len(e.advance_history) == 0]

    def summarize(self) -> str:
        """人类可读报表。"""
        lines = ["## 伏笔台账"]
        for status in ("buried", "advanced", "resolved", "abandoned"):
            entries = [e for e in self._entries.values() if e.status == status]
            if entries:
                lines.append(f"\n{status} ({len(entries)}):")
                for e in entries:
                    lines.append(f"  {e.id}: {e.description} "
                                 f"(第{e.buried_chapter}章埋"
                                 + (f"→计划第{e.planned_resolution_chapter}章收" if e.planned_resolution_chapter else "")
                                 + ")")
        return "\n".join(lines)

    # ── 序列化 ──

    def to_dict(self) -> dict:
        return {
            "entries": {k: v.to_dict() for k, v in self._entries.items()},
            "next_id": self._next_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ForeshadowingLedger":
        ledger = cls()
        ledger._entries = {
            k: ForeshadowingEntry.from_dict(v)
            for k, v in d.get("entries", {}).items()
        }
        ledger._next_id = d.get("next_id", 1)
        return ledger
