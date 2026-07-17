# -*- coding: utf-8 -*-
"""续写质量 LLM Judge —— 基于原文 KG 对续写内容打分。

评估 5 个维度:
  1. character_consistency — 角色行为是否符合前文 Voice/Boundary
  2. setting_consistency   — 设定是否有矛盾（死活、派系、时间线）
  3. style_consistency     — 基调、笔法是否接近原文
  4. plot_coherence        — 续写各章之间是否自洽
  5. writing_quality       — 对话是否自然、叙述是否流畅

每维度 1-10 分，附带理由。
"""

import json
import os
from typing import Optional

# 默认 prompt 模板
JUDGE_SYSTEM_PROMPT = """你是专业小说编辑，擅长评估续写质量。请根据原文前文（KG来源）对续写内容进行评分。

评分标准（每题1-10分）：

1. **角色一致性**: 续写中的角色行为、说话方式是否符合前文建立的性格？有没有OOC？
   - 10分: 角色言行完全贴合前文设定，Voice/行为边界无偏差
   - 5分: 部分角色有轻微OOC，但主要角色基本贴合
   - 1分: 角色完全脱离前文设定

2. **设定一致性**: 有没有角色死而复生、派系写错、时间线矛盾、身份错误？
   - 10分: 无任何设定矛盾
   - 5分: 有1-2处小矛盾但不影响主线
   - 1分: 严重的设定矛盾（如死人复活、身份颠倒）

3. **风格一致性**: 基调、节奏、句长、用词是否接近前文风格？
   - 10分: 几乎无法分辨是续写
   - 5分: 基本接近，偶有风格波动
   - 1分: 风格完全偏离（如刑侦变言情）

4. **情节连贯性**: 续写各章之间是否自洽？情节推进是否合理？
   - 10分: 情节层层推进，章间衔接自然，无跳跃感
   - 5分: 基本连贯，偶有突兀转折
   - 1分: 章间断裂，情节不连贯

5. **写作质量**: 对话是否自然？叙述是否流畅？有没有水字数/重复？
   - 10分: 文笔出色，对话鲜活，节奏得当
   - 5分: 通顺可读，偶有冗余
   - 1分: 语言生硬、大量重复或无效内容

请返回 JSON:
{
  "scores": {
    "character_consistency": {"score": N, "reason": "..."},
    "setting_consistency": {"score": N, "reason": "..."},
    "style_consistency": {"score": N, "reason": "..."},
    "plot_coherence": {"score": N, "reason": "..."},
    "writing_quality": {"score": N, "reason": "..."}
  },
  "overall": N,
  "summary": "一句话总结"
}

只返回 JSON，不要其他内容。"""


def build_judge_prompt(source_text: str, generated_text: str,
                        genre: str = "", max_source: int = 3000,
                        max_generated: int = 4000) -> str:
    """构建 judge prompt。

    Args:
        source_text: 原文前 N 章（KG 来源）
        generated_text: 续写生成的完整文本
        genre: 小说类型（可选，帮助 judge 理解上下文）
        max_source: 原文最大输入长度
        max_generated: 续写最大输入长度

    Returns:
        完整的 user prompt 字符串
    """
    genre_hint = f"\n小说类型: {genre}\n" if genre else ""

    return (
        f"## 原文前文（续写基于此文本构建的知识图谱）{genre_hint}\n"
        f"{source_text[:max_source]}\n\n"
        f"---\n\n"
        f"## 续写内容\n"
        f"{generated_text[:max_generated]}\n\n"
        f"请根据原文前文评估此续写的质量，按评分标准逐维度打分。"
    )


def fragments_to_text(fragments: list) -> str:
    """将 StoryFragment 列表拼接为可读文本。

    Args:
        fragments: [{"type": "dialogue", "character": "江停", "text": "..."}, ...]

    Returns:
        格式化的续写文本
    """
    lines = []
    for f in fragments:
        ftype = f.get("type", "narration")
        text = f.get("text", "")
        char = f.get("character", "")

        if ftype == "divider":
            label = f.get("divider_label", "")
            lines.append(f"\n--- {label} ---\n")
        elif ftype == "dialogue":
            lines.append(f"「{char}」{text}")
        elif ftype == "action":
            lines.append(f"({char} {text})")
        elif ftype == "inner_thought":
            lines.append(f"【{char} 内心】{text}")
        else:
            lines.append(text)
    return "\n".join(lines)


class QualityJudge:
    """续写质量评估器。

    用法:
        judge = QualityJudge(llm)
        result = judge.evaluate(source_chapters, generated_chapters, genre="刑侦推理")
        # result = {
        #     "scores": {...},
        #     "overall": 7.5,
        #     "summary": "..."
        # }
    """

    def __init__(self, llm):
        """初始化 Judge。

        Args:
            llm: UnifiedLLM 实例（需要 chat_json 方法）
        """
        self._llm = llm

    def evaluate(self, source_text: str, generated_text: str,
                 genre: str = "") -> dict:
        """运行评估。

        Args:
            source_text: 原文前 N 章文本（KG 来源）
            generated_text: 续写生成的完整文本
            genre: 小说类型

        Returns:
            评估结果 dict，含 scores / overall / summary
        """
        prompt = build_judge_prompt(source_text, generated_text, genre)

        try:
            result = self._llm.chat_json(
                system_prompt=JUDGE_SYSTEM_PROMPT,
                user_prompt=prompt,
                temperature=0.3,
                max_tokens=2048,
            )
            if isinstance(result, dict) and "scores" in result:
                return self._normalize(result)
        except Exception as e:
            print(f"  [Judge] LLM 调用失败: {e}")

        # Fallback
        return {
            "scores": {d: {"score": 0, "reason": "judge failed"}
                       for d in ["character_consistency", "setting_consistency",
                                  "style_consistency", "plot_coherence", "writing_quality"]},
            "overall": 0,
            "summary": f"Judge 评估失败",
            "error": str(e) if 'e' in dir() else "unknown",
        }

    def _normalize(self, result: dict) -> dict:
        """规范化 judge 输出。"""
        # 确保所有维度存在
        expected_dims = [
            "character_consistency", "setting_consistency",
            "style_consistency", "plot_coherence", "writing_quality",
        ]
        scores = result.get("scores", {})
        for dim in expected_dims:
            if dim not in scores:
                scores[dim] = {"score": 0, "reason": "missing"}

        # 计算 overall（如果没有）
        if "overall" not in result or not isinstance(result["overall"], (int, float)):
            valid_scores = [s["score"] for s in scores.values()
                           if isinstance(s.get("score"), (int, float)) and s["score"] > 0]
            result["overall"] = (sum(valid_scores) / len(valid_scores)
                                 if valid_scores else 0)

        return result

    def evaluate_chapter(self, source_text: str, chapter_fragments: list,
                         genre: str = "") -> dict:
        """评估单章续写。

        Args:
            source_text: 原文前文文本
            chapter_fragments: 本章 fragment 列表
            genre: 小说类型

        Returns:
            评估结果
        """
        generated = fragments_to_text(chapter_fragments)
        return self.evaluate(source_text, generated, genre)
