# -*- coding: utf-8 -*-
"""StoryMemory —— Pipeline 应用层记忆。

统一管理跨章节、跨 Agent 的故事状态。
与 AgentFlow 框架层记忆（Working/Episodic/Semantic）互补：
  - 框架层管"一个 Agent 怎么记"（滑动窗口、token 截断、事实提取）
  - 应用层管"故事怎么演进"（角色演化、伏笔追踪、章节压缩、跨 Agent 分发）

设计原则:
  1. 状态集中: 所有故事状态存在一处，避免散装字段
  2. 分层压缩: 最近 N 章保留详情，超出的压缩为摘要
  3. 按需分发: snapshot() 给 Architect，snapshot_light() 给 Writer
  4. 显式生命周期: post_chapter() 更新，compact() 压缩，scratchpad 隔离
"""

from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

from .foreshadowing_ledger import ForeshadowingLedger
from .narrative_card import ChapterNarrativeCard, BatchNarrativeSummary

if TYPE_CHECKING:
    from ..core.llm import UnifiedLLM


# ============================================================
# 压缩阈值
# ============================================================
MAX_DETAILED_CHAPTERS = 10     # 保留最近 N 章的详细规划
MAX_RECENT_EVENTS = 20         # 保留最近 N 个关键事件
MAX_CHARACTER_ARC_ENTRIES = 30  # 每个角色最多保留 N 条演化记录


# ============================================================
# StoryMemory
# ============================================================

