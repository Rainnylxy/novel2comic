# -*- coding: utf-8 -*-
"""管线服务层 —— 确定性执行任务的封装。

每个服务无状态（除配置和客户端外），
Agent 通过 ServiceRegistry 访问服务。
"""

from novel2comic.src.services.kg_service import KnowledgeGraphService
from novel2comic.src.services.image_service import ImageGenerationService
from novel2comic.src.services.comic_service import ComicCompilationService
from novel2comic.src.services.project_service import ProjectService
from novel2comic.src.services.search_service import SearchService

__all__ = [
    "KnowledgeGraphService",
    "ImageGenerationService",
    "ComicCompilationService",
    "ProjectService",
    "SearchService",
]
