# -*- coding: utf-8 -*-
"""ChapterWriter —— 章节写手（流式核心）。

不走 AgentFlow ReAct 循环。直接使用 httpx 异步流式请求 LLM API，
逐行解析输出为 StoryFragment，通过 async generator yield。

流式控制:
  - 正常流: 构建 prompt → stream LLM → 逐行解析 → yield StoryFragment
  - 注入中断: 收到 inject signal → abort 当前 stream → 拼接上下文 → 重新 stream
"""

import asyncio
import json
import os
from typing import AsyncGenerator, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..context import GlobalContext, ServiceRegistry
    from ..llm import UnifiedLLM


class ChapterWriter:
    """章节写手 —— 流式续写核心。

    不走 AgentFlow，通过 httpx 直接调用 LLM streaming API。
    以 StoryFragment 为单位逐条输出。

    用法:
        writer = ChapterWriter(ctx, services, llm)
        async for fragment in writer.stream(outline):
            send_sse(fragment)
    """

    def __init__(self, ctx: "GlobalContext", services: "ServiceRegistry",
                 llm: "UnifiedLLM"):
        self._ctx = ctx
        self._services = services
        self._llm = llm
        self._kg = services.kg

        # 注入控制
        self._inject_event = asyncio.Event()
        self._inject_instruction: str = ""
        self._aborted = False

        # 已生成的 fragments（用于 inject 重建上下文）
        self._generated_fragments: list = []

        # 运行时上下文
        self._outline: dict = {}
        self._style_profile = None
        self._previous_chapter_ending: str = ""
        self._character_profiles: dict = {}
        self._character_statuses: dict = {}  # {name: status}  from KG

    def set_context(
        self,
        outline: dict,
        style_profile,
        previous_chapter_ending: str,
        character_profiles: dict,
        character_statuses: dict = None,
    ):
        """设置 Writer 运行时上下文。"""
        self._outline = outline
        self._style_profile = style_profile
        self._previous_chapter_ending = previous_chapter_ending
        self._character_profiles = character_profiles
        self._character_statuses = character_statuses or {}

    async def inject(self, instruction: str):
        """注入用户指令。触发当前流中断并重连。

        Args:
            instruction: 用户的自然语言指令
        """
        self._inject_instruction = instruction
        self._inject_event.set()
        self._aborted = True

    async def stream(self, outline: dict) -> AsyncGenerator["StoryFragment", None]:
        """流式生成章节内容。

        每次 yield 一个 StoryFragment。收到 inject signal 后
        abort 当前请求、拼接上下文、重新 stream。

        Args:
            outline: Plot Architect 生成的章节大纲

        Yields:
            StoryFragment: 逐个片段
        """
        from .fragment import StoryFragment

        # 首次生成
        async for fragment in self._do_stream(outline, ""):
            yield fragment

        # 处理注入循环
        while self._aborted:
            self._aborted = False
            instruction = self._inject_instruction
            self._inject_instruction = ""
            self._inject_event.clear()

            # 用已生成的文本 + 新指令作为上下文重新 stream
            continuation_context = self._build_continuation_context(instruction)
            async for fragment in self._do_stream(outline, continuation_context):
                yield fragment

    async def _do_stream(
        self, outline: dict, extra_context: str
    ) -> AsyncGenerator["StoryFragment", None]:
        """执行一次 streaming 请求。

        Args:
            outline: 章节大纲
            extra_context: 额外上下文（注入指令 + 已生成文本）
        """
        from .fragment import StoryFragment

        # 构建消息
        system_prompt = self._build_writer_system_prompt()
        user_prompt = self._build_writer_user_prompt(outline, extra_context)

        # 调用 LLM streaming API
        # 使用 httpx 异步流式请求
        import httpx

        api_key = os.getenv("AGENTFLOW_API_KEY", "")
        base_url = os.getenv("AGENTFLOW_BASE_URL", "https://api.deepseek.com/")
        model = os.getenv("AGENTFLOW_MODEL", "deepseek-v4-pro")
        proxy = os.getenv("AGENTFLOW_PROXY", "")

        url = f"{base_url.rstrip('/')}/chat/completions"

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.7,
            "max_tokens": 4096,
            "stream": True,
        }

        line_buffer = ""

        client_kwargs = {"timeout": httpx.Timeout(300.0, connect=30.0)}
        if proxy:
            client_kwargs["proxy"] = proxy

        async with httpx.AsyncClient(**client_kwargs) as client:
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                if response.status_code != 200:
                    error_text = await response.aread()
                    raise RuntimeError(f"LLM API error {response.status_code}: {error_text.decode()[:500]}")
                async for line in response.aiter_lines():
                    # 检查是否需要 abort
                    if self._aborted:
                        # 不等待 response.close()，直接 break
                        break

                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]  # 去掉 "data: " 前缀
                    if data_str == "[DONE]":
                        break

                    try:
                        chunk = json.loads(data_str)
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content", "")
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue

                    if not content:
                        continue

                    line_buffer += content

                    # 按换行拆分，尝试解析完整 fragment
                    while "\n" in line_buffer:
                        line_part, line_buffer = line_buffer.split("\n", 1)
                        fragment = StoryFragment.parse_stream_line(line_part)
                        if fragment:
                            self._generated_fragments.append(fragment)
                            yield fragment

                # 处理 buffer 中剩余的文本
                if line_buffer.strip() and not self._aborted:
                    fragment = StoryFragment.parse_stream_line(line_buffer.strip())
                    if fragment:
                        self._generated_fragments.append(fragment)
                        yield fragment

    def _build_continuation_context(self, instruction: str) -> str:
        """构建注入后重连的上下文。"""
        parts = [f"[用户指令] {instruction}\n"]
        parts.append("[已生成内容] 请从以下内容的结尾处自然衔接继续写:\n")

        # 取最后 10 个 fragment 作为上下文
        recent = self._generated_fragments[-10:] if len(self._generated_fragments) > 10 else self._generated_fragments
        for f in recent:
            if f.character:
                parts.append(f"[{f.type}] {f.character}: {f.text}")
            else:
                parts.append(f"[{f.type}] {f.text}")

        parts.append("\n继续写（不要重复上面已有的内容，从下一个自然段开始）:")
        return "\n".join(parts)

    def _build_writer_system_prompt(self) -> str:
        """构建 Chapter Writer 的 system prompt。"""
        parts = [
            "## 角色",
            "你是专业小说续写者。你需要根据大纲、文风约束和角色设定，以结构化片段格式续写小说内容。",
            "",
            "## 输出格式",
            "严格以 StoryFragment JSON 格式逐行输出，每行一个完整的 JSON 对象:",
            "",
            '  {"type": "narration", "text": "旁白/叙述文本..."}',
            '  {"type": "dialogue", "character": "角色名", "text": "对话内容..."}',
            '  {"type": "action", "character": "角色名", "text": "动作描写..."}',
            '  {"type": "inner_thought", "character": "角色名", "text": "内心独白..."}',
            '  {"type": "divider", "text": "", "divider_label": "时间/地点标签"}',
            "",
            "## 规则",
            "1. 每行一个完整的 JSON，行末不要有逗号",
            "2. dialogue 和 inner_thought 的 text 中不要包含引号",
            "3. action 是小字附加在角色名下，text 要简短（<30字）",
            "4. narration 用于场景描写和第三人称旁白",
            "5. 对话和动作交替推进故事，不要连续输出太长的 narration",
            "6. 保持原作叙事风格和角色性格一致性",
            "7. 不要输出 JSON 以外的任何内容（不要解释、不要评论）",
        ]

        # 注入文风约束
        if self._style_profile:
            parts.append("\n" + self._style_profile.summary())
            exemplars_text = self._style_profile.exemplars_text()
            if exemplars_text:
                parts.append("\n" + exemplars_text)

        # 注入角色生死状态（硬约束）
        if self._character_statuses:
            dead_chars = [n for n, s in self._character_statuses.items()
                          if s in ("dead", "deceased", "killed")]
            missing_chars = [n for n, s in self._character_statuses.items()
                             if s == "missing"]
            if dead_chars:
                parts.append("\n## ⚠️ 角色生死状态（绝对约束，违反即为严重错误）")
                parts.append(f"以下角色**已经死亡**，绝不能以存活状态出现在续写中: {', '.join(dead_chars)}")
                parts.append("已死亡角色只能以回忆、闪回、幻觉、他人提及的方式出现。不能让已死亡角色说话、行动或参与任何当前时间线的事件。")
            if missing_chars:
                parts.append(f"以下角色**下落不明**: {', '.join(missing_chars)}")
                parts.append("下落不明角色不能直接出现，只能通过线索或他人转述提及。")

        # 注入角色约束
        if self._character_profiles:
            parts.append("\n## 角色行为约束")
            for name, profile in self._character_profiles.items():
                parts.append(f"\n### {name}")
                if hasattr(profile, 'voice') and profile.voice:
                    v = profile.voice
                    if v.summary:
                        parts.append(f"- Voice: {v.summary}")
                    if v.taboo_words:
                        parts.append(f"- 禁用词: {', '.join(v.taboo_words)}")
                if hasattr(profile, 'boundary') and profile.boundary:
                    b = profile.boundary
                    if b.hard_rules:
                        parts.append(f"- 硬底线: {', '.join(b.hard_rules)}")
                if hasattr(profile, 'policy_anchors') and profile.policy_anchors:
                    anchors = profile.policy_anchors
                    if anchors:
                        parts.append("- 行为参考:")
                        for a in anchors[:3]:
                            if hasattr(a, 'situation') and hasattr(a, 'action'):
                                parts.append(f"  - {a.situation} → {a.action}")

        return "\n".join(parts)

    def _build_writer_user_prompt(self, outline: dict, extra_context: str = "") -> str:
        """构建 user prompt（含大纲 + 前一章结尾 + 额外上下文）。"""
        parts = [
            "## 写作文本",
            f"章节: 第 {outline.get('chapter_number', '?')} 章「{outline.get('title', '')}」",
            f"梗概: {outline.get('synopsis', '')}",
        ]

        structure = outline.get("structure", {})
        if structure:
            parts.append(f"开篇: {structure.get('opening', '')}")
            parts.append(f"推进: {structure.get('rising', '')}")
            parts.append(f"高潮: {structure.get('climax', '')}")
            parts.append(f"钩子: {structure.get('hook', '')}")

        plot_advanced = outline.get("plot_threads_advanced", [])
        if plot_advanced:
            parts.append(f"推进伏笔: {', '.join(plot_advanced)}")

        if self._previous_chapter_ending:
            ending = self._previous_chapter_ending
            parts.append(f"\n## 前一章结尾\n{ending[-2000:] if len(ending) > 2000 else ending}")

        if extra_context:
            parts.append(f"\n{extra_context}")

        parts.append("\n现在从上一章结尾的自然衔接点开始续写。直接输出 StoryFragment JSON 序列。")
        return "\n".join(parts)
