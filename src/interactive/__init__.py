# -*- coding: utf-8 -*-
"""互动小说引擎 —— 用户角色 × 抉择 × 多结局。

DirectorAgent: 编剧+导演，管理场景、NPC调度、抉择生成
StoryState: 游戏存档（亲密度 + 旗标 + 决策日志）
ChoiceEngine: 普通抉择生成（间隔触发）
NPCManager: NPC 角色池 + 亲密度注入
"""

from .story_state import StoryState, UserCharacter, PivotEvent
from .choice_engine import ChoiceEngine
from .npc_manager import NPCManager
from .director_agent import DirectorAgent

__all__ = [
    "DirectorAgent",
    "StoryState",
    "UserCharacter",
    "PivotEvent",
    "ChoiceEngine",
    "NPCManager",
]
