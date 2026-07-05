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
from typing import AsyncGenerator, Optional, TYPE_CHECKING

from .fragment import PipelineEvent, StoryFragment
from .plot_architect import PlotArchitect
from .chapter_writer import ChapterWriter
from .consistency_reviewer import ConsistencyReviewer
from .revision_editor import RevisionEditor
from .character_status_resolver import CharacterStatusResolver

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

    def _get_character_statuses(self) -> dict:
        """获取角色状态映射。

        两层策略:
          1. 从 KG 获取 baseline（快速但有误差）
          2. 对 importance >= 6 的关键角色，用 CharacterStatusResolver
             回到原文现场分析状态（准确性更高）
          3. Resolver 结果优先于 KG 缓存

        Returns:
            {name: status}
        """
        graph = self._ctx.novel.story_graph if self._ctx.novel else None
        if not graph:
            return {}

        # Layer 1: KG baseline
        persons = self._kg.get_all_persons(graph)
        kg_statuses = {p.name: p.status for p in persons if p.status}

        # Layer 2: Progressive resolution for important characters
        important = [p for p in persons if p.importance >= 6]
        if important:
            novel_text = self._get_novel_text()
            if novel_text:
                resolver = CharacterStatusResolver(self._llm)
                for person in important:
                    try:
                        resolved = resolver.resolve(
                            person.name, novel_text, graph,
                        )
                        if resolved.get("confidence") in ("high", "medium"):
                            new_status = resolved.get("status", "")
                            old_status = kg_statuses.get(person.name, "")
                            if new_status and new_status != old_status:
                                print(
                                    f"  [StatusResolver] {person.name}: "
                                    f"KG={old_status} → Resolved={new_status} "
                                    f"({resolved.get('confidence')})"
                                )
                            kg_statuses[person.name] = new_status
                    except Exception:
                        pass  # 单个角色解析失败不影响整体

        # 只返回 non-active 的状态（active 是默认，不需要约束）
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
