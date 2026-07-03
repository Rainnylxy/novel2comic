# -*- coding: utf-8 -*-
"""场景引擎 —— 将对话锚定到具体章节。

职责:
- SceneContext: 当前场景的完整描述（章节/地点/在场人物/最近事件）
- SceneEngine: 从 KG + 章节文本中组装 SceneContext

用法:
    engine = SceneEngine()
    scene = engine.build_scene(graph, chapters, chapter_index=24, char_name="江停")
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .models import StoryGraph, ChapterInfo


@dataclass
class SceneContext:
    """当前场景的锚定信息。

    挂载在 RolePlayState.scene 上。
    """

    chapter_index: int = 0
    chapter_title: str = ""
    location: str = ""
    present_characters: list = field(default_factory=list)
    recent_events: list = field(default_factory=list)  # [{summary, chapter_start}]
    chapter_brief: str = ""
    total_chapters: int = 0

    def to_dict(self) -> dict:
        return {
            "chapter_index": self.chapter_index,
            "chapter_title": self.chapter_title,
            "location": self.location,
            "present_characters": self.present_characters,
            "recent_events": self.recent_events,
            "chapter_brief": self.chapter_brief,
            "total_chapters": self.total_chapters,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SceneContext":
        return cls(
            chapter_index=d.get("chapter_index", 0),
            chapter_title=d.get("chapter_title", ""),
            location=d.get("location", ""),
            present_characters=d.get("present_characters", []),
            recent_events=d.get("recent_events", []),
            chapter_brief=d.get("chapter_brief", ""),
            total_chapters=d.get("total_chapters", 0),
        )

    def format_for_prompt(self) -> str:
        """生成注入 user message 的场景前缀。"""
        if self.chapter_index == 0:
            return ""

        parts = [f"第{self.chapter_index}章"]
        if self.chapter_title:
            parts.append(f"「{self.chapter_title}」")

        if self.location:
            parts.append(f"📍 {self.location}")

        if self.present_characters:
            others = [c for c in self.present_characters[:6]]
            parts.append(f"👥 {', '.join(others)}")

        header = "[当前场景] " + " | ".join(parts)

        if self.recent_events:
            summaries = [e.get("summary", e.get("name", "")) for e in self.recent_events[:3]]
            events_line = "  ⚡ " + " / ".join(s[:60] for s in summaries if s)
            header += "\n" + events_line

        return header + "\n\n"

    def format_detail(self) -> str:
        """格式化完整场景详情（/scene 命令用）。"""
        lines = [
            f"第 {self.chapter_index}/{self.total_chapters} 章"
            + (f"「{self.chapter_title}」" if self.chapter_title else ""),
            f"地点: {self.location or '未知'}",
        ]
        if self.present_characters:
            lines.append(f"在场人物: {', '.join(self.present_characters)}")
        if self.chapter_brief:
            lines.append(f"章节简述: {self.chapter_brief}")
        if self.recent_events:
            lines.append("本章事件:")
            for e in self.recent_events[:10]:
                name = e.get("summary", e.get("name", ""))
                ch = e.get("chapter_start", "")
                lines.append(f"  - [{ch}] {name[:80]}")
        return "\n".join(lines)


class SceneEngine:
    """从 KG + 章节文本中组装场景上下文。

    纯函数风格，无状态。所有方法接收 graph + chapters 作为参数。
    """

    @staticmethod
    def build_scene(
        graph: "StoryGraph",
        chapters: list,
        chapter_index: int,
        char_name: str = "",
    ) -> SceneContext:
        """组装完整场景上下文。

        Args:
            graph: 知识图谱
            chapters: parse_novel_chapters() 返回的 ChapterInfo 列表
            chapter_index: 目标章节号 (1-based)
            char_name: 当前角色名（用于筛选该角色参与的事件）

        Returns:
            SceneContext
        """
        total = len(chapters)
        chapter_index = max(1, min(chapter_index, total))

        scene = SceneContext(
            chapter_index=chapter_index,
            total_chapters=total,
        )

        # 1. 章节标题 + 简述
        if 1 <= chapter_index <= total:
            ch_info = chapters[chapter_index - 1]
            scene.chapter_title = ch_info.title
            # 简述取文本前 100 字
            if ch_info.content:
                brief = ch_info.content.strip()[:120]
                # 去掉章节标题行
                if brief.startswith("第") and "章" in brief[:10]:
                    brief = brief.split("\n", 1)[-1].strip() if "\n" in brief else brief
                scene.chapter_brief = brief[:100]

        # 2. 在场人物（从 appears_in_edges 筛选）
        try:
            edges = graph.appears_in_edges if graph else []
            present = set()
            for edge in edges:
                if edge.chapter == chapter_index:
                    present.add(edge.person)
            # 排除当前角色自己
            present.discard(char_name)
            scene.present_characters = sorted(present)
        except Exception:
            pass

        # 3. 地点（从本章事件中取第一个有 location 的）
        try:
            events = graph.event_timeline() if graph else []
            for ev in events:
                if ev.chapter_start == chapter_index and ev.location:
                    scene.location = ev.location
                    break
            # 如果本章没有地点，向前回溯
            if not scene.location:
                for ev in reversed(events):
                    if ev.chapter_start < chapter_index and ev.location:
                        scene.location = ev.location
                        break
        except Exception:
            pass

        # 4. 最近事件（角色在本章参与的）
        try:
            if char_name and graph:
                char_events = graph.character_events(char_name)
                chapter_events = []
                for ev in char_events:
                    if ev.get("chapter_start") == chapter_index:
                        chapter_events.append(ev)
                scene.recent_events = chapter_events[:5]
        except Exception:
            pass

        # 5. 如果章节简述为空，尝试从 ChapterNode 取
        if not scene.chapter_brief:
            try:
                for cn in graph.chapter_nodes if graph else []:
                    if cn.index == chapter_index and cn.summary:
                        scene.chapter_brief = cn.summary[:100]
                        break
            except Exception:
                pass

        return scene

    @staticmethod
    def get_events_in_chapter(graph: "StoryGraph", chapter_index: int) -> list:
        """获取某一章的所有事件（不限于特定角色）。"""
        if not graph:
            return []
        events = graph.event_timeline()
        return [
            {"name": e.name, "summary": e.summary, "location": e.location,
             "chapter_start": e.chapter_start, "importance": e.importance}
            for e in events
            if e.chapter_start == chapter_index
        ]
