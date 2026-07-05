# -*- coding: utf-8 -*-
"""续写系统 —— 多 Agent 协作流水线。

组件:
  - AuthorStyleDistiller: 作者文风蒸馏
  - PlotArchitect: 剧情架构师
  - ChapterWriter: 章节写手（流式核心）
  - ConsistencyReviewer: 一致性审校
  - RevisionEditor: 修订编辑
  - ContinuationPipeline: 流水线编排器
"""

from .chapter_writer import ChapterWriter
from .consistency_reviewer import ConsistencyReviewer
from .revision_editor import RevisionEditor

__all__ = ["ChapterWriter", "ConsistencyReviewer", "RevisionEditor"]
