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

        # 角色状态验证标记：已验证过的角色不再重复验证
        self._status_verified: set = set()

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
        """获取角色状态映射（KG baseline + 已验证角色的覆盖值）。

        不做主动验证——验证由 _verify_characters_in_text() 按需触发。
        只返回 non-active 状态（active 是默认，不需要约束提示）。

        Returns:
            {name: status}
        """
        graph = self._ctx.novel.story_graph if self._ctx.novel else None
        if not graph:
            return {}
        persons = self._kg.get_all_persons(graph)
        return {p.name: p.status for p in persons
                if p.status and p.status != "active"}

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
            if p.name in text and p.name not in self._status_verified:
                mentioned.append(p)

        if not mentioned:
            return

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
                self._status_verified.add(name)  # 未找到也标记，避免反复扫
                continue

            # 取最后 3 章的文本 + 角色名周围上下文
            last_chapters = sorted(appeared)[-3:]
            last_text = "\n".join(chapters.get(ch, "") for ch in last_chapters)
            context = self._extract_name_context(name, last_text)

            # LLM 现场分析
            resolved = self._llm_resolve_status(name, context, last_chapters[-1])

            # 标记已验证
            self._status_verified.add(name)

            if resolved:
                old = person.status
                if resolved != old:
                    print(
                        f"  [KG Fix] {name}: {old} → {resolved} "
                        f"(章节 {last_chapters[-1]})"
                    )
                person._status = resolved  # 直接修正 KG

    def _llm_resolve_status(self, name: str, context: str,
                            last_chapter: int) -> str:
        """LLM 分析角色当前状态。

        基于该角色最后几次出场的原文场景，判断其当前状态。
        只调用 1 次 LLM，传入已提取好（规则定位）的场景文本。

        Args:
            name: 角色名
            context: 该角色最后几次出场的上下文文本（已由规则提取）
            last_chapter: 最后出场章节

        Returns:
            状态字符串（dead/active/missing/arrested），或空字符串（无法确定）
        """
        if not context or len(context) < 20:
            return ""

        prompt = f"""你是小说分析员。根据角色最后几次出场的原文片段，判断该角色当前状态。

角色: {name}
最后出场章节: 第{last_chapter}章

原文场景:
{context[:3000]}

该角色当前是:
- dead: 原文明确描写了死亡（如"停止了呼吸""确认死亡""尸体"等）
- arrested: 被逮捕/关押
- missing: 失踪/下落不明
- active: 还活着，正常活动

返回 JSON: {{"status": "dead|active|missing|arrested", "evidence": "原文关键句（证明该状态的一句原文引用）"}}
只返回 JSON。"""

        try:
            result = self._llm.chat_json(
                system_prompt="你是专业小说分析员。只返回 JSON，不返回其他内容。",
                user_prompt=prompt,
                temperature=0.2,
                max_tokens=512,
            )
            if isinstance(result, dict):
                status = result.get("status", "")
                if status in ("dead", "arrested", "missing", "active"):
                    return status
        except Exception:
            pass

        return ""

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
        # 按需验证：大纲中涉及的角色做现场状态校验
        outline_text = json.dumps(outline, ensure_ascii=False)
        self._verify_characters_in_text(outline_text)
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
        # 按需验证：用户指令中可能提到新角色
        self._verify_characters_in_text(instruction)
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
