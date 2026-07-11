# -*- coding: utf-8 -*-
"""统一 LLM 封装。

提供:
  - chat_json(): 返回解析后的 dict（工具内部用）
  - chat():      返回纯文本（摘要、压缩等非结构化输出）
  - token 使用量追踪
"""

import json
from typing import Optional


class UnifiedLLM:
    """统一 LLM 入口 —— 工具内部的 LLM 调用。

    封装两种调用模式:
      - chat_json(): 强约束 JSON 输出 → dict
      - chat():      自由文本输出 → str

    同时追踪 token 使用量。
    """

    def __init__(self, client, model: str = ""):
        """初始化。

        Args:
            client: openai.OpenAI 同步客户端
            model: 模型名称（如 deepseek-chat）
        """
        self._client = client
        self._model = model
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    # ── JSON 模式 ──

    def chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        timeout: int = 120,
    ) -> dict:
        """调用 LLM 并返回解析后的 JSON 字典。

        自动附加 JSON-only 指令，去除 markdown 围栏标记。

        Raises:
            json.JSONDecodeError: LLM 返回的不是有效 JSON
        """
        text = self._call(
            system_prompt=system_prompt
            + "\n\nYou MUST respond with valid JSON only. No markdown fences, no explanation.",
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )

        # 去除 markdown 代码围栏
        if text.startswith("```"):
            lines = text.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)

        return json.loads(text)

    # ── 文本模式 ──

    def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.5,
        max_tokens: int = 2048,
        timeout: int = 120,
    ) -> str:
        """调用 LLM 并返回纯文本（用于摘要、翻译等非结构化输出）。

        Returns:
            LLM 返回的文本内容
        """
        return self._call(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )

    # ── 内部 ──

    def _call(self, system_prompt: str, user_prompt: str,
              temperature: float, max_tokens: int, timeout: int) -> str:
        """底层同步调用。追踪 token 使用量。"""
        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            timeout=timeout,
            max_tokens=max_tokens,
        )

        if hasattr(response, "usage") and response.usage:
            self.total_prompt_tokens += response.usage.prompt_tokens or 0
            self.total_completion_tokens += response.usage.completion_tokens or 0

        return (response.choices[0].message.content or "").strip()

    # ── 属性 ──

    @property
    def model(self) -> str:
        return self._model

    @property
    def sync_client(self):
        """暴露底层同步客户端（用于 KG 提取等需要 raw client 的场景）。"""
        return self._client

    @property
    def token_usage(self) -> dict:
        return {
            "prompt_tokens": self.total_prompt_tokens,
            "completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_prompt_tokens + self.total_completion_tokens,
        }
