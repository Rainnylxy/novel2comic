# -*- coding: utf-8 -*-
"""搜索服务 —— 章节搜索和关键词提取。

提取自 agent.py 中 ask_plot 工具的搜索逻辑。
"""

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from novel2comic.src.models import Novel, ChapterInfo


class SearchService:
    """章节搜索服务。

    提取自 agent.py 第 804-843 行的 _extract_keywords + _search_chapters 逻辑。
    """

    # 常见中文停用词
    STOP_WORDS = {
        "的", "了", "在", "是", "我", "有", "和", "就",
        "不", "人", "都", "一", "一个", "上", "也", "很",
        "到", "说", "要", "去", "你", "会", "着", "没有",
        "看", "好", "自己", "这", "他", "她", "它", "们",
        "那", "什么", "怎么", "为什么", "如何", "哪里",
        "吗", "吧", "呢", "啊", "哦", "嗯",
    }

    @staticmethod
    def extract_keywords(question: str) -> list[str]:
        """从问题中提取关键词。

        使用简单的中文分词（按常见停用词切分）。

        Args:
            question: 用户问题

        Returns:
            关键词列表
        """
        # 按停用词和非中文字符切分
        cleaned = re.sub(r"[^一-鿿\w]", " ", question)
        words = cleaned.split()
        keywords = [
            w for w in words
            if w not in SearchService.STOP_WORDS and len(w) >= 2
        ]
        return keywords[:10]  # 最多 10 个关键词

    @staticmethod
    def search_chapters(
        novel: "Novel",
        keywords: list[str],
        max_results: int = 5,
    ) -> list:
        """搜索与关键词最相关的章节。

        Args:
            novel: Novel 对象
            keywords: 关键词列表
            max_results: 最大返回章节数

        Returns:
            相关 ChapterInfo 列表，带 relevance 分数
        """
        if not novel or not novel.chapters:
            return []

        scored = []
        for ch in novel.chapters:
            content = ch.content or ""
            score = 0
            for kw in keywords:
                score += content.count(kw)
            if score > 0:
                scored.append((ch, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return [ch for ch, _ in scored[:max_results]]
