# -*- coding: utf-8 -*-
"""核心基础设施 —— AppContext, UnifiedLLM, Novel, StoryGraph 等。"""

from .context import AppContext, ServiceRegistry, GlobalContext
from .llm import UnifiedLLM
from .models import (
    Novel, StoryGraph,
    CharacterNode, EventNode, LocationNode,
    OrganizationNode, ItemNode,
    RelationshipEdge, ChapterInfo,
)

__all__ = [
    "AppContext", "ServiceRegistry", "GlobalContext",
    "UnifiedLLM",
    "Novel", "StoryGraph",
    "CharacterNode", "EventNode", "LocationNode",
    "OrganizationNode", "ItemNode",
    "RelationshipEdge", "ChapterInfo",
]
