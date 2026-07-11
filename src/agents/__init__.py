# -*- coding: utf-8 -*-
"""Agent 层 —— ReAct Agent 实现。

  - BaseAgent:    AgentFlow 集成基类
  - PlotArchitect: 剧情架构师（两级规划）
  - ChapterWriter: 章节写手（流式核心）
  - ReviewEditor:  审校 + 修订（合并）
"""

from .base_agent import BaseAgent
from .plot_architect import PlotArchitect, make_fallback_chapter, make_fallback_roadmap
from .chapter_writer import ChapterWriter
from .review_editor import ReviewEditor

__all__ = [
    "BaseAgent",
    "PlotArchitect", "make_fallback_chapter", "make_fallback_roadmap",
    "ChapterWriter",
    "ReviewEditor",
]
