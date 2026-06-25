# -*- coding: utf-8 -*-
"""Agent 层 —— "导演"，做创意决策。

每个 Agent 是专注于一个领域的轻量 Agent（3-5 个工具），
通过 ServiceRegistry 调用管线服务，通过共享 KG 获取小说信息。
"""

from novel2comic.src.agents.base_agent import BaseAgent
from novel2comic.src.agents.comic_agent import ComicAdaptationAgent
from novel2comic.src.agents.continuation_agent import ContinuationAgent
from novel2comic.src.agents.roleplay_agent import RolePlayAgent
from novel2comic.src.agents.recommendation_agent import RecommendationAgent
from novel2comic.src.agents.summarization_agent import SummarizationAgent

__all__ = [
    "BaseAgent",
    "ComicAdaptationAgent",
    "ContinuationAgent",
    "RolePlayAgent",
    "RecommendationAgent",
    "SummarizationAgent",
]
