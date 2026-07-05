# -*- coding: utf-8 -*-
"""ContinuationPipeline —— 续写流水线编排器。

串联 4 个 Agent:
  ① Plot Architect → 生成大纲
  ② Chapter Writer → 流式写作（核心）
  ③ Consistency Reviewer → 审校（异步，不阻塞前端）
  ④ Revision Editor → 修订（异步，不阻塞前端）

管理 SSE 事件总线，通过 AsyncGenerator 将事件推送到 HTTP handler。
"""

import asyncio
import json
import re
from typing import AsyncGenerator, Optional, TYPE_CHECKING

from .fragment import PipelineEvent, StoryFragment
from .plot_architect import PlotArchitect
from .chapter_writer import ChapterWriter
from .consistency_reviewer import ConsistencyReviewer
from .revision_editor import RevisionEditor

if TYPE_CHECKING:
    from ..context import GlobalContext, ServiceRegistry
    from ..llm import UnifiedLLM


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

        # 4 个 Agent
        self.architect: Optional[PlotArchitect] = None
        self.writer: Optional[ChapterWriter] = None
        self.reviewer: Optional[ConsistencyReviewer] = None
        self.editor: Optional[RevisionEditor] = None

        # 状态
        self._phase: str = "idle"
        self._chapter: int = 0
        self._fragment_count: int = 0

        # 缓存数据
        self._style_profile = None
        self._character_profiles: dict = {}
        self._previous_chapter_ending: str = ""

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
        from ..chapter_parser import parse_novel_chapters
        from ..models import Novel
        from ..services.project_service import ProjectService as PS
        from ..character_distiller import CharacterDistiller
        from .author_style_distiller import AuthorStyleDistiller

        # 1. 加载文本 & 解析章节
        text = PS.read_text_file(novel_path)
        base_name = novel_path.replace("\\", "/").split("/")[-1].rsplit(".", 1)[0]
        chapters = parse_novel_chapters(text, base_name)

        # 2. 创建 Novel → 提取 KG
        project_dir = self._services.project.create_project_dir(base_name)
        self._ctx.novel = Novel(
            title=base_name,
            file_path=novel_path,
            chapters=chapters,
            output_dir=project_dir,
        )

        self._ctx.novel.story_graph = self._services.kg.extract_incremental(
            chapters,
            batch_size=int(__import__('os').getenv("KG_BATCH_SIZE", "10")),
        )
        self._services.project.save_novel(self._ctx.novel)

        graph = self._ctx.novel.story_graph
        self._chapter = len(chapters)

        # 3. 蒸馏文风 Profile
        distiller = AuthorStyleDistiller(self._llm)
        # 检查是否有缓存的 style profile
        cached_style = self._ctx.novel.output_dir and self._load_cached_style(
            self._ctx.novel.output_dir
        )
        if cached_style:
            self._style_profile = cached_style
        else:
            self._style_profile = distiller.distill(text)
            if self._ctx.novel.output_dir:
                self._save_cached_style(self._ctx.novel.output_dir, self._style_profile)

        # 4. 蒸馏主要角色 Profile（importance >= 6）
        char_distiller = CharacterDistiller(self._llm, self._kg)
        persons = self._kg.get_all_persons(graph)
        important = [p for p in persons if p.importance >= 5]
        for person in important[:8]:
            try:
                profile = char_distiller.distill_character(
                    person.name, text, graph,
                )
                self._character_profiles[person.name] = profile
            except Exception:
                pass

        # 5. 提取最后一章结尾
        if chapters:
            last_ch = chapters[-1]
            self._previous_chapter_ending = last_ch.content[-3000:] if len(last_ch.content) > 3000 else last_ch.content

        # 6. 初始化 Agent
        self._init_agents()

    # ── 规则：死亡关键词 ──
    _DEATH_PATTERNS = [
        (re.compile(r'(?:已经|已经)?死了|死去|身亡|丧命|毙命|遇难|殉职|牺牲'), "dead"),
        (re.compile(r'停止[了]?呼吸|没有[了]?呼吸|没了气息|断[了]?气'), "dead"),
        (re.compile(r'确认死亡|当场死亡|抢救无效|宣告死亡'), "dead"),
        (re.compile(r'尸体|遗体|遗容|遗物.{0,10}' + r'(?:被|已|已经)'), "dead"),
        (re.compile(r'杀[死害][了]?' + r'(?:他|她|它|其)'), "dead"),
        (re.compile(r'(?:他|她|其)被.{0,5}杀[死害]'), "dead"),
    ]

    _ARREST_PATTERNS = [
        (re.compile(r'被[逮抓]捕|被[押拘]走|被关[押进]|入狱|锒铛入狱'), "arrested"),
        (re.compile(r'带[上走].{0,5}手铐|戴[上了].{0,5}手铐'), "arrested"),
    ]

    _MISSING_PATTERNS = [
        (re.compile(r'下落不明|不知所踪|音讯全无|人间蒸发|杳无音信'), "missing"),
        (re.compile(r'失踪|失联|找不到.{0,5}' + r'(?:他|她|其)'), "missing"),
    ]

    def _get_character_statuses(self) -> dict:
        """获取角色状态映射。

        策略:
          1. 从 KG 获取 baseline
          2. 对 importance >= 6 的角色，用规则在原文中检测真实状态
             - 正则定位角色出场的章节（纯文本匹配，不需要 KG 索引）
             - 提取最后几章中该角色出现的段落
             - 用死亡/被捕/失踪关键词规则判断状态
          3. 规则检测到的状态覆盖 KG 缓存

        Returns:
            {name: status}
        """
        graph = self._ctx.novel.story_graph if self._ctx.novel else None
        if not graph:
            return {}

        # Layer 1: KG baseline
        persons = self._kg.get_all_persons(graph)
        kg_statuses = {p.name: p.status for p in persons if p.status}

        # Layer 2: 规则检测 — 对重要角色回到原文验证
        important = [p for p in persons if p.importance >= 6]
        if not important:
            return {n: s for n, s in kg_statuses.items() if s and s != "active"}

        novel_text = self._get_novel_text()
        if not novel_text:
            return {n: s for n, s in kg_statuses.items() if s and s != "active"}

        # 按章节切分原文
        chapters = self._split_novel_by_chapter(novel_text)
        if not chapters:
            return {n: s for n, s in kg_statuses.items() if s and s != "active"}

        for person in important:
            name = person.name
            # 规则 1: 找到该角色最后出现的几章
            appeared = self._find_chapters_by_name(name, chapters)
            if not appeared:
                continue

            # 取最后 3 章出场的文本
            last_chapters = sorted(appeared)[-3:]
            last_text = "\n".join(
                chapters.get(ch, "") for ch in last_chapters
            )

            # 规则 2: 提取角色周围的上下文段落
            context = self._extract_name_context(name, last_text)

            # 规则 3: 用关键词判断状态
            detected = self._detect_status_by_rules(name, context)

            if detected:
                old = kg_statuses.get(name, "active")
                if detected != old:
                    print(
                        f"  [KG Fix] {name}: {old} → {detected} "
                        f"(章节 {last_chapters[-1]})"
                    )
                kg_statuses[name] = detected
                # 同时修正 KG 中的 person 节点
                person._status = detected

        # 只返回 non-active
        return {n: s for n, s in kg_statuses.items() if s and s != "active"}

    def _get_novel_text(self) -> str:
        """获取小说全文文本。"""
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

    @classmethod
    def _detect_status_by_rules(cls, name: str, context: str) -> str:
        """用规则关键词判断角色状态。

        优先级: dead > arrested > missing
        只在角色名附近（前后 50 字）出现关键词时才触发，
        避免把"他杀了别人"误判为该角色死亡。

        Returns:
            检测到的 status，或空字符串（未检测到）
        """
        # 提取所有出现位置的附近文本（更精确）
        nearby_contexts = []
        idx = 0
        while True:
            idx = context.find(name, idx)
            if idx == -1:
                break
            start = max(0, idx - 80)
            end = min(len(context), idx + 120)
            nearby_contexts.append(context[start:end])
            idx += len(name)

        nearby_text = " ".join(nearby_contexts) if nearby_contexts else context

        # 死亡检测
        for pattern, status in cls._DEATH_PATTERNS:
            matches = pattern.findall(nearby_text)
            if matches:
                return status

        # 被捕检测
        for pattern, status in cls._ARREST_PATTERNS:
            if pattern.search(nearby_text):
                return status

        # 失踪检测
        for pattern, status in cls._MISSING_PATTERNS:
            if pattern.search(nearby_text):
                return status

        return ""


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
        """初始化 4 个 Agent。"""
        self.architect = PlotArchitect(self._ctx, self._services, self._llm)
        self.writer = ChapterWriter(self._ctx, self._services, self._llm)
        self.reviewer = ConsistencyReviewer(self._ctx, self._services, self._llm)
        self.editor = RevisionEditor(self._ctx, self._services, self._llm)

    async def run(self, instruction: str = "") -> AsyncGenerator[PipelineEvent, None]:
        """运行续写流水线。

        Args:
            instruction: 用户初始指令（可选）

        Yields:
            PipelineEvent: 每个事件通过 SSE 推送
        """
        if not self.architect or not self.writer:
            raise RuntimeError("请先调用 load_novel() 加载小说")

        # —— 阶段 1: 大纲 ——
        self._phase = "planning"
        yield PipelineEvent("phase", {"phase": "planning"})

        self.architect.set_context(
            previous_chapter_ending=self._previous_chapter_ending,
            style_profile=self._style_profile,
            character_profiles=self._character_profiles,
            last_chapter=self._chapter,
            user_instruction=instruction,
            character_statuses=self._get_character_statuses(),
        )

        architect_result_raw = await self.architect.run(
            f"为第 {self._chapter + 1} 章规划大纲"
        )

        # 解析 Plot Architect 的输出（可能是 ReAct 的自然文本终止）
        outline = self._parse_outline(architect_result_raw)
        yield PipelineEvent("outline", outline)

        # —— 阶段 2: 写作 ——
        self._phase = "writing"
        yield PipelineEvent("phase", {"phase": "writing"})

        self.writer.set_context(
            outline=outline,
            style_profile=self._style_profile,
            previous_chapter_ending=self._previous_chapter_ending,
            character_profiles=self._character_profiles,
            character_statuses=self._get_character_statuses(),
        )

        draft_fragments = []
        async for fragment in self.writer.stream(outline):
            draft_fragments.append(fragment)
            self._fragment_count += 1
            yield PipelineEvent("fragment", fragment.to_dict())

        # —— 阶段 3: 审校 ——
        self._phase = "reviewing"
        yield PipelineEvent("phase", {"phase": "reviewing"})

        self.reviewer.set_context(
            draft_fragments=draft_fragments,
            character_profiles=self._character_profiles,
            style_profile=self._style_profile,
        )
        review_result_raw = await self.reviewer.run("审校草稿")
        issues = self._parse_review(review_result_raw)
        yield PipelineEvent("review", issues)

        # —— 阶段 4: 修订 ——
        self._phase = "revising"
        yield PipelineEvent("phase", {"phase": "revising"})

        if issues.get("issues"):
            revision_input = json.dumps({
                "draft": [f.to_dict() for f in draft_fragments],
                "issues": issues["issues"],
            })
            revision_result_raw = await self.editor.run(revision_input)
            revised = self._parse_revision(revision_result_raw)
            yield PipelineEvent("complete", revised)
        else:
            yield PipelineEvent("complete", {
                "fragments": [f.to_dict() for f in draft_fragments],
                "changes": [],
            })

        # 完成
        self._phase = "idle"
        yield PipelineEvent("done", {})

    async def inject(self, instruction: str):
        """接收用户注入指令，转发到 Chapter Writer。

        Args:
            instruction: 用户自然语言指令
        """
        if self.writer:
            await self.writer.inject(instruction)

    def _parse_outline(self, raw: str) -> dict:
        """从 Plot Architect 的 ReAct 输出中解析大纲。"""
        # AgentFlow 自然终止时返回的是 LLM 输出的文本
        # 可能是纯 JSON 或包含 JSON 的文本
        if isinstance(raw, dict):
            return raw

        text = str(raw).strip()
        # 尝试直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 尝试从文本中提取 JSON 块
        import re
        json_match = re.search(r'\{.*"chapter_number".*\}', text, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                pass

        # Fallback
        return {
            "chapter_number": self._chapter + 1,
            "title": "",
            "synopsis": "继续推进故事",
            "structure": {},
            "tone": "保持原作风格",
            "status": "parsed_fallback",
        }

    def _parse_review(self, raw: str) -> dict:
        """从 Reviewer 输出中解析问题列表。"""
        if isinstance(raw, dict):
            return raw
        try:
            return json.loads(str(raw).strip())
        except json.JSONDecodeError:
            pass
        return {"issues": [], "overall_score": 0}

    def _parse_revision(self, raw: str) -> dict:
        """从 Editor 输出中解析修订结果。"""
        if isinstance(raw, dict):
            return raw
        try:
            return json.loads(str(raw).strip())
        except json.JSONDecodeError:
            pass
        return {"revised_fragments": [], "changes": [], "status": "parse_failed"}

    def _load_cached_style(self, project_dir: str) -> Optional[object]:
        """从项目目录加载缓存的文风 Profile。"""
        import os
        from .author_style_profile import AuthorStyleProfile
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
        import os
        cache_path = os.path.join(project_dir, "author_style_profile.json")
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(profile.to_dict(), f, ensure_ascii=False, indent=2)
        except Exception:
            pass
