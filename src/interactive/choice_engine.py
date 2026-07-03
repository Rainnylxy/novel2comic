# -*- coding: utf-8 -*-
"""ChoiceEngine —— 普通抉择生成。

职责:
- 判断是否应该触发抉择（间隔 + 张力检测）
- 调用 LLM 生成 2-3 个选项
- 格式化抉择展示
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..llm import UnifiedLLM
    from .story_state import StoryState


# 抉择生成 Prompt
CHOICE_GENERATION_PROMPT = """你是互动小说的编剧。当前场景中出现了戏剧性时刻，用户需要做出选择。

## 用户角色
{user_summary}

## 当前场景
第 {chapter} 章 | {location} | 在场: {present_characters}

## 当前亲密度
{intimacy_summary}

## 已触发的剧情旗标
{flags_summary}

## 最近对话
{recent_dialogue}

## 任务
基于以上信息，生成 2-3 个用户可以做的行为选择。

要求:
1. 每个选择必须符合用户角色的身份
2. 选项之间应有不同的社交策略（谨慎/冒险/坦诚/隐瞒/追问/沉默）
3. 亲密度变化的幅度应合理（±5~15），不能过度
4. 至少一个选项有明显风险
5. 选项描述用第一人称，简短有力（15字以内）

返回严格 JSON:
{{
  "moment": "当前戏剧性时刻的一句话描述",
  "choices": [
    {{
      "text": "选项描述",
      "intimacy_changes": {{"角色名": ±5~15}},
      "risk": "low|medium|high",
      "strategy": "谨慎|冒险|坦诚|隐瞒|追问|沉默"
    }}
  ]
}}
"""


class ChoiceEngine:
    """普通抉择生成引擎。

    用法:
        engine = ChoiceEngine(llm)
        choice = engine.generate(story_state, recent_dialogue, scene_context)
    """

    # 两次抉择的最小间隔轮数
    MIN_INTERVAL = 5

    def __init__(self, llm: "UnifiedLLM"):
        self._llm = llm

    def should_trigger(self, state: "StoryState") -> bool:
        """判断是否应该触发普通抉择。"""
        if state.total_turns == 0:
            return False
        since_last = state.total_turns - state.last_choice_turn
        return since_last >= self.MIN_INTERVAL

    def generate(
        self,
        state: "StoryState",
        recent_dialogue: str,
        location: str = "",
        present_characters: str = "",
    ) -> dict:
        """生成普通抉择。

        Args:
            state: 当前故事状态
            recent_dialogue: 最近 N 轮对话文本
            location: 当前地点
            present_characters: 在场角色列表

        Returns:
            {"moment": "...", "choices": [{"text": "...", "intimacy_changes": {...}, ...}]}
        """
        user_prompt = CHOICE_GENERATION_PROMPT.format(
            user_summary=state.user_character.summary,
            chapter=state.chapter,
            location=location or "未知",
            present_characters=present_characters or "未知",
            intimacy_summary=state.intimacy_summary(),
            flags_summary=state.plot_flags_summary(),
            recent_dialogue=recent_dialogue[-2000:] if recent_dialogue else "（对话刚开始）",
        )

        try:
            result = self._llm.chat_json(
                system_prompt="你是一个专业的互动小说编剧。只返回 JSON，不返回其他内容。",
                user_prompt=user_prompt,
                temperature=0.7,
                max_tokens=1024,
            )
            if isinstance(result, dict) and result.get("choices"):
                # 确保必要字段存在
                for c in result["choices"]:
                    c.setdefault("intimacy_changes", {})
                    c.setdefault("risk", "medium")
                    c.setdefault("strategy", "中立")
                return result
        except Exception:
            pass

        # Fallback: 返回一个简单抉择
        return {
            "moment": "你感到需要做出选择",
            "choices": [
                {"text": "继续当前对话", "intimacy_changes": {}, "risk": "low", "strategy": "沉默"},
                {"text": "主动推进话题", "intimacy_changes": {}, "risk": "medium", "strategy": "冒险"},
            ],
        }

    @staticmethod
    def format_choice_display(choice_result: dict) -> str:
        """格式化抉择为终端展示文本。"""
        lines = [
            "",
            "═" * 42,
            f" ⚡ {choice_result.get('moment', '抉择时刻')}",
            "═" * 42,
        ]
        for i, c in enumerate(choice_result.get("choices", [])):
            letter = chr(65 + i)  # A, B, C
            changes = c.get("intimacy_changes", {})
            risk_label = {"low": "低风险", "medium": "中风险", "high": "高风险"}.get(
                c.get("risk", "medium"), ""
            )
            change_str = " | ".join(f"{name}:{delta:+d}" for name, delta in changes.items())
            lines.append(f"")
            lines.append(f"  {letter}) {c.get('text', '')}")
            lines.append(f"     → {change_str} | {risk_label}" if change_str else f"     → {risk_label}")
        lines.append("═" * 42)
        return "\n".join(lines)
