# -*- coding: utf-8 -*-
"""StoryFragment + PipelineEvent —— 前端渲染契约 + SSE 事件封装。

Fragmentizer 负责 prose → StoryFragment，Writer 只输出自然段落。
"""

import json
from dataclasses import dataclass
from typing import Optional, Literal


@dataclass
class StoryFragment:
    """前端渲染的最小单元。

    type: 决定渲染方式（聊天气泡 / 旁白卡片 / 动作小字 / 虚线气泡 / 分割线）
    """

    type: Literal["dialogue", "narration", "action", "inner_thought", "divider"]
    text: str
    character: Optional[str] = None
    divider_label: Optional[str] = None

    def to_dict(self) -> dict:
        d = {"type": self.type, "text": self.text}
        if self.character:
            d["character"] = self.character
        if self.divider_label:
            d["divider_label"] = self.divider_label
        return d


@dataclass
class PipelineEvent:
    """SSE 事件 —— 前端流式消费。"""

    event_type: str
    data: dict = None

    def __post_init__(self):
        if self.data is None:
            self.data = {}

    def to_sse(self) -> str:
        lines = [f"event: {self.event_type}"]
        data_str = json.dumps(self.data, ensure_ascii=False)
        lines.append(f"data: {data_str}")
        lines.append("")
        return "\n".join(lines) + "\n"
