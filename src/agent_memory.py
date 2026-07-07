# -*- coding: utf-8 -*-
"""AgentMemory —— 精简记忆层（续写系统）。

BaseAgent 需要一个 AgentMemory 实例，但续写系统不涉及角色扮演状态。
这里提供一个最小实现，对话管理委托给 AgentFlow 的 WorkingMemory/EpisodicMemory。
"""


class AgentMemory:
    """最小记忆管理器。续写系统不需要 roleplay 的数字化心理引擎。"""

    def __init__(self):
        pass
