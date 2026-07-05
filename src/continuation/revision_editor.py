# -*- coding: utf-8 -*-
"""RevisionEditor —— 修订编辑 Agent。

根据审校问题列表对草稿做局部修订。只修改有问题的 fragment，不重写整章。
"""

import json
import logging
from typing import TYPE_CHECKING

from ..agents.base_agent import BaseAgent
from .fragment import StoryFragment

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..context import GlobalContext, ServiceRegistry
    from ..llm import UnifiedLLM


class RevisionEditor(BaseAgent):
    """修订编辑 Agent。

    根据审校结果做局部修订。不走 ReAct，直接用 LLM 做针对性修改。
    """

    SKILL_NAME = "revision_editor"

    def __init__(self, ctx, services, llm, memory=None):
        super().__init__(ctx, services, llm, memory)
        self._llm_client = llm

    async def run(self, task: str = ""):
        """修订草稿。

        task 格式: JSON 字符串 {"draft": [...], "issues": [...]}
        """
        try:
            data = json.loads(task) if isinstance(task, str) else task
        except json.JSONDecodeError:
            return json.dumps({"revised_fragments": [], "changes": [],
                               "error": "Invalid input"}, ensure_ascii=False)

        draft = data.get("draft", [])
        issues = data.get("issues", [])

        if not issues:
            # 没有问题，直接返回原稿
            return json.dumps({
                "revised_fragments": draft,
                "changes": [],
                "status": "ok",
            }, ensure_ascii=False)

        # 将 fragment 转换为文本格式
        draft_text = "\n".join(
            f"[{i}] {{{f.get('type', '?')}}} "
            + (f"{f.get('character', '')}: " if f.get('character') else "")
            + f.get('text', '')
            for i, f in enumerate(draft)
        )

        try:
            result = self._llm_client.chat_json(
                system_prompt=(
                    "你是专业的修订编辑。根据问题列表修改草稿中的对应片段。"
                    "只修改有问题的 fragment，其他地方保持不变。"
                    "返回整个 fragment 列表（含未修改的）。只返回 JSON。"
                ),
                user_prompt=(
                    f"## 原草稿\n{draft_text[:5000]}\n\n"
                    f"## 问题列表\n{json.dumps(issues, ensure_ascii=False, indent=2)}\n\n"
                    f"返回 JSON:\n"
                    f'{{"revised_fragments": [{{"type": "...", "text": "...", '
                    f'"character": "..."}}], '
                    f'"changes": [{{"fragment_index": 0, "original": "...", '
                    f'"revised": "...", "reason": "..."}}]}}'
                ),
                temperature=0.3,
                max_tokens=4096,
            )
            if isinstance(result, dict):
                return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            logger.warning("RevisionEditor LLM 调用失败: %s", e)

        return json.dumps({
            "revised_fragments": draft,
            "changes": [],
            "status": "revision_failed",
        }, ensure_ascii=False)
