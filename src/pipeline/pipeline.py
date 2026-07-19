# -*- coding: utf-8 -*-
"""ContinuationPipeline —— 续写流水线编排器。

串联 3 个 Agent:
  ① Plot Architect → 两级规划（篇章路线图 + 章节大纲）
  ② Chapter Writer → 流式写作（核心）
  ③ Review Editor → 审校 + 修订（一次调用完成）

管理 SSE 事件总线，通过 AsyncGenerator 将事件推送到 HTTP handler。
"""

import asyncio
import json
import os
import re
from typing import AsyncGenerator, Optional, TYPE_CHECKING

from .fragment import PipelineEvent, StoryFragment
from .state import PipelineState
from ..agents.plot_architect import PlotArchitect, make_fallback_chapter, make_fallback_roadmap
from ..agents.chapter_writer import ChapterWriter
from ..agents.review_editor import ReviewEditor
from .story_memory import StoryMemory

if TYPE_CHECKING:
    from ..core.context import GlobalContext, ServiceRegistry
    from ..core.llm import UnifiedLLM


class ContinuationPipeline:
    """续写流水线编排器。

    用法:
        pipeline = ContinuationPipeline(ctx, services, llm)
        pipeline.load_novel(novel_path)

        async for event in pipeline.run("让江停更主动"):
            send_sse(event)
    """

    def __init__(self, ctx: "GlobalContext", services: "ServiceRegistry",
                 llm: "UnifiedLLM"):
        self._ctx = ctx
        self._services = services
        self._llm = llm
        self._kg = services.kg

        # 3 个 Agent
        self.architect: Optional[PlotArchitect] = None
        self.writer: Optional[ChapterWriter] = None
        self.review_editor: Optional[ReviewEditor] = None

        # 状态
        self._phase: str = "idle"
        self._chapter: int = 0
        self._fragment_count: int = 0

        # 角色状态验证标记：已验证过的角色不再重复验证
        self._status_verified: set = set()
        # 验证修正的状态（待持久化）
        self._status_fixes: dict = {}
        # 当前故事弧线（Plot Architect 规划多章，Pipeline 逐章执行）
        self._pending_arc: dict = {}
        self._arc_chapter_index: int = 0

        # 篇章路线图（10-20 章高层规划，持久化到 roadmap.json）
        self._roadmap: dict = {}
        self._roadmap_chapter_index: int = 0

        # 缓存数据
        self._style_profile = None
        self._character_profiles: dict = {}
        self._previous_chapter_ending: str = ""
        self._novel_text: str = ""  # load_novel 时缓存
        self._introduced_threads: list = []  # 路线图 + 各章引入的伏笔
        self._chapter_plan_history: list = []  # 前序章节规划摘要（兼容旧引用）
        self._stop_requested: bool = False  # inject("stop") 或前端关闭 SSE 触发

        # 应用层记忆：故事状态的单一事实源
        self._story_memory = StoryMemory()

        # Agent 共享状态（取代 ctx/services 的全量注入）
        self._state = PipelineState()

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def chapter(self) -> int:
        return self._chapter

    @property
    def fragment_count(self) -> int:
        return self._fragment_count

    def load_novel(self, novel_path: str):
        """加载小说并准备续写上下文。

        包括:
          1. 解析章节
          2. 提取/加载 KG
          3. 蒸馏角色 Profile（已有）
          4. 蒸馏文风 Profile（新增）
          5. 获取最后一章结尾

        Args:
            novel_path: 小说文件路径（如 novels/poyun.txt）
        """
        from ..core.chapter_parser import parse_novel_chapters
        from ..core.models import Novel
        from ..services.project_service import ProjectService as PS
        from ..distillers.character_distiller import CharacterDistiller
        from ..distillers.style_distiller import AuthorStyleDistiller

        # 1. 加载文本 & 解析章节
        text = PS.read_text_file(novel_path)
        self._novel_text = text  # 缓存全文，verify_character 需要
        base_name = novel_path.replace("\\", "/").split("/")[-1].rsplit(".", 1)[0]
        chapters = parse_novel_chapters(text, base_name)
        self._chapter = len(chapters)

        # 2. KG: 优先读缓存，没有才提取
        cached = self._find_cached_project(base_name, len(chapters))
        if cached:
            self._ctx.novel = cached
            print(f"[KG] 从缓存加载：{cached.story_graph.total_node_count} 个节点, "
                  f"{cached.story_graph.total_edge_count} 条边")
        else:
            project_dir = self._services.project.create_project_dir(base_name)
            self._ctx.novel = Novel(
                title=base_name,
                file_path=os.path.abspath(novel_path) if hasattr(os, 'path') else novel_path,
                chapters=chapters,
                output_dir=project_dir,
            )
            print(f"[KG] 正在逐章提取知识图谱（{len(chapters)} 章）...")
            self._ctx.novel.story_graph = self._services.kg.extract_incremental(
                chapters,
                batch_size=int(__import__('os').getenv("KG_BATCH_SIZE", "10")),
            )
            self._services.project.save_novel(self._ctx.novel)
            graph = self._ctx.novel.story_graph
            print(f"[KG] 完成：{graph.total_node_count} 个节点, "
                  f"{graph.total_edge_count} 条边")

        graph = self._ctx.novel.story_graph

        # 加载之前验证过的状态修正，覆盖 KG 中的过时值
        self._status_fixes = self._load_status_fixes()
        if self._status_fixes:
            persons = self._kg.get_all_persons(graph)
            for p in persons:
                if p.name in self._status_fixes:
                    fixed = self._status_fixes[p.name]
                    if p.status != fixed:
                        print(f"  [KG Fix] {p.name}: {p.status} → {fixed} (从缓存恢复)")
                        p._status = fixed
            print(f"  [KG] 从缓存恢复 {len(self._status_fixes)} 个状态修正")

        # 2.5 加载篇章路线图（优先读缓存）
        project_dir = self._ctx.novel.output_dir or ""
        self._roadmap = self._load_roadmap(project_dir) if project_dir else {}
        self._roadmap_chapter_index = self._load_roadmap_index(project_dir) if project_dir else 0
        if self._roadmap:
            ms_count = len(self._roadmap.get("milestones", []))
            print(f"[Roadmap] 从缓存加载：{ms_count} 个里程碑, 当前第 {self._roadmap_chapter_index + 1} 个")

        # 3. 蒸馏文风 Profile（优先读缓存）
        distiller = AuthorStyleDistiller(self._llm)
        project_dir = self._ctx.novel.output_dir or ""
        cached_style = project_dir and self._load_cached_style(project_dir)
        if cached_style:
            self._style_profile = cached_style
            print(f"[Style] 从缓存加载文风 Profile")
        else:
            self._style_profile = distiller.distill(text)
            if project_dir:
                self._save_cached_style(project_dir, self._style_profile)
            print(f"[Style] 文风蒸馏完成")

        # 4. 蒸馏主要角色 Profile（importance >= 5，最多 8 个）—— 优先读缓存
        project_dir = self._ctx.novel.output_dir or ""
        cached_profiles = project_dir and self._load_cached_char_profiles(project_dir)
        char_distiller = CharacterDistiller(self._llm, self._kg)
        persons = self._kg.get_all_persons(graph)
        important = [p for p in persons if p.importance >= 5]
        to_distill = [p for p in important[:8]
                      if cached_profiles is None or p.name not in cached_profiles]

        if cached_profiles:
            self._character_profiles = cached_profiles
            print(f"[Char] 从缓存加载 {len(cached_profiles)} 个角色 Profile")

        if to_distill:
            print(f"[Char] 蒸馏 {len(to_distill)} 个新角色...", end=" ", flush=True)
            distilled_count = 0
            for person in to_distill:
                try:
                    profile = char_distiller.distill_character(
                        person.name, text, graph,
                    )
                    self._character_profiles[person.name] = profile
                    distilled_count += 1
                    print(f"{person.name}", end=" ", flush=True)
                except Exception:
                    print(f"{person.name}(失败)", end=" ", flush=True)
            print(f"| 完成 {distilled_count}/{len(to_distill)}")

            # 保存到缓存
            if project_dir:
                self._save_cached_char_profiles(project_dir, self._character_profiles)
        elif not cached_profiles:
            print(f"[Char] 无角色需要蒸馏")

        # 4.5 初始化 StoryMemory（从 KG + 验证缓存构建角色状态）
        persons = self._kg.get_all_persons(graph)
        for p in persons:
            status = self._status_fixes.get(p.name, p.status or "active")
            self._story_memory.update_character(
                p.name,
                status=status,
                verified=p.name in self._status_verified,
                role_type=p.role_type or "",
                importance=p.importance or 0,
                faction=p.faction or "",
                ending=getattr(p, 'ending', '') or '',
                foreshadowing=getattr(p, 'foreshadowing', '') or '',
            )
        if hasattr(graph, 'enemy_pairs'):
            conflicts = []
            for pair in self._kg.enemy_pairs(graph):
                rel = graph.get_relationship_edge(pair[0], pair[1])
                if rel:
                    conflicts.append({
                        "characters": list(pair),
                        "tension": rel.current_tension or "?",
                        "description": (rel.shared_history or "")[:120],
                    })
            self._story_memory.update_conflicts(conflicts)
        print(f"[StoryMemory] 初始化: {len(self._story_memory.character_states)} 个角色, "
              f"{len(self._story_memory.active_conflicts)} 对冲突")

        # 5. 提取最后一章结尾
        if chapters:
            last_ch = chapters[-1]
            self._previous_chapter_ending = last_ch.content[-3000:] if len(last_ch.content) > 3000 else last_ch.content
            print(f"[Context] 最后一章: 第{last_ch.index}章, "
                  f"结尾上下文: {len(self._previous_chapter_ending)} 字")

        # 6. 初始化 Agent
        print(f"[Agents] 初始化 3 个 Agent...", end=" ", flush=True)
        self._init_agents()
        print("完成")

        # 7. 尾部叙事分析（新增）
        from ..distillers.narrative_distiller import NarrativeDistiller
        distiller = NarrativeDistiller(self._llm)
        print(f"[Narrative] 正在分析尾部 15 章叙事特征...")
        tail_cards = distiller.analyze_tail(self._novel_text, last_n=15)
        for card in tail_cards:
            self._story_memory.narrative_cards[card.chapter_number] = card
        print(f"[Narrative] 完成: {len(tail_cards)} 张叙事卡")

        # 8. Session 恢复：检测已生成章节并从断点继续
        self._restore_session(project_dir)

    def _verify_characters_in_text(self, text: str):
        """按需验证：提取文本中提到的角色，对其做现场状态验证。

        只有尚未验证过的角色才会触发规则定位 + LLM 分析。
        验证后加入 _status_verified 集合，后续不再重复。

        调用时机:
          - Plot Architect 生成大纲后（验证大纲中涉及的角色）
          - 用户 inject 指令后（验证指令中提到的角色）

        Args:
            text: 大纲文本或用户指令文本（从中提取角色名）
        """
        graph = self._ctx.novel.story_graph if self._ctx.novel else None
        if not graph:
            return

        # 找出文本中提到的、尚未验证的角色名
        persons = self._kg.get_all_persons(graph)
        # 按名字长度降序排列，避免"江停"匹配到"江停的前队友"中的子串问题
        mentioned = []
        for p in sorted(persons, key=lambda x: -len(x.name)):
            if p.name and p.name in text and p.name not in self._status_verified:
                mentioned.append(p)

        if not mentioned:
            return

        names = [p.name for p in mentioned]
        print(f"  [Verify] 现场验证 {len(mentioned)} 个角色: {', '.join(names)}")

        novel_text = self._get_novel_text()
        if not novel_text:
            return

        chapters = self._split_novel_by_chapter(novel_text)
        if not chapters:
            return

        for person in mentioned:
            name = person.name

            # 规则：定位该角色最后出场的章节
            appeared = self._find_chapters_by_name(name, chapters)
            if not appeared:
                self._status_verified.add(name)
                print(f"    {name}: 未在原文中找到出场章节")
                continue

            # 取最后 3 章的文本 + 角色名周围上下文
            last_chapters = sorted(appeared)[-3:]
            print(f"    {name}: 定位最后出场章节 {last_chapters} → LLM 分析中...",
                  end=" ", flush=True)

            last_text = "\n".join(chapters.get(ch, "") for ch in last_chapters)
            context = self._extract_name_context(name, last_text)

            # LLM 现场分析（完整档案，不只是状态）
            dossier = self._llm_resolve_character_dossier(
                name, context, last_chapters[-1],
            )

            # 标记已验证
            self._status_verified.add(name)

            if dossier:
                resolved = dossier.get("status", "")
                ending = dossier.get("ending", "")
                foreshadowing = dossier.get("foreshadowing", "")

                old = person.status
                if resolved != old:
                    print(f"{old} → {resolved} ({ending[:40]}...)")
                    print(f"  [KG Fix] {name}: {old} → {resolved}")
                else:
                    print(f"确认 {resolved}" + (f" — {ending[:40]}" if ending else ""))
                person._status = resolved  # 修正 KG PersonNode
                self._status_fixes[name] = resolved
                # 同步到 StoryMemory
                self._story_memory.update_character(name, status=resolved, verified=True,
                                                    ending=ending, foreshadowing=foreshadowing)
            else:
                print("无法确定（LLM 返回空）")

        # 持久化状态修正到磁盘（下次启动自动加载）
        if self._status_fixes:
            self._save_status_fixes()

    def _load_status_fixes(self) -> dict:
        """加载之前验证过的状态修正。"""
        project_dir = self._ctx.novel.output_dir if self._ctx.novel else ""
        if not project_dir:
            return {}
        path = os.path.join(project_dir, "status_fixes.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_status_fixes(self):
        """持久化状态修正到项目目录。"""
        project_dir = self._ctx.novel.output_dir if self._ctx.novel else ""
        if not project_dir:
            return
        path = os.path.join(project_dir, "status_fixes.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._status_fixes, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _llm_resolve_character_dossier(self, name: str, context: str,
                                        last_chapter: int) -> dict:
        """LLM 分析角色完整档案：状态 + 结局 + 伏笔。

        基于该角色最后几次出场的原文场景，全面分析。
        返回的 dossier 会注入 Writer prompt 作为背景约束。

        Args:
            name: 角色名
            context: 该角色最后几次出场的上下文文本（已由规则提取）
            last_chapter: 最后出场章节

        Returns:
            {"status": "dead", "ending": "第112章被枪击身亡...",
             "foreshadowing": "他死前提到'组织里还有内鬼'，这个线索尚未解决",
             "key_relationships": "与严峫是敌对关系，与黑桃K是上下级",
             "evidence": "确认状态的原文引用"}
            或空 dict（无法确定）
        """
        if not context or len(context) < 20:
            return {}

        prompt = f"""你是专业小说分析员。根据角色最后几次出场的原文片段，全面分析该角色的当前状态和结局。

角色: {name}
最后出场章节: 第{last_chapter}章

原文场景:
{context[:3000]}

请返回 JSON:
{{
  "status": "dead|active|missing|arrested",
  "ending": "该角色在原文中的结局——如何退场的？最后在做什么？一句话概括。如果已死亡，说明死因和方式。",
  "foreshadowing": "该角色身上还有哪些未解决的伏笔或线索？比如死前未说完的话、未完成的计划、留下的悬念。如果没有，填'无'。",
  "key_relationships": "该角色与其他角色的关键关系——对谁重要？谁在意他的生死？一句话概括。",
  "evidence": "证明以上判断的原文关键句引用"
}}

只返回 JSON。"""

        try:
            result = self._llm.chat_json(
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

    def _get_novel_text(self) -> str:
        """获取小说全文文本。优先用 load_novel 时缓存的 _novel_text。"""
        if self._novel_text:
            return self._novel_text
        if self._ctx.novel and self._ctx.novel.file_path:
            try:
                from ..services.project_service import ProjectService as PS
                return PS.read_text_file(self._ctx.novel.file_path)
            except Exception:
                pass
        return ""

    @staticmethod
    def _split_novel_by_chapter(text: str) -> dict:
        """按章节切分小说。

        Returns:
            {chapter_number: chapter_text}
        """
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
                current_ch = ContinuationPipeline._parse_chapter_number(m.group(1))
                current_text = [part]
            else:
                current_text.append(part)

        if current_ch > 0 and current_text:
            chapters[current_ch] = "".join(current_text)

        return chapters

    @staticmethod
    def _find_chapters_by_name(name: str, chapters: dict) -> list[int]:
        """规则定位：角色在哪些章节出场（纯字符串匹配）。

        Args:
            name: 角色名
            chapters: {ch_num: text}

        Returns:
            章节号列表
        """
        appeared = []
        for ch_num in sorted(chapters.keys()):
            if name in chapters[ch_num]:
                appeared.append(ch_num)
        return appeared

    @staticmethod
    def _extract_name_context(name: str, text: str,
                              window: int = 300) -> str:
        """提取角色名周围上下文段落。

        在文本中找到角色名每次出现的位置，提取前后各 window 字的上下文。
        取最后 5 处出现（最接近结局的）。

        Returns:
            拼接后的上下文字符串
        """
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

        # 取最后 5 处
        return "\n---\n".join(contexts[-5:]) if contexts else text[-2000:]

    @staticmethod
    def _parse_chapter_number(title: str) -> int:
        """从 '第X章' 中解析章节号。"""
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

    def _init_agents(self):
        """初始化 3 个 Agent。"""
        # 构建共享状态（此后 agent 通过 state 按需访问，不再依赖 ctx/services）
        self._state = PipelineState(
            novel=self._ctx.novel if self._ctx else None,
            style_profile=self._style_profile,
            character_profiles=self._character_profiles,
            story_memory=self._story_memory,
            status_verified=self._status_verified,
            status_fixes=self._status_fixes,
            novel_text=self._novel_text,
            kg=self._kg,
            agent_llm=self._services.agent_llm if self._services else None,
            sync_llm=self._llm,
        )

        self.architect = PlotArchitect(
            self._state.agent_llm, self._kg, self._state,
        )
        self.writer = ChapterWriter(
            self._state.agent_llm, self._kg, self._state,
        )
        self.review_editor = ReviewEditor(
            self._state.agent_llm, self._kg, self._state,
        )

    async def run(self, instruction: str = "",
                  auto_loop: bool = True) -> AsyncGenerator[PipelineEvent, None]:
        """运行续写流水线。

        使用 while 循环自动推进多章：Planning → Writing → Review → 下一章。
        Architect 的 AgentFlow 会话在循环中持久——working memory 跨章累积。

        Args:
            instruction: 用户初始指令（仅首章有效）
            auto_loop: True=自动循环直到路线图耗尽, False=只写一章（CLI 调试用）

        Yields:
            PipelineEvent: 每个事件通过 SSE 推送
        """
        if not self.architect or not self.writer or not self.review_editor:
            raise RuntimeError("请先调用 load_novel() 加载小说")

        max_chapters = int(os.getenv("MAX_CHAPTERS", "5"))
        self._stop_requested = False
        self._fragment_count = 0
        chapter_count = 0
        active_instruction = instruction

        project_dir = self._ctx.novel.output_dir if self._ctx.novel else ""

        # ── 主循环：每迭代一章 ──
        while not self._stop_requested and chapter_count < max_chapters:
            chapter_count += 1
            ch_num = self._chapter + 1

            # ================================================
            # Phase 1: Planning（Architect AgentFlow 会话持久）
            # ================================================
            self._phase = "planning"
            print(f"\n{'─'*40}\n[第{chapter_count}章] Phase 1/3 — Plot Architect 规划第{ch_num}章...")
            yield PipelineEvent("phase", {"phase": "planning"})

            # 构建 roadmap_store（可变，Agent 工具直接读写）
            roadmap_store = {
                "data": self._roadmap,
                "chapter_index": self._roadmap_chapter_index,
                "next_chapter": ch_num,
                "dirty": False,
                "project_dir": project_dir,
            }

            # 同步 PipelineState 中每章会变的部分
            self._state.refresh_statuses()
            self._state.novel_text = self._get_novel_text()

            self.architect.set_context(
                previous_chapter_ending=self._previous_chapter_ending,
                last_chapter=self._chapter,
                user_instruction=active_instruction,
                roadmap_store=roadmap_store,
                previous_chapter_plans=self._chapter_plan_history,
            )

            chapter_raw = await self.architect.run(
                f"请完成第{ch_num}章的详细规划。你的 skill 和工具已经就绪，直接开始规划。"
            )

            # 同步路线图（Agent 可能通过 update_roadmap 工具修改了它）
            if roadmap_store.get("dirty"):
                self._roadmap = roadmap_store["data"]
                self._roadmap_chapter_index = roadmap_store.get("chapter_index", 0)
                if project_dir:
                    self._save_roadmap(project_dir, self._roadmap)
                    self._save_roadmap_index(project_dir, self._roadmap_chapter_index)
                ms_count = len(self._roadmap.get("milestones", []))
                print(f"  [Roadmap] 路线图更新: {self._roadmap.get('roadmap_title', '?')} — {ms_count} 个里程碑")

            # 解析章节规划
            chapter = self._parse_chapter_plan(chapter_raw)
            ch_title = chapter.get("title", "")

            # 检查是否是大结局标记
            if chapter.get("is_final"):
                print(f"  [Architect] 标记为大结局，写完本章后结束")
                self._stop_requested = True

            # 累积规划历史（StoryMemory 统一管理）
            plan_summary = {
                "chapter_number": chapter.get("chapter_number", ch_num),
                "title": ch_title,
                "synopsis": chapter.get("synopsis", ""),
                "characters_involved": list(chapter.get("character_beats", {}).keys()) if isinstance(chapter.get("character_beats"), dict) else [],
                "key_events": [s.get("goal", "") for s in chapter.get("sections", [])],
                "plot_threads_introduced": chapter.get("plot_threads_introduced", []),
            }
            self._story_memory.add_chapter_plan(plan_summary)
            self._chapter_plan_history = self._story_memory.chapter_plans  # 兼容旧引用

            # 收集伏笔（StoryMemory 去重管理）
            if roadmap_store.get("dirty"):
                for t in self._roadmap.get("plot_threads_introduced", []):
                    self._story_memory.add_thread(t, ch_num)
            for t in chapter.get("plot_threads_introduced", []):
                self._story_memory.add_thread(t, ch_num)
            self._introduced_threads = self._story_memory.get_pending_threads()

            # 持久化
            if project_dir:
                self._save_chapter_plan(project_dir, ch_num, chapter)

            self._pending_arc = {"chapters": [chapter]}
            self._arc_chapter_index = 0
            yield PipelineEvent("outline", self._pending_arc)

            # ================================================
            # Phase 2: Writing
            # ================================================
            self._phase = "writing"
            sections = chapter.get("sections", [])
            if not sections:
                sections = [{"name": "main", "goal": chapter.get("synopsis", ""),
                             "characters": [], "key_beats": [], "target_fragments": 20}]

            print(f"[第{chapter_count}章] Phase 2/3 — 写作「{ch_title}」{len(sections)} 节")
            yield PipelineEvent("phase", {"phase": "writing"})
            yield PipelineEvent("fragment", {
                "type": "divider", "text": "",
                "divider_label": f"第{ch_num}章「{ch_title}」"
            })

            self.writer.set_context(
                chapter=chapter,
                previous_chapter_ending=self._previous_chapter_ending,
                plot_threads=self._introduced_threads,
            )

            draft_fragments = []
            for i, section in enumerate(sections):
                section_name = section.get("name", f"section_{i}")
                print(f"  [{i+1}/{len(sections)}] {section_name}: "
                      f"{section.get('goal', '')[:50]}")

                async for fragment in self.writer.stream(section, section_index=i):
                    draft_fragments.append(fragment)
                    self._fragment_count += 1
                    yield PipelineEvent("fragment", fragment.to_dict())

                if draft_fragments:
                    recent = draft_fragments[-3:]
                    self._previous_chapter_ending = "\n".join(
                        f"[{f.type}] {f.character + ': ' if f.character else ''}{f.text}"
                        for f in recent
                    )

            # 推进状态
            self._arc_chapter_index += 1
            self._chapter = ch_num
            self._roadmap_chapter_index += 1
            if project_dir:
                self._save_roadmap_index(project_dir, self._roadmap_chapter_index)

            # ================================================
            # Phase 3: Review
            # ================================================
            self._phase = "reviewing"
            print(f"[第{chapter_count}章] Phase 3/3 — 审校修订...")
            yield PipelineEvent("phase", {"phase": "reviewing"})

            self.review_editor.set_context(draft_fragments=draft_fragments)
            result_raw = await self.review_editor.run("审校并修订草稿")
            result = self._parse_review_result(result_raw)

            changes = result.get("changes", [])
            score = result.get("overall_score", "?")
            revised = result.get("revised_fragments",
                                 [f.to_dict() for f in draft_fragments])

            if changes:
                print(f"  审校: {len(changes)} 处修改 (评分: {score})")
            else:
                print(f"  审校: 通过 (评分: {score})")

            yield PipelineEvent("review", {
                "issues": [{"type": c.get("reason", ""), "severity": "fixed",
                            "description": c.get("original", "")[:80],
                            "suggestion": c.get("revised", "")[:80]}
                           for c in changes],
                "overall_score": score,
            })
            yield PipelineEvent("complete", {
                "chapter": ch_num,
                "title": ch_title,
                "revised_fragments": revised,
                "changes": changes,
                "chapter_count": chapter_count,
                "total_fragments": self._fragment_count,
            })

            # ── 持久化完整章节数据（规划 + 片段 + 审校） ──
            if project_dir:
                self._save_chapter_full(project_dir, ch_num, chapter,
                                        revised, changes, score)

            # 首次指令只用一次
            active_instruction = ""

            # ── 应用层记忆更新 ──
            self._story_memory.post_chapter(chapter, draft_fragments, ch_num)

            # ── KG 增量更新：将新章节的角色/事件/关系回写到知识图谱 ──
            await self._update_kg_with_new_chapter(draft_fragments, ch_num, project_dir)

            # ── 章节压缩检查 ──
            if len(self._story_memory.chapter_plans) > 10:
                await self._story_memory.compact(self._llm)

            # 检查是否应该继续循环
            if not auto_loop:
                break

            has_roadmap = bool(self._roadmap.get("milestones"))
            if not has_roadmap:
                # 无路线图 → Architect 没有创建路线图，停止
                print(f"  [Loop] 无篇章路线图，续写结束")
                break

            milestones = self._roadmap.get("milestones", [])
            if self._roadmap_chapter_index >= len(milestones):
                print(f"  [Loop] 路线图 {len(milestones)} 个里程碑全部完成")
                if self._roadmap.get("is_final_roadmap"):
                    print(f"  [Loop] 最终路线图完成，续写结束")
                    break
                # Architect 下次规划会创建新路线图
                print(f"  [Loop] 下轮将触发新路线图规划")

        # ── 结束 ──
        self._phase = "idle"
        reason = "stop_requested" if self._stop_requested else \
                 "max_chapters" if chapter_count >= max_chapters else \
                 "roadmap_exhausted"
        print(f"{'─'*40}\n[完成] {chapter_count} 章, {self._fragment_count} 个片段 ({reason})")
        yield PipelineEvent("done", {"chapters_written": chapter_count, "reason": reason})

    async def inject(self, instruction: str):
        """接收用户注入指令，转发到 Chapter Writer。

        特殊指令:
          - "stop" / "停止": 在当前章节完成后停止循环

        Args:
            instruction: 用户自然语言指令
        """
        if instruction.strip().lower() in ("stop", "停止", "结束"):
            self._stop_requested = True
            print(f"  [Inject] 收到停止指令，当前章节完成后将结束循环")
            return

        # 按需验证：用户指令中可能提到新角色
        self._verify_characters_in_text(instruction)
        if self.writer:
            await self.writer.inject(instruction)

    async def _update_kg_with_new_chapter(self, fragments: list, ch_num: int,
                                          project_dir: str):
        """将新章节内容增量更新到知识图谱。

        从 draft_fragments 拼接章节文本，调用 KG 增量更新，
        新角色/事件/关系会被合并到 StoryGraph 中。

        Args:
            fragments: 本章所有 StoryFragment 列表
            ch_num: 本章章节号
            project_dir: 项目输出目录（用于保存更新后的 KG）
        """
        graph = self._ctx.novel.story_graph if self._ctx.novel else None
        if not graph or not fragments:
            return

        # 拼接章节文本
        chapter_text = "\n".join(
            (f"{f.character}: " if f.character else "") + f.text
            for f in fragments
        )
        if not chapter_text.strip():
            return

        # 记录更新前状态
        old_names = {p.name for p in self._kg.get_all_persons(graph)}
        before_persons = len(old_names)
        before_events = len(graph.event_nodes)

        try:
            updated = self._kg.update_with_chapter(graph, chapter_text, ch_num)
            self._ctx.novel.story_graph = updated

            after_persons = len(self._kg.get_all_persons(updated))
            after_events = len(updated.event_nodes)
            new_chars = after_persons - before_persons
            new_events = after_events - before_events

            if new_chars or new_events:
                print(f"  [KG] 新章节更新: +{new_chars} 角色, +{new_events} 事件")

                # 找出新增的角色
                new_persons = [p for p in self._kg.get_all_persons(updated)
                              if p.name not in old_names]

                for p in new_persons:
                    # 同步新角色到 StoryMemory
                    self._story_memory.update_character(
                        p.name, status=p.status or "active",
                        verified=False, role_type=p.role_type or "",
                        importance=p.importance or 0,
                        faction=p.faction or "",
                    )
                    print(f"  [KG] 新角色「{p.name}」已同步到 StoryMemory")

                    # 蒸馏新角色的 CharacterProfile（Voice/Boundary/State）
                    if p.importance >= 5 and self._novel_text:
                        try:
                            from ..distillers.character_distiller import CharacterDistiller
                            distiller = CharacterDistiller(self._llm, self._kg)
                            profile = distiller.distill_character(
                                p.name, self._novel_text + "\n" + chapter_text, updated,
                            )
                            self._character_profiles[p.name] = profile
                            print(f"  [Distill] 新角色「{p.name}」Profile 蒸馏完成")
                            if project_dir:
                                self._save_cached_char_profiles(
                                    project_dir, self._character_profiles,
                                )
                        except Exception as e:
                            print(f"  [Distill] 新角色「{p.name}」蒸馏失败（非致命）: {e}")

            # 持久化更新后的 KG
            if project_dir:
                self._services.project.save_novel(self._ctx.novel)
        except Exception as e:
            print(f"  [KG] 新章节更新失败（非致命）: {e}")

    # ================================================================
    # Agent 输出解析
    # ================================================================

    @staticmethod
    def _parse_architect_output(raw) -> dict:
        """从 AgentFlow 输出中提取 JSON dict。

        处理 AgentResult / str / dict 三种格式。
        """
        from agentflow.runtime.builder import AgentResult

        if isinstance(raw, AgentResult):
            text = raw.output
        elif isinstance(raw, str):
            text = raw
        elif isinstance(raw, dict):
            return raw
        else:
            text = str(raw)

        if not text or not text.strip():
            return {}

        # 尝试直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 从 markdown 代码块或 JSON 块中提取
        import re
        for pattern in [
            r'```(?:json)?\s*\n?(.*?)\n?```',          # ```json ... ```
            r'\{.*"type"\s*:\s*"(?:roadmap|chapter)".*\}',  # JSON object
            r'\{.*"chapter_number"\s*:.*\}',
            r'\{.*"roadmap_title"\s*:.*\}',
            r'\{.*"sections"\s*:.*\}',
        ]:
            m = re.search(pattern, text, re.DOTALL)
            if m:
                group = m.group(1) if pattern.startswith(r'```') else m.group()
                try:
                    return json.loads(group)
                except (json.JSONDecodeError, IndexError):
                    continue

        # 兜底：找第一个 { 到最后一个 } 之间的内容
        first_brace = text.find('{')
        last_brace = text.rfind('}')
        if first_brace >= 0 and last_brace > first_brace:
            try:
                return json.loads(text[first_brace:last_brace + 1])
            except json.JSONDecodeError:
                pass

        return {}

    def _parse_roadmap(self, raw) -> dict:
        """解析篇章路线图。失败时返回兜底 roadmap。"""
        data = self._parse_architect_output(raw)
        if data and data.get("milestones"):
            data.setdefault("status", "ok")
            return data
        print("  [Warning] 路线图解析失败，使用兜底方案")
        return make_fallback_roadmap(self._chapter + 1)

    def _parse_chapter_plan(self, raw) -> dict:
        """解析单章章节规划。失败时返回兜底章节。"""
        data = self._parse_architect_output(raw)
        if data and data.get("sections"):
            data.setdefault("status", "ok")
            return data
        # 兼容旧格式：如果 data 自身就是 chapter dict（含 chapter_number）
        if data and data.get("chapter_number") and data.get("synopsis"):
            # 旧格式没有 sections，补上兜底 sections
            if "sections" not in data:
                fallback = make_fallback_chapter(data["chapter_number"])
                data["sections"] = fallback["sections"]
            data.setdefault("status", "ok")
            return data
        print("  [Warning] 章节规划解析失败，使用兜底方案")
        return make_fallback_chapter(self._chapter + 1)

    # ================================================================
    # 路线图持久化
    # ================================================================

    @staticmethod
    def _load_roadmap(project_dir: str) -> dict:
        """从项目目录加载篇章路线图。"""
        if not project_dir:
            return {}
        path = os.path.join(project_dir, "roadmap.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    @staticmethod
    def _save_roadmap(project_dir: str, roadmap: dict):
        """持久化篇章路线图到项目目录。"""
        if not project_dir:
            return
        path = os.path.join(project_dir, "roadmap.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(roadmap, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    @staticmethod
    def _load_roadmap_index(project_dir: str) -> int:
        """加载路线图当前里程碑索引。"""
        if not project_dir:
            return 0
        path = os.path.join(project_dir, "roadmap_index.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return 0

    @staticmethod
    def _save_roadmap_index(project_dir: str, index: int):
        """持久化路线图当前里程碑索引。"""
        if not project_dir:
            return
        path = os.path.join(project_dir, "roadmap_index.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(index, f)
        except Exception:
            pass

    def _save_chapter_plan(self, project_dir: str, chapter_number: int,
                           chapter: dict):
        """持久化单章章节规划到磁盘，便于审查。"""
        if not project_dir:
            return
        path = os.path.join(project_dir, f"chapter_{chapter_number:04d}.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(chapter, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _save_chapter_full(self, project_dir: str, chapter_number: int,
                           chapter_plan: dict, revised_fragments: list,
                           changes: list, overall_score):
        """持久化完整章节数据：规划 + 片段 + 审校结果。

        覆盖 _save_chapter_plan 写入的文件，追加 fragments、review。
        eval 系统的 collect_fixtures.py 从此文件提取质量评分和错误检测所需数据。
        """
        if not project_dir:
            return
        path = os.path.join(project_dir, f"chapter_{chapter_number:04d}.json")

        # 合并：保留原有规划字段 + 追加写作和审校数据
        full = dict(chapter_plan)
        full["fragments"] = revised_fragments
        full["fragment_count"] = len(revised_fragments)
        full["review"] = {
            "changes": changes,
            "overall_score": overall_score,
            "change_count": len(changes),
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(full, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

        # TXT 落盘：便于读者直接阅读
        txt_path = os.path.join(project_dir, f"chapter_{chapter_number:04d}.txt")
        try:
            title = chapter_plan.get("title", f"第{chapter_number}章")
            lines = [f"第{chapter_number}章 {title}", ""]
            for frag in revised_fragments:
                t = frag.get("type", "narration")
                text = frag.get("text", "")
                char = frag.get("character")
                if t == "dialogue" and char:
                    lines.append(f"{char}：「{text}」")
                elif t == "inner_thought" and char:
                    lines.append(f"（{char}心想：{text}）")
                elif t == "action" and char:
                    lines.append(f"（{char}{text}）")
                elif t == "divider":
                    label = frag.get("divider_label", "")
                    lines.append(f"\n  --- {label} ---\n" if label else "\n  ---\n")
                else:
                    lines.append(text)
                lines.append("")
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except Exception:
            pass

    # ================================================================
    # Session 恢复：扫描已生成章节并从断点继续
    # ================================================================

    @staticmethod
    def _scan_generated_chapters(project_dir: str, original_count: int) -> list:
        """扫描 project_dir 中已生成的续写章节号列表。

        Args:
            project_dir: 项目输出目录
            original_count: 原文章节数，仅返回大于此数的章节号

        Returns:
            已排序的章节号列表，仅包含续写生成的章节
        """
        if not project_dir or not os.path.isdir(project_dir):
            return []
        chapters = []
        pattern = re.compile(r'^chapter_(\d+)\.json$')
        for fname in os.listdir(project_dir):
            m = pattern.match(fname)
            if m:
                ch_num = int(m.group(1))
                if ch_num > original_count:
                    chapters.append(ch_num)
        chapters.sort()
        return chapters

    @staticmethod
    def _load_chapter_full_from_disk(project_dir: str, chapter_number: int) -> dict:
        """从磁盘加载单章完整数据（规划 + fragments + 审校）。

        Returns:
            章节数据 dict，文件不存在或解析失败返回空 dict
        """
        path = os.path.join(project_dir, f"chapter_{chapter_number:04d}.json")
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _restore_session(self, project_dir: str):
        """扫描已生成章节，恢复续写进度。

        从 project_dir/chapter_XXXX.json 检测已生成的续写章节，
        恢复 _chapter、_previous_chapter_ending、_story_memory。

        Args:
            project_dir: 项目输出目录
        """
        if not project_dir:
            return

        original_count = self._chapter
        generated = self._scan_generated_chapters(project_dir, original_count)
        if not generated:
            return

        max_gen = max(generated)
        self._chapter = max_gen
        print(f"[Resume] 检测到 {len(generated)} 章已生成续写章节 "
              f"(第{min(generated)}-{max_gen}章)，从第{max_gen + 1}章继续")

        # 恢复最后一章结尾上下文（供下一章 Writer 衔接）
        last = self._load_chapter_full_from_disk(project_dir, max_gen)
        if last:
            fragments = last.get("fragments", [])
            if fragments:
                recent = fragments[-3:]
                self._previous_chapter_ending = "\n".join(
                    (f"{f.get('character', '')}: " if f.get('character') else "")
                    + f.get('text', '')
                    for f in recent
                )
                print(f"[Resume] 从第{max_gen}章恢复结尾上下文 "
                      f"({len(self._previous_chapter_ending)} 字)")

        # 恢复 StoryMemory：章节规划历史 + 伏笔列表
        restored_plans = 0
        restored_threads = 0
        for ch_num in sorted(generated):
            ch_data = self._load_chapter_full_from_disk(project_dir, ch_num)
            if not ch_data:
                continue
            plan_summary = {
                "chapter_number": ch_data.get("chapter_number", ch_num),
                "title": ch_data.get("title", ""),
                "synopsis": ch_data.get("synopsis", ""),
                "characters_involved": (
                    list(ch_data.get("character_beats", {}).keys())
                    if isinstance(ch_data.get("character_beats"), dict) else []
                ),
                "key_events": [
                    s.get("goal", "")
                    for s in ch_data.get("sections", [])
                ],
                "plot_threads_introduced": ch_data.get("plot_threads_introduced", []),
            }
            self._story_memory.add_chapter_plan(plan_summary)
            restored_plans += 1

            for t in ch_data.get("plot_threads_introduced", []):
                self._story_memory.add_thread(t, ch_num)
                restored_threads += 1

        self._chapter_plan_history = self._story_memory.chapter_plans
        self._introduced_threads = self._story_memory.get_pending_threads()
        print(f"[Resume] StoryMemory 已恢复: {restored_plans} 个章节规划, "
              f"{restored_threads} 条伏笔")

    def _parse_review_result(self, raw) -> dict:
        """解析 ReviewEditor 输出：{revised_fragments, changes, overall_score}。"""
        if isinstance(raw, dict):
            return raw
        try:
            text = raw.output if hasattr(raw, 'output') else str(raw)
            return json.loads(text.strip())
        except (json.JSONDecodeError, AttributeError):
            pass
        return {"revised_fragments": [], "changes": [], "overall_score": 0}

    def _find_cached_project(self, base_name: str, expected_chapters: int):
        """在 projects 目录查找已缓存的 novel.json。

        匹配条件: 目录名以 base_name 开头 + novel.json 存在 + 章节数匹配。

        Returns:
            Novel 对象或 None
        """
        from ..core.models import Novel
        projects_dir = getattr(self._services.project, '_projects_dir', '')
        if not projects_dir or not os.path.isdir(projects_dir):
            return None

        candidates = sorted(
            [d for d in os.listdir(projects_dir)
             if d.startswith(base_name)],
            reverse=True,
        )
        for dir_name in candidates:
            novel_path = os.path.join(projects_dir, dir_name, "novel.json")
            if not os.path.exists(novel_path):
                continue
            try:
                with open(novel_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                saved_chapters = len(data.get("chapters", []))
                if saved_chapters != expected_chapters:
                    continue
                novel = Novel.from_dict(data)
                novel.file_path = ""
                novel.output_dir = os.path.join(projects_dir, dir_name)
                if novel.story_graph and novel.story_graph.total_node_count > 0:
                    return novel
            except Exception:
                continue
        return None

    def _load_cached_style(self, project_dir: str) -> Optional[object]:
        """从项目目录加载缓存的文风 Profile。"""
        import os
        from ..distillers.style_profile import AuthorStyleProfile
        cache_path = os.path.join(project_dir, "author_style_profile.json")
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    return AuthorStyleProfile.from_dict(json.load(f))
            except Exception:
                pass
        return None

    def _save_cached_style(self, project_dir: str, profile):
        """缓存文风 Profile 到项目目录。"""
        cache_path = os.path.join(project_dir, "author_style_profile.json")
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(profile.to_dict(), f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _load_cached_char_profiles(self, project_dir: str) -> Optional[dict]:
        """从项目目录加载缓存的角色 Profile。

        Returns:
            {name: CharacterProfile} 或 None（缓存不存在或读取失败）
        """
        from ..distillers.character_profile import CharacterProfile
        cache_path = os.path.join(project_dir, "character_profiles.json")
        if not os.path.exists(cache_path):
            return None
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            profiles = {}
            for name, d in data.items():
                profiles[name] = CharacterProfile.from_dict(d)
            return profiles if profiles else None
        except Exception:
            return None

    def _save_cached_char_profiles(self, project_dir: str, profiles: dict):
        """缓存角色 Profile 到项目目录。"""
        cache_path = os.path.join(project_dir, "character_profiles.json")
        try:
            data = {name: p.to_dict() for name, p in profiles.items()}
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
