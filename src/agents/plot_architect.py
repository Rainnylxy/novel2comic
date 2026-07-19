# -*- coding: utf-8 -*-
"""PlotArchitect —— 剧情架构师 Agent。

继承 BaseAgent，通过 AgentFlow ReAct 循环做两级规划:
  1. 篇章路线图（Roadmap）: 10-20 章高层规划
  2. 章节规划（Chapter Plan）: 单章详细规划 + 角色节拍

工具（纯代码 KG 查询，无嵌套 LLM 调用）:
  - gather_hanging_threads()  — 未解决伏笔
  - gather_active_conflicts() — 活跃冲突
  - lookup_character(name)    — 角色详细档案
  - get_event_timeline()      — 事件时间线

Push 上下文（动态前缀，每次 run() 时注入）:
  - 角色状态（dead/missing）
  - 风格核心标签
  - 原文结尾
  - 当前路线图 + 里程碑
"""

import json
import logging
from typing import TYPE_CHECKING, Optional

from agentflow.runtime.toolkit import tool

from .base_agent import BaseAgent

logger = logging.getLogger(__name__)
if not logger.handlers:
    _fh = logging.FileHandler("plot_architect.log", encoding="utf-8")
    _fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    ))
    logger.addHandler(_fh)
    _sh = logging.StreamHandler()
    _sh.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(_sh)
    logger.setLevel(logging.INFO)

if TYPE_CHECKING:
    from ..distillers.style_profile import AuthorStyleProfile
    from ..pipeline.state import PipelineState


# ============================================================
# 兜底方案（供 Pipeline 使用）
# ============================================================

def make_fallback_chapter(chapter_number: int) -> dict:
    """构建单章兜底大纲。"""
    return {
        "type": "chapter",
        "chapter_number": chapter_number,
        "title": "续",
        "synopsis": "继续推进故事",
        "emotion_arc": "延续上一章情绪 → 事件推进 → 埋下悬念",
        "rhythm_position": "推进",
        "closing_hook_type": "悬念钩子",
        "forbidden_releases": [],
        "milestone_source": 0,
        "character_beats": {},
        "foreshadowing_advanced": [],
        "foreshadowing_resolved": [],
        "sections": [
            {"name": "opening", "goal": "衔接上一章结尾", "characters": [],
             "key_beats": ["承接上一章结尾 [铺垫·中150]"], "target_fragments": 5},
            {"name": "rising", "goal": "推进冲突", "characters": [],
             "key_beats": ["推进核心冲突 [推进·密250]"], "target_fragments": 6},
            {"name": "climax", "goal": "关键转折", "characters": [],
             "key_beats": ["情节点高潮 [高潮·密250]"], "target_fragments": 5},
            {"name": "hook", "goal": "章尾悬念", "characters": [],
             "key_beats": ["章尾悬念 [悬念·密150]"], "target_fragments": 3},
        ],
        "plot_threads_introduced": [],
    }


def make_fallback_roadmap(chapter: int) -> dict:
    """构建兜底篇章路线图。"""
    return {
        "type": "roadmap",
        "roadmap_title": f"第{chapter}章起",
        "roadmap_synopsis": "继续推进故事",
        "total_chapters": 1,
        "milestones": [{
            "index": 1,
            "milestone_title": f"第{chapter}章",
            "synopsis": "继续推进故事",
            "key_conflicts": [],
            "characters_involved": [],
            "thematic_focus": "延续",
            "expected_tone": "保持原作风格",
        }],
        "climax_milestone": 1,
        "final_boss_hints": "",
        "major_themes": [],
        "plot_threads_introduced": [],
        "status": "fallback",
    }


# ============================================================
# PlotArchitect
# ============================================================

