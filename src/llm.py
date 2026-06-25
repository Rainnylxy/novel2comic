# -*- coding: utf-8 -*-
"""统一 LLM 封装。

提取 agent.py 中的 _llm_chat_json 逻辑，
提供 JSON 模式强制、错误重试、token 使用量追踪。
"""

import json
from typing import Optional


class UnifiedLLM:
    """统一 LLM 调用（工具内部的 JSON 调用）。

    封装原先 agent.py 中 _llm_chat_json 的逻辑：
    - JSON 模式强制（去除 ``` 围栏标记）
    - 稳定的 temperature 默认值
    - token 使用量追踪
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

    def chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        timeout: int = 120,
    ) -> dict:
        """调用 LLM 并返回解析后的 JSON 字典。

        自动附加 JSON-only 指令，去除 markdown 围栏标记，
        解析 JSON 并追踪 token 使用量。

        Args:
            system_prompt: 系统提示词
            user_prompt: 用户提示词
            temperature: LLM 温度参数
            max_tokens: 最大输出 token 数
            timeout: 超时时间（秒）

        Returns:
            解析后的 JSON 字典

        Raises:
            json.JSONDecodeError: LLM 返回的不是有效 JSON
        """
        full_system = (
            system_prompt
            + "\n\nYou MUST respond with valid JSON only. No markdown fences, no explanation."
        )

        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": full_system},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            timeout=timeout,
            max_tokens=max_tokens,
        )

        # 追踪 token 使用量
        if hasattr(response, "usage") and response.usage:
            self.total_prompt_tokens += response.usage.prompt_tokens or 0
            self.total_completion_tokens += response.usage.completion_tokens or 0

        text = response.choices[0].message.content or ""
        text = text.strip()

        # 去除 markdown 代码围栏
        if text.startswith("```"):
            lines = text.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines)

        return json.loads(text)

    @property
    def model(self) -> str:
        return self._model

    @property
    def token_usage(self) -> dict:
        return {
            "prompt_tokens": self.total_prompt_tokens,
            "completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_prompt_tokens + self.total_completion_tokens,
        }