@dataclass
class StoryMemory:
    """故事的应用层工作记忆。

    生命周期:
      - load_novel() 时创建
      - post_chapter() 每章写完更新
      - compact() 章节超限时压缩
      - snapshot() / snapshot_light() 注入给各 Agent
    """

    # ── 角色状态 ──
    # {name: {"status": "active|dead|missing|arrested",
    #         "location": "建宁市",
    #         "verified": True,         # 是否经过 LLM 验证
    #         "last_seen_ch": 112,      # 最后出场章节（原文）
    #         "ending": "..."}}         # 原文结局
    character_states: dict = field(default_factory=dict)

    # ── 角色演化轨迹 ──
    # {name: [{ch, state_change, key_moment, emotion_shift}]}
    character_arcs: dict = field(default_factory=dict)

    # ── 活跃冲突 ──
    # [{characters: ["A","B"], tension: "high", description: "...", introduced_ch: 50}]
    active_conflicts: list = field(default_factory=list)

    # ── 伏笔追踪 ──
    # [{thread: "内鬼身份", introduced_ch: 162, status: "pending|resolved",
    #   resolved_ch: None, description: "..."}]
    pending_threads: list = field(default_factory=list)

    # ── 章节规划历史 ──
    # 最近 MAX_DETAILED_CHAPTERS 章的详细规划摘要
    chapter_plans: list = field(default_factory=list)

    # ── 故事摘要（压缩后的旧章节） ──
    # 超过 MAX_DETAILED_CHAPTERS 的章压缩为此摘要
    story_summary: str = ""

    # ── 最近关键事件 ──
    # ["第162章: 严峫发现新线索 → ...", "第163章: 江停秘密调查 → ..."]
    recent_events: list = field(default_factory=list)

    # ── Scratchpad（跨章临时草稿，不入 Agent 对话历史） ──
    # Architect 中间产物、Writer 笔记等
    _scratchpad: dict = field(default_factory=dict)

    # ── 统计 ──
    total_chapters_written: int = 0
    total_fragments_written: int = 0
    last_compaction_ch: int = 0

    # ── 叙事分析卡 ──
    # {chapter_number: ChapterNarrativeCard}  单章叙事特征
    narrative_cards: dict = field(default_factory=dict)

    # 批级聚合，按时间顺序
    batch_summaries: list = field(default_factory=list)

    # ── 伏笔账本 ──
    foreshadowing_ledger: ForeshadowingLedger = field(default_factory=ForeshadowingLedger)

    # ================================================================
    # 序列化 / 反序列化
    # ================================================================

    def to_dict(self) -> dict:
        return {
            "character_states": self.character_states,
            "character_arcs": self.character_arcs,
            "active_conflicts": self.active_conflicts,
            "pending_threads": self.pending_threads,
            "chapter_plans": self.chapter_plans,
            "story_summary": self.story_summary,
            "recent_events": self.recent_events,
            "total_chapters_written": self.total_chapters_written,
            "total_fragments_written": self.total_fragments_written,
            "last_compaction_ch": self.last_compaction_ch,
            "narrative_cards": {str(k): v.to_dict() for k, v in self.narrative_cards.items()},
            "batch_summaries": [bs.to_dict() for bs in self.batch_summaries],
            "foreshadowing_ledger": self.foreshadowing_ledger.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "StoryMemory":
        return cls(
            character_states=d.get("character_states", {}),
            character_arcs=d.get("character_arcs", {}),
            active_conflicts=d.get("active_conflicts", []),
            pending_threads=d.get("pending_threads", []),
            chapter_plans=d.get("chapter_plans", []),
            story_summary=d.get("story_summary", ""),
            recent_events=d.get("recent_events", []),
            total_chapters_written=d.get("total_chapters_written", 0),
            total_fragments_written=d.get("total_fragments_written", 0),
            last_compaction_ch=d.get("last_compaction_ch", 0),
            narrative_cards={
                int(k): ChapterNarrativeCard.from_dict(v)
                for k, v in d.get("narrative_cards", {}).items()
            },
            batch_summaries=[
                BatchNarrativeSummary.from_dict(bs)
                for bs in d.get("batch_summaries", [])
            ],
            foreshadowing_ledger=ForeshadowingLedger.from_dict(
                d.get("foreshadowing_ledger", {})
            ),
        )

    # ================================================================
    # 角色
    # ================================================================

    def update_character(self, name: str, **kwargs):
        """更新角色状态（合并式）。"""
        entry = self.character_states.setdefault(name, {})
        entry.update(kwargs)

    def get_dead_or_missing(self) -> dict[str, str]:
        """返回非活跃角色 {name: status}，供 Agent 做硬约束。"""
        return {n: s["status"] for n, s in self.character_states.items()
                if s.get("status") in ("dead", "deceased", "killed", "missing")}

    def get_character_statuses(self) -> dict[str, str]:
        """返回所有角色状态 {name: status}。"""
        return {n: s.get("status", "active") for n, s in self.character_states.items()}

    def record_character_arc(self, name: str, ch: int, state_change: str = "",
                             key_moment: str = "", emotion_shift: str = ""):
        """记录角色演化轨迹。"""
        arc = self.character_arcs.setdefault(name, [])
        arc.append({
            "ch": ch, "state_change": state_change,
            "key_moment": key_moment, "emotion_shift": emotion_shift,
        })
        if len(arc) > MAX_CHARACTER_ARC_ENTRIES:
            self.character_arcs[name] = arc[-MAX_CHARACTER_ARC_ENTRIES:]

    # ================================================================
    # 冲突
    # ================================================================

    def update_conflicts(self, conflicts: list):
        """全量替换活跃冲突列表。"""
        self.active_conflicts = conflicts

    # ================================================================
    # 伏笔
    # ================================================================

    def add_thread(self, thread: str, introduced_ch: int, description: str = ""):
        """引入新伏笔。去重：同名伏笔不重复添加。"""
        for t in self.pending_threads:
            if t["thread"] == thread:
                return
        self.pending_threads.append({
            "thread": thread, "introduced_ch": introduced_ch,
            "status": "pending", "resolved_ch": None,
            "description": description,
        })

    def resolve_thread(self, thread: str, resolved_ch: int):
        """标记伏笔已回收。"""
        for t in self.pending_threads:
            if t["thread"] == thread:
                t["status"] = "resolved"
                t["resolved_ch"] = resolved_ch
                return

    def get_pending_threads(self) -> list[dict]:
        """获取所有未回收的伏笔。

        优先从 foreshadowing_ledger 读取（新数据源），
        兜底从旧的 pending_threads 列表读取（兼容旧序列化数据）。
        """
        entries = self.foreshadowing_ledger.get_pending()
        if entries:
            return [
                {
                    "thread": e.description,
                    "introduced_ch": e.buried_chapter,
                    "status": e.status,
                    "resolved_ch": e.actual_resolution_chapter or None,
                    "description": e.description,
                }
                for e in entries
            ]
        return [t for t in self.pending_threads if t.get("status") == "pending"]

    # ================================================================
    # 章节历史
    # ================================================================

    def add_chapter_plan(self, plan: dict):
        """添加一章的规划摘要。自动触发 compact() 检查。"""
        self.chapter_plans.append(plan)
        if self.total_chapters_written - self.last_compaction_ch > MAX_DETAILED_CHAPTERS:
            self._needs_compaction = True

    def get_recent_plans(self, n: int = 5) -> list[dict]:
        """获取最近 n 章规划摘要。"""
        return self.chapter_plans[-n:] if self.chapter_plans else []

    def get_plan_context(self) -> str:
        """生成前序章节规划回顾文本，供 Architect push context 使用。"""
        recent = self.get_recent_plans(5)
        if not recent:
            return ""

        lines = ["## 前序章节规划回顾（已完成的规划）"]
        for pcp in recent:
            ch = pcp.get("chapter_number", "?")
            title = pcp.get("title", "?")
            synopsis = pcp.get("synopsis", "")[:120]
            characters = pcp.get("characters_involved", [])
            key_events = pcp.get("key_events", [])
            plots = pcp.get("plot_threads_introduced", [])
            lines.append(f"- 第{ch}章「{title}」: {synopsis}")
            if characters:
                lines.append(f"  涉及角色: {', '.join(characters[:8])}")
            if key_events:
                lines.append(f"  关键事件: {'; '.join(key_events[:5])}")
            if plots:
                lines.append(f"  引入伏笔: {'; '.join(plots[:3])}")

        if self.story_summary:
            lines.append(f"\n## 前情摘要\n{self.story_summary[:800]}")

        lines.append(">> 请确保本章规划与上述前序章节保持连贯，避免重复或矛盾。")
        return "\n".join(lines)

    # ================================================================
    # 事件
    # ================================================================

    def add_event(self, event: str, ch: int):
        """记录关键事件。"""
        self.recent_events.append(f"第{ch}章: {event}")
        if len(self.recent_events) > MAX_RECENT_EVENTS:
            self.recent_events = self.recent_events[-MAX_RECENT_EVENTS:]

    # ================================================================
    # 压缩
    # ================================================================

    async def compact(self, llm: "UnifiedLLM") -> str:
        """将超过阈值的旧章节压缩为故事摘要。

        压缩策略:
          保留最近 MAX_DETAILED_CHAPTERS 章的详情，
          更早的章节调 LLM 压缩为一段故事摘要。

        Returns:
            压缩后的 story_summary
        """
        if len(self.chapter_plans) <= MAX_DETAILED_CHAPTERS:
            return self.story_summary

        # 被压缩的章 = 总数 - MAX_DETAILED_CHAPTERS
        cutoff = len(self.chapter_plans) - MAX_DETAILED_CHAPTERS
        to_compress = self.chapter_plans[:cutoff]

        if not to_compress:
            return self.story_summary

        # 构建压缩 prompt
        old_summary = self.story_summary or "（新开始的故事）"
        chapters_text = "\n".join(
            f"第{cp.get('chapter_number','?')}章「{cp.get('title','?')}」: {cp.get('synopsis','')}"
            for cp in to_compress
        )

        prompt = (
            "你是专业小说编辑。请将以下已写章节合并压缩为一段流畅的故事摘要（200-500字）。"
            "保留关键剧情转折、角色变化和重要伏笔。\n\n"
            f"【已有摘要】\n{old_summary}\n\n"
            f"【新增章节】\n{chapters_text}\n\n"
            "请输出合并后的完整故事摘要："
        )

        try:
            result = llm.chat(
                system_prompt="你是专业小说编辑，擅长提炼故事主线。只输出摘要，不要评价。",
                user_prompt=prompt,
                temperature=0.3,
                max_tokens=1024,
            )
            self.story_summary = result.strip()
        except Exception:
            # LLM 压缩失败 → 退化为简单拼接
            self.story_summary = old_summary + "\n" + chapters_text[:500]

        # 清理已压缩的章节详情
        self.chapter_plans = self.chapter_plans[cutoff:]
        self.last_compaction_ch = self.total_chapters_written

        return self.story_summary

    # ================================================================
    # 跨章更新（Pipeline 在每章写完后调用）
    # ================================================================

    def post_chapter(self, chapter_plan: dict, fragments: list,
                     ch_num: int):
        """每章写完后更新记忆。

        Pipeline 在 writing + review 完成后调用。

        Args:
            chapter_plan: 本章规划 dict
            fragments: 本章所有 StoryFragment 列表
            ch_num: 本章章节号
        """
        self.total_chapters_written += 1
        self.total_fragments_written += len(fragments)

        # 1. 记录章节摘要
        plan_summary = {
            "chapter_number": ch_num,
            "title": chapter_plan.get("title", ""),
            "synopsis": chapter_plan.get("synopsis", ""),
            "characters_involved": list(chapter_plan.get("character_beats", {}).keys())
                if isinstance(chapter_plan.get("character_beats"), dict) else [],
            "key_events": [s.get("goal", "") for s in chapter_plan.get("sections", [])],
            "plot_threads_introduced": chapter_plan.get("plot_threads_introduced", []),
        }
        self.add_chapter_plan(plan_summary)

        # 2. 记录新伏笔
        for thread in chapter_plan.get("plot_threads_introduced", []):
            self.add_thread(thread, ch_num)

        # 3. 记录角色节拍（演化轨迹）
        beats = chapter_plan.get("character_beats", {})
        if isinstance(beats, dict):
            for name, beat in beats.items():
                arc = beat.get("arc", "") if isinstance(beat, dict) else str(beat)
                key = beat.get("key_action", "") if isinstance(beat, dict) else ""
                emotion = beat.get("emotional_beat", "") if isinstance(beat, dict) else ""
                self.record_character_arc(name, ch_num, state_change=arc,
                                          key_moment=key, emotion_shift=emotion)

        # 4. 提取本章关键事件
        self.add_event(chapter_plan.get("synopsis", "")[:100], ch_num)

        # 5. 清理 scratchpad（每章结束重置临时草稿）
        self._scratchpad.clear()

    # ================================================================
    # Scratchpad
    # ================================================================

    def scratchpad_set(self, key: str, value):
        """写入临时草稿（Architect 内部笔记，不入 Agent history）。"""
        self._scratchpad[key] = value

    def scratchpad_get(self, key: str, default=None):
        """读取临时草稿。"""
        return self._scratchpad.get(key, default)

    def scratchpad_clear(self):
        """清空临时草稿。"""
        self._scratchpad.clear()

    # ================================================================
    # 快照（按需分发）
    # ================================================================

    def snapshot_character_statuses(self) -> dict[str, str]:
        """给 Writer/ReviewEditor 用的角色状态快照。"""
        return self.get_dead_or_missing()

    # ================================================================
    # 叙事分析卡
    # ================================================================

    def get_recent_narrative_cards(self, n: int = 5) -> list:
        """获取最近 n 章的叙事分析卡，按章节号降序。"""
        sorted_cards = sorted(
            self.narrative_cards.values(),
            key=lambda c: c.chapter_number,
            reverse=True,
        )
        return sorted_cards[:n]

    def get_latest_batch_summary(self) -> Optional["BatchNarrativeSummary"]:
        """获取最新的批级聚合卡。"""
        return self.batch_summaries[-1] if self.batch_summaries else None

    def get_narrative_context(self, n: int = 5) -> str:
        """生成最近 n 章叙事上下文文本，供 Agent prompt 注入。"""
        cards = self.get_recent_narrative_cards(n)
        if not cards:
            return "（暂无叙事分析数据）"

        lines = ["## 最近章节叙事特征"]
        for card in sorted(cards, key=lambda c: c.chapter_number):
            lines.append(
                f"第{card.chapter_number}章: "
                f"情绪={card.emotion_arc or '?'}, "
                f"节奏={card.rhythm_type or '?'}, "
                f"钩子={card.closing_hook_type or '?'}"
                + (f", 爽点={card.highlight_type}" if card.highlight_type and card.highlight_type != "无" else "")
            )
        return "\n".join(lines)
