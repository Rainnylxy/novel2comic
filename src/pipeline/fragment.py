# -*- coding: utf-8 -*-
"""StoryFragment —— 续写流式输出的基本单位。

5 种片段类型对应不同的前端渲染方式:
  - dialogue:     角色对话 → 聊天气泡
  - narration:    第三人称旁白 → 居中灰字卡片
  - action:       角色动作 → 附属小字
  - inner_thought: 角色内心独白 → 虚线气泡
  - divider:      场景分隔 → 水平分割线
"""

import json
from dataclasses import dataclass, asdict
from typing import Optional, Literal

FragmentType = Literal["dialogue", "narration", "action", "inner_thought", "divider"]


@dataclass
class StoryFragment:
    """续写流的最小输出单元。

    Attributes:
        type: 片段类型，决定前端渲染方式
        text: 文本内容
        character: 角色名（dialogue / action / inner_thought 时必填）
        divider_label: 场景分隔标签（divider 时可选，如 "三小时后"）
    """

    type: FragmentType
    text: str
    character: Optional[str] = None
    divider_label: Optional[str] = None

    def to_dict(self) -> dict:
        """序列化为字典（用于 JSON → SSE 推送）。"""
        d = {"type": self.type, "text": self.text}
        if self.character:
            d["character"] = self.character
        if self.divider_label:
            d["divider_label"] = self.divider_label
        return d

    def to_sse(self) -> str:
        """序列化为单行 JSON（SSE data 字段）。

        Returns:
            不会包含换行符的紧凑 JSON 字符串
        """
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict) -> "StoryFragment":
        """从字典反序列化。"""
        return cls(
            type=d.get("type", "narration"),
            text=d.get("text", ""),
            character=d.get("character"),
            divider_label=d.get("divider_label"),
        )

    @classmethod
    def from_json(cls, json_str: str) -> "StoryFragment":
        """从 JSON 字符串反序列化。

        Raises:
            json.JSONDecodeError: 输入不是有效 JSON
        """
        return cls.from_dict(json.loads(json_str))

    @staticmethod
    def parse_stream_line(line: str) -> Optional["StoryFragment"]:
        """尝试将 LLM streaming 的一行输出解析为 StoryFragment。

        容错处理：去除首尾空白、跳过空行、跳过非 JSON 行。
        对于不完整的 JSON 行返回 None（由调用方缓冲拼接）。

        Args:
            line: LLM streaming 输出的一行文本

        Returns:
            StoryFragment 或 None（该行不是有效 fragment）
        """
        line = line.strip()
        if not line:
            return None
        # 跳过非 JSON 行（LLM 偶尔输出的解释文本）
        if not line.startswith("{"):
            return None
        try:
            return StoryFragment.from_json(line)
        except json.JSONDecodeError:
            # 不完整 JSON → 返回 None，由上层 buffer 处理
            return None


@dataclass
class PipelineEvent:
    """流水线事件 —— 用于 SSE 推送。

    事件类型:
      - "phase": 阶段切换 (data = {"phase": "planning"|"writing"|"reviewing"|"revising"})
      - "outline": Plot Architect 生成的章节大纲
      - "fragment": Chapter Writer 产出的 StoryFragment
      - "review": Consistency Reviewer 的审校结果
      - "complete": 流水线完成 (data = {"fragments": [...]} 或修订后结果)
      - "error": 错误 (data = {"message": "..."})
      - "done": 流结束标记
    """

    event_type: str
    data: dict = None

    def __post_init__(self):
        if self.data is None:
            self.data = {}

    def to_sse(self) -> str:
        """格式化为 SSE 协议文本。

        Returns:
            包含 event + data 的 SSE 消息，以双换行结尾
        """
        lines = [f"event: {self.event_type}"]
        data_str = json.dumps(self.data, ensure_ascii=False)
        lines.append(f"data: {data_str}")
        lines.append("")  # SSE 要求空行分隔
        return "\n".join(lines) + "\n"