class PlotArchitect(BaseAgent):
    """剧情架构师 Agent。

    继承 BaseAgent，通过 ReAct 循环做两级规划。
    System prompt 来自 skills/plot_architect.md（缓存）。
    动态数据通过 _build_dynamic_prefix() 注入 task。

    用法:
        architect = PlotArchitect(ctx, services, llm)
        architect.set_context(
            previous_chapter_ending=...,
            character_profiles=...,
            last_chapter=...,
            style_profile=...,
            user_instruction=...,
            character_statuses=...,
            roadmap=...,
            current_milestone=...,
        )
        result = await architect.run("规划新篇章")
        # 或
        result = await architect.run("制作第51章规划")
    """

    SKILL_NAME = "plot_architect"

    def _get_system_prompt(self) -> str:
        """热加载：skill body 直接注入 system prompt，不走 use_skill_xxx。"""
        return self._load_skill_body()

    def __init__(self, agent_llm, kg, state: "PipelineState"):
        super().__init__(agent_llm, kg, state)
        self._kg = kg

        # 运行时上下文（由 set_context 注入，每次 run 前更新）
        self._previous_chapter_ending: str = ""
        self._last_chapter: int = 0
        self._user_instruction: str = ""
        self._roadmap_store: Optional[dict] = None

        # 角色验证状态（共享可变引用）
        self._status_verified: set = state.status_verified
        self._status_fixes: dict = state.status_fixes
        self._novel_text: str = state.novel_text

        # 前序章节规划摘要
        self._previous_chapter_plans: list[dict] = []

    # ================================================================
    # 上下文设置
    # ================================================================

    def set_context(
        self,
        previous_chapter_ending: str = "",
        last_chapter: int = 0,
        user_instruction: str = "",
        roadmap_store: Optional[dict] = None,
        previous_chapter_plans: list = None,
    ):
        """设置每章变化的运行时上下文。

        Args:
            previous_chapter_ending: 上一章结尾文本（衔接）
            last_chapter: 最后章节号
            user_instruction: 用户初始指令（仅首章生效）
            roadmap_store: 可变 dict {data, chapter_index, next_chapter, dirty}
            previous_chapter_plans: 前序章节规划摘要
        """
        self._previous_chapter_ending = previous_chapter_ending
        self._last_chapter = last_chapter
        self._user_instruction = user_instruction
        self._roadmap_store = roadmap_store or {}
        self._previous_chapter_plans = previous_chapter_plans or []
        # 从 PipelineState 同步每章会变的状态
        self._character_statuses = self._state.character_statuses
        self._style_profile = self._state.style_profile
        self._character_profiles = self._state.character_profiles

    # ================================================================
    # 思考模式
    # ================================================================

    def _get_thinking_mode(self):
        """Plan-Execute: 第一阶段规划（如创建路线图），第二阶段按计划执行（展开章节规划）。"""
        from agentflow.runtime.thinking import ThinkingMode
        return ThinkingMode.PLAN_EXECUTE

    # ================================================================
    # 记忆配置
    # ================================================================

    def _get_memory_profile(self):
        """PlotArchitect 需要更大工作记忆来跨章节共享上下文。"""
        from agentflow.runtime.memory.manager import MemoryProfile, WorkingConfig
        return MemoryProfile(
            working=WorkingConfig(max_turns=60, max_tokens=24000),
            episodic_max=500,
            semantic_enabled=False,
        )

    # ================================================================
    # 动态前缀（Push 上下文）
    # ================================================================

    def _build_dynamic_prefix(self) -> str:
        """构建每章变化的动态上下文。

        稳定内容（文风、角色死活、Voice）已走 _get_references() → Reference 卡。
        这里只放每次 run() 都变的内容：路线图进度、前序章节、结尾文本。
        """
        parts = []

        # ── 路线图上下文 ──
        store = self._roadmap_store or {}
        roadmap = store.get("data", {})
        ch_idx = store.get("chapter_index", 0)
        milestones = roadmap.get("milestones", []) if roadmap else []

        if milestones:
            parts.append("## 当前篇章规划")
            parts.append(f"篇章: {roadmap.get('roadmap_title', '?')}")
            parts.append(f"总章数: {roadmap.get('total_chapters', '?')}")
            parts.append(f"梗概: {roadmap.get('roadmap_synopsis', '')[:200]}")
            if roadmap.get("major_themes"):
                parts.append(f"核心主题: {', '.join(roadmap['major_themes'])}")

            if ch_idx < len(milestones):
                ms = milestones[ch_idx]
                parts.append(f"当前里程碑 ({ch_idx + 1}/{len(milestones)}): {ms.get('milestone_title', '?')}")
                parts.append(f"  梗概: {ms.get('synopsis', '')}")
                if ms.get("key_conflicts"):
                    parts.append(f"  核心冲突: {', '.join(ms['key_conflicts'])}")
                if ms.get("characters_involved"):
                    parts.append(f"  涉及角色: {', '.join(ms['characters_involved'])}")
            else:
                parts.append(f"⚠️ 路线图已用尽（{len(milestones)} 个里程碑全部完成），请用 update_roadmap 工具创建新路线图。")
        else:
            parts.append("## 当前篇章规划\n暂无篇章路线图。请用 update_roadmap 工具创建新的 10-20 章路线图。")

        # ── 前序章节规划摘要（从 StoryMemory 读取） ──
        if self._state.story_memory:
            # 故事摘要（压缩后的旧章节）
            story_summary = getattr(self._state.story_memory, 'story_summary', '')
            if story_summary:
                parts.append("## 前情摘要\n" + story_summary[:800])
            # 最近章节规划
            plan_context = self._state.story_memory.get_plan_context()
            if plan_context:
                parts.append(plan_context)
        elif self._previous_chapter_plans:
            # Fallback: 兼容旧式列表传入
            parts.append("## 前序章节规划回顾（已完成的规划）")
            for pcp in self._previous_chapter_plans[-5:]:
                ch = pcp.get("chapter_number", "?")
                title = pcp.get("title", "?")
                synopsis = pcp.get("synopsis", "")[:120]
                characters = pcp.get("characters_involved", [])
                key_events = pcp.get("key_events", [])
                plots = pcp.get("plot_threads_introduced", [])
                parts.append(f"- 第{ch}章「{title}」: {synopsis}")
                if characters:
                    parts.append(f"  涉及角色: {', '.join(characters[:8])}")
                if key_events:
                    parts.append(f"  关键事件: {'; '.join(key_events[:5])}")
                if plots:
                    parts.append(f"  引入伏笔: {'; '.join(plots[:3])}")
            parts.append(">> 请确保本章规划与上述前序章节保持连贯，避免重复或矛盾。")

        # ── 原文结尾 ──
        parts.append("## 原文结尾（叙事衔接点）")
        ending = self._previous_chapter_ending
        parts.append(ending[-500:] if len(ending) > 500 else ending)

        return "\n".join(parts)

    # ================================================================
    # run 入口
    # ================================================================

    async def run(self, task: str = ""):
        """运行 ReAct 循环。

        Stable context → self.set_reference()（永不裁剪）
        Dynamic context → _build_dynamic_prefix()（拼接到 task）
        """
        prefix = self._build_dynamic_prefix()
        full_task = (prefix + "\n\n" + task) if prefix else task

        logger.info("PlotArchitect task: %.200s", task)
        return await super().run(full_task)

    # ================================================================
    # 角色验证辅助方法
    # ================================================================

    @staticmethod
    def _split_novel_by_chapter(text: str) -> dict:
        """按章节切分小说。Returns {chapter_number: chapter_text}."""
        import re
        pattern = re.compile(r'(第[零一二三四五六七八九十百千\d]+章[^\n]*)')
        parts = pattern.split(text)
        chapters = {}
        current_ch = 0
        current_text = []
        for part in parts:
            m = pattern.match(part)
            if m:
                if current_ch > 0 and current_text:
                    chapters[current_ch] = "".join(current_text)
                current_ch = PlotArchitect._parse_chapter_number(m.group(1))
                current_text = [part]
            else:
                current_text.append(part)
        if current_ch > 0 and current_text:
            chapters[current_ch] = "".join(current_text)
        return chapters

    @staticmethod
    def _find_chapters_by_name(name: str, chapters: dict) -> list:
        """规则定位：角色在哪些章节出场（纯字符串匹配）。"""
        appeared = []
        for ch_num in sorted(chapters.keys()):
            if name in chapters[ch_num]:
                appeared.append(ch_num)
        return appeared

    @staticmethod
    def _extract_name_context(name: str, text: str, window: int = 300) -> str:
        """提取角色名周围上下文段落。取最后 5 处出现。"""
        contexts = []
        idx = 0
        while True:
            idx = text.find(name, idx)
            if idx == -1:
                break
            start = max(0, idx - window)
            end = min(len(text), idx + window)
            ctx = text[start:end].strip()
            if len(ctx) >= 20:
                contexts.append(ctx)
            idx += len(name)
        return "\n---\n".join(contexts[-5:]) if contexts else text[-2000:]

    @staticmethod
    def _parse_chapter_number(title: str) -> int:
        """从 '第X章' 中解析章节号。"""
        import re
        m = re.search(r'第\s*(\d+)\s*章', title)
        if m:
            return int(m.group(1))
        cn = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
              "十": 10, "百": 100, "千": 1000}
        m = re.search(r'第([零一二三四五六七八九十百千]+)章', title)
        if m:
            s = m.group(1)
            result = 0
            unit = 1
            for ch in reversed(s):
                if ch in ("十", "百", "千"):
                    unit = cn[ch]
                else:
                    result += cn.get(ch, 0) * unit
            return result if result > 0 else unit
        return 0

    def _resolve_character_dossier(self, name: str, context: str,
                                   last_chapter: int) -> dict:
        """LLM 分析角色完整档案：状态 + 结局 + 伏笔。"""
        if not context or len(context) < 20:
            return {}

        prompt = (
            f"你是专业小说分析员。根据角色最后几次出场的原文片段，全面分析该角色的当前状态和结局。\n\n"
            f"角色: {name}\n"
            f"最后出场章节: 第{last_chapter}章\n\n"
            f"原文场景:\n{context[:3000]}\n\n"
            f"请返回 JSON:\n"
            f'{{"status": "dead|active|missing|arrested",'
            f'"ending": "该角色在原文中的结局——如何退场的？最后在做什么？一句话概括。",'
            f'"foreshadowing": "该角色身上还有哪些未解决的伏笔或线索。没有则填 无。",'
            f'"key_relationships": "该角色与其他角色的关键关系——对谁重要？谁在意他的生死？",'
            f'"evidence": "证明以上判断的原文关键句引用"}}\n\n'
            f"只返回 JSON。"
        )

        try:
            result = self._state.sync_llm.chat_json(
                system_prompt="你是专业小说分析员。只返回 JSON，不返回其他内容。",
                user_prompt=prompt,
                temperature=0.3,
                max_tokens=1024,
            )
            if isinstance(result, dict) and result.get("status"):
                return result
        except Exception:
            pass
        return {}

    # ================================================================
    # 共享工具工厂
    # ================================================================

    def _make_shared_toolkit(self):
        """创建 SharedToolKit 实例（每次 _build_tools 调用时刷新）。"""
        from ..tools import SharedToolKit
        return SharedToolKit(
            graph=self._state.graph,
            kg=self._kg,
            character_profiles=self._state.character_profiles,
            character_statuses=self._character_statuses,
            story_memory=self._state.story_memory,
        )

    # ================================================================
    # 工具（Pull —— 共享工具 + Agent 专有工具）
    # ================================================================

    def _build_tools(self) -> list:
        self_ref = self
        kg = self._kg

        # 共享工具（来自 SharedToolKit）
        shared = self._make_shared_toolkit()
        lookup_character = shared.make_lookup_character()
        gather_active_conflicts = shared.make_gather_active_conflicts()

        @tool
        def lookup_roadmap() -> str:
            """查看当前篇章路线图的状态和进度。

            返回当前路线图的标题、总章数、已完成/剩余里程碑数、
            当前里程碑详情。如果路线图已用尽或不存在，会明确提示需要更新。

            Returns:
                格式化的路线图状态文本
            """
            store = self_ref._roadmap_store or {}
            roadmap = store.get("data", {})
            ch_idx = store.get("chapter_index", 0)
            milestones = roadmap.get("milestones", [])

            if not milestones:
                return (
                    "暂无篇章路线图。你需要使用 update_roadmap 工具创建新的 10-20 章路线图。"
                    f"当前需要规划的是第 {store.get('next_chapter', '?')} 章。"
                )

            total = len(milestones)
            remaining = total - ch_idx

            lines = [
                f"路线图: 《{roadmap.get('roadmap_title', '?')}》",
                f"总章数: {roadmap.get('total_chapters', '?')}",
                f"梗概: {roadmap.get('roadmap_synopsis', '')[:200]}",
                f"进度: 已完成 {ch_idx}/{total} 个里程碑，剩余 {remaining} 个",
            ]

            if roadmap.get("major_themes"):
                lines.append(f"核心主题: {', '.join(roadmap['major_themes'])}")
            if roadmap.get("climax_milestone"):
                lines.append(f"高潮在里程碑 #{roadmap['climax_milestone']}")
            if roadmap.get("final_boss_hints"):
                lines.append(f"最终Boss/冲突: {roadmap['final_boss_hints']}")

            if ch_idx >= total:
                lines.append("")
                lines.append("⚠️ 路线图已全部完成！请使用 update_roadmap 工具设计新的 10-20 章路线图。")
            else:
                ms = milestones[ch_idx]
                lines.append(f"\n当前里程碑 ({ch_idx + 1}/{total}): {ms.get('milestone_title', '?')}")
                lines.append(f"  梗概: {ms.get('synopsis', '')}")
                if ms.get("key_conflicts"):
                    lines.append(f"  核心冲突: {', '.join(ms['key_conflicts'])}")
                if ms.get("characters_involved"):
                    lines.append(f"  涉及角色: {', '.join(ms['characters_involved'])}")
                if ms.get("expected_tone"):
                    lines.append(f"  预期基调: {ms['expected_tone']}")
                if ms.get("thematic_focus"):
                    lines.append(f"  主题焦点: {ms['thematic_focus']}")

                # 显示后续里程碑概览
                if remaining > 1:
                    lines.append(f"\n后续里程碑预览:")
                    for i in range(ch_idx + 1, min(ch_idx + 4, total)):
                        future = milestones[i]
                        lines.append(f"  #{i + 1} {future.get('milestone_title', '?')}: "
                                     f"{future.get('synopsis', '')[:80]}")

            return "\n".join(lines)

        @tool
        def update_roadmap(roadmap_json: str) -> str:
            """创建或更新篇章路线图。用于设计 10-20 章的高层故事弧线。

            当你发现路线图已用尽、用户指令改变了故事走向、或现有路线图不再适用时，
            调用此工具来创建新的路线图。

            Args:
                roadmap_json: 路线图 JSON 字符串，格式为:
                  {
                    "type": "roadmap",
                    "roadmap_title": "弧线名",
                    "roadmap_synopsis": "整体走向",
                    "total_chapters": 15,
                    "stage_definition": "开篇期/发展期/转折期/高潮期/收尾期",
                    "emotion_curve": "压抑 → 加压 → 爽感释放 → 冷却",
                    "forbidden_releases": ["不能提前揭露的真相"],
                    "milestones": [
                      {"index": 1, "milestone_title": "...", "synopsis": "...",
                       "key_conflicts": [...], "characters_involved": [...],
                       "thematic_focus": "...", "expected_tone": "..."}
                    ],
                    "climax_milestone": 14,
                    "final_boss_hints": "...",
                    "major_themes": [...]
                  }

            Returns:
                确认消息
            """
            store = self_ref._roadmap_store
            if store is None:
                return "错误：路线图存储未初始化"

            try:
                new_roadmap = json.loads(roadmap_json)
            except json.JSONDecodeError as e:
                return f"错误：路线图 JSON 解析失败: {e}"

            if not isinstance(new_roadmap, dict):
                return "错误：路线图必须是 JSON 对象"

            if "milestones" not in new_roadmap:
                return "错误：路线图必须包含 milestones 数组"

            store["data"] = new_roadmap
            store["chapter_index"] = 0
            store["dirty"] = True

            # 立即落盘，避免 Agent 后续崩溃导致路线图丢失
            project_dir = store.get("project_dir", "")
            if project_dir:
                try:
                    import os as _os
                    _path = _os.path.join(project_dir, "roadmap.json")
                    with open(_path, "w", encoding="utf-8") as _f:
                        json.dump(new_roadmap, _f, ensure_ascii=False, indent=2)
                    _path2 = _os.path.join(project_dir, "roadmap_index.json")
                    with open(_path2, "w", encoding="utf-8") as _f:
                        json.dump(0, _f)
                except Exception:
                    pass  # 落盘失败不阻塞规划流程

            ms_count = len(new_roadmap.get("milestones", []))
            return (
                f"路线图已更新: 《{new_roadmap.get('roadmap_title', '?')}》"
                f" — {ms_count} 个里程碑。"
                f"当前进度已重置为第 1 个里程碑: "
                f"{new_roadmap['milestones'][0].get('milestone_title', '?')}"
            )

        @tool
        def verify_character(name: str) -> str:
            """验证角色的当前状态（生死/失踪/活跃）。

            当你需要用到某个角色但不确定其最新状态时，调用此工具。
            已验证过的角色不会重复分析，直接返回缓存结果。

            验证过程:
            1. 在原文中定位该角色最后出场的章节
            2. 提取上下文 + LLM 分析 → 确定状态/结局/伏笔
            3. 更新角色状态缓存

            Args:
                name: 角色名

            Returns:
                角色的验证后状态信息
            """
            verified = self_ref._status_verified
            fixes = self_ref._status_fixes

            # 已验证过，直接返回缓存
            if name in verified:
                status = fixes.get(name, "?")
                return f"[已缓存] {name}: {status}（已验证，无需重复分析）"

            novel_text = self_ref._novel_text
            if not novel_text:
                return f"无法验证 {name}：小说原文不可用"

            chapters = PlotArchitect._split_novel_by_chapter(novel_text)
            if not chapters:
                return f"无法验证 {name}：章节解析失败"

            # 规则定位：角色出场的章节
            appeared = PlotArchitect._find_chapters_by_name(name, chapters)
            if not appeared:
                verified.add(name)
                return f"{name}: 未在原文中找到出场章节（可能为原创角色或名字拼写有误）"

            # 取最后 3 章文本
            last_chapters = sorted(appeared)[-3:]
            last_text = "\n".join(chapters.get(ch, "") for ch in last_chapters)
            context = PlotArchitect._extract_name_context(name, last_text)

            # LLM 分析
            dossier = self_ref._resolve_character_dossier(name, context, last_chapters[-1])

            verified.add(name)

            if dossier:
                resolved = dossier.get("status", "?")
                ending = dossier.get("ending", "")
                foreshadowing = dossier.get("foreshadowing", "")
                fixes[name] = resolved

                # 同步更新 KG CharacterNode，后续 lookup_character 直接读到最新数据
                graph = self_ref._state.graph
                if graph:
                    person = kg.get_person(graph, name) if hasattr(kg, 'get_person') else None
                    if person:
                        if person.status != resolved:
                            person._status = resolved
                        person.ending = ending or person.ending
                        person.foreshadowing = foreshadowing or person.foreshadowing
                        person.evidence = dossier.get("evidence", "") or person.evidence

                lines = [f"{name}: {resolved}"]
                if ending:
                    lines.append(f"  结局: {ending[:120]}")
                if foreshadowing and foreshadowing != "无":
                    lines.append(f"  伏笔: {foreshadowing[:120]}")
                evidence = dossier.get("evidence", "")
                if evidence:
                    lines.append(f"  原文依据: {evidence[:120]}")
                return "\n".join(lines)
            else:
                return f"{name}: 无法确定状态（LLM 返回空），请基于原文上下文自行判断"

        @tool
        def query_narrative_pattern(start_ch: int = 0, end_ch: int = 0) -> str:
            """查询原文的叙事模式（节奏曲线、钩子偏好、爽点密度）。

            用于帮助理解"原著的叙事习惯"，让续写规划更贴近原作节奏。

            Args:
                start_ch: 起始章节号（0 表示取最新的叙事分析数据）
                end_ch: 结束章节号（0 表示取所有可用数据）

            Returns:
                格式化的叙事模式摘要文本
            """
            sm = self_ref._state.story_memory
            if not sm:
                return "故事记忆不可用"

            if start_ch > 0 and end_ch > 0:
                cards_in_range = [
                    c for c in sm.narrative_cards.values()
                    if start_ch <= c.chapter_number <= end_ch
                ]
                cards = sorted(cards_in_range, key=lambda c: c.chapter_number)
            else:
                cards = sorted(
                    sm.narrative_cards.values(),
                    key=lambda c: c.chapter_number,
                )

            if not cards:
                return "暂无叙事分析数据。请确认已对原文进行了叙事分析。"

            lines = [f"叙事模式分析（共 {len(cards)} 章数据）"]
            lines.append(f"章节范围: 第{cards[0].chapter_number}-{cards[-1].chapter_number}章")

            # 节奏分布
            rhythm_counts = {}
            for c in cards:
                r = c.rhythm_type or "未标注"
                rhythm_counts[r] = rhythm_counts.get(r, 0) + 1
            total = len(cards)
            rhythm_parts = [f"{k} {v}/{total} ({v*100//total}%)" for k, v in sorted(rhythm_counts.items(), key=lambda x: -x[1])]
            lines.append(f"节奏分布: {', '.join(rhythm_parts)}")

            # 钩子偏好
            hook_counts = {}
            for c in cards:
                h = c.closing_hook_type or "未标注"
                hook_counts[h] = hook_counts.get(h, 0) + 1
            hook_parts = [f"{k} {v}次" for k, v in sorted(hook_counts.items(), key=lambda x: -x[1])]
            lines.append(f"章尾钩子偏好: {', '.join(hook_parts)}")

            # 爽点密度
            highlights = [c for c in cards if c.highlight_type and c.highlight_type != "无"]
            if highlights:
                density = len(cards) / len(highlights)
                types = set(h.highlight_type for h in highlights)
                lines.append(f"爽点密度: 每 {density:.1f} 章一个 (类型: {', '.join(types)})")
            else:
                lines.append("爽点密度: 无明显标注爽点")

            # 最近 5 章情绪曲线
            recent = cards[-5:]
            lines.append("最近 5 章情绪走向:")
            for c in recent:
                lines.append(f"  第{c.chapter_number}章: {c.emotion_arc or '?'} [{c.rhythm_type or '?'}]")

            # 批级聚合
            batch = sm.get_latest_batch_summary()
            if batch:
                lines.append(f"\n最近的批级聚合 (第{batch.chapters_range[0]}-{batch.chapters_range[1]}章):")
                if batch.emotion_curve:
                    lines.append(f"  情绪曲线: {batch.emotion_curve}")
                if batch.rhythm_pattern:
                    lines.append(f"  节奏模式: {batch.rhythm_pattern}")

            return "\n".join(lines)

        @tool
        def query_foreshadowing_status() -> str:
            """查询当前伏笔台账的快照摘要。

            返回: 全部待回收伏笔 / 已过期伏笔 / 即将到期的伏笔的格式化摘要。
            用于帮助决定本章需要推进或回收哪些伏笔。

            Returns:
                格式化伏笔摘要文本
            """
            sm = self_ref._state.story_memory
            if not sm:
                return "故事记忆不可用"

            # 检查是否有 ForeshadowingLedger
            ledger = getattr(sm, 'foreshadowing_ledger', None)
            if ledger is None:
                # 回退到旧 pending_threads
                pending = sm.get_pending_threads()
                if not pending:
                    return "暂无待回收伏笔。"
                lines = ["## 伏笔台账（旧格式）", f"共 {len(pending)} 条待回收:"]
                for t in pending:
                    lines.append(f"  - [{t.get('status', '?')}] {t.get('thread', '?')} (埋于第{t.get('introduced_ch', '?')}章)")
                return "\n".join(lines)

            pending = ledger.get_pending()
            stale = ledger.get_stale(threshold=30)

            lines = ["## 伏笔台账"]
            lines.append(f"待回收: {len(pending)} 条")

            if stale:
                lines.append(f"⚠ 超30章未推进 (可能已遗忘): {len(stale)} 条")
                for e in stale:
                    lines.append(f"  - {e.id}: {e.description} (埋于第{e.buried_chapter}章)")

            if pending:
                lines.append("\n近期需处理:")
                for e in pending[:10]:
                    lines.append(f"  - {e.id}: {e.description} (计划第{e.planned_resolution_chapter}章回收)")

            return "\n".join(lines)

        return [
            lookup_roadmap,
            update_roadmap,
            verify_character,
            query_narrative_pattern,
            query_foreshadowing_status,
            gather_active_conflicts,
            lookup_character,
        ]
