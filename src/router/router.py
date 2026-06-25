# -*- coding: utf-8 -*-
"""意图路由器 —— 将用户自然语言输入路由到正确的 Agent。

路由策略：
1. 显式子命令（CLI --agent 或子命令优先级最高）
2. 基于关键词的自动分类
3. 低置信度时要求澄清
"""

from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from novel2comic.src.context import GlobalContext, ServiceRegistry


class Intent(str, Enum):
    """用户意图枚举。"""
    COMIC = "comic"
    CONTINUATION = "continuation"
    ROLEPLAY = "roleplay"
    RECOMMENDATION = "recommendation"
    SUMMARIZATION = "summarization"


class IntentRouter:
    """意图路由器。

    将用户自然语言映射到对应的 Agent。
    """

    # 每个意图的关键词触发词
    INTENT_KEYWORDS: dict[Intent, list[str]] = {
        Intent.COMIC: [
            "生成", "漫画", "分镜", "storyboard", "分格",
            "场景", "排版", "compile", "生成图片", "改编",
            "画成漫画", "转为漫画", "做成漫画",
        ],
        Intent.CONTINUATION: [
            "续写", "继续写", "后续", "下一章", "接着写",
            "写下去", "接下来", "连载",
        ],
        Intent.ROLEPLAY: [
            "扮演", "对话", "聊天", "聊聊", "说话",
            "当成", "你要扮演", "假装你是", "cos",
            "你会说", "我是", "告诉我",
        ],
        Intent.RECOMMENDATION: [
            "推荐", "推荐类似", "喜欢什么", "有没有",
            "想看", "类似", "找书", "书荒",
        ],
        Intent.SUMMARIZATION: [
            "总结", "摘要", "概括", "归纳", "主题",
            "角色分析", "关系分析", "讲了什么", "分析",
        ],
    }

    @classmethod
    def classify(cls, text: str) -> tuple[Intent, float]:
        """使用关键词匹配进行分类。

        Args:
            text: 用户输入的自然语言文本

        Returns:
            (最佳意图, 置信度 0.0-1.0)
        """
        scores: dict[Intent, int] = {intent: 0 for intent in Intent}

        for intent, keywords in cls.INTENT_KEYWORDS.items():
            for kw in keywords:
                if kw in text:
                    scores[intent] += 1

        total = sum(scores.values())
        if total == 0:
            # 没有任何关键词匹配，默认返回漫画改编（最常见的场景）
            return Intent.COMIC, 0.0

        best_intent = max(scores, key=scores.get)  # type: ignore
        confidence = scores[best_intent] / total
        return best_intent, confidence

    @classmethod
    def classify_with_confidence(cls, text: str) -> dict:
        """返回详细的分类结果。"""
        intent, confidence = cls.classify(text)
        return {
            "intent": intent.value,
            "confidence": round(confidence, 2),
            "action": cls._action_for_intent(intent, confidence),
        }

    @staticmethod
    def _action_for_intent(intent: Intent, confidence: float) -> str:
        """根据置信度决定处理策略。"""
        if confidence >= 0.8:
            return "auto_route"       # 直接路由，无需确认
        elif confidence >= 0.5:
            return "confirm"          # 路由但提示确认
        else:
            return "ask_clarify"      # 无法判断，要求澄清

    @classmethod
    def format_clarification(cls, text: str, intent: Intent, confidence: float) -> str:
        """生成澄清提示。"""
        if confidence >= 0.8:
            return f"[Router] 识别意图: {intent.value} (置信度: {confidence:.0%})"
        elif confidence >= 0.5:
            return (
                f"[Router] 判断为: {intent.value} (置信度: {confidence:.0%})\n"
                f"如果不是，请告诉我你想做什么。"
            )
        else:
            return (
                f"[Router] 不确定你想做什么。\n"
                f"你可以试试这些命令：\n"
                f"  · 生成漫画 / 改编 → comic\n"
                f"  · 续写下一章 → continue\n"
                f"  · 和角色对话 → roleplay\n"
                f"  · 推荐类似小说 → recommend\n"
                f"  · 总结分析 → summarize"
            )
