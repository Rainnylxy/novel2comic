# -*- coding: utf-8 -*-
"""AgentMemory —— AgentLLM 记忆组织层。

职责：跨 Agent、跨会话的记忆管理。

两层结构：
- Key-Value 记忆：global / agent / session（通用偏好、决策、笔记）
- 领域状态：结构化子对象（如 RolePlayState）

与 PromptContext 的关系：
- AgentMemory  → AgentLLM 记忆（记住"什么"，跨时间持久化）
- PromptContext → LLM Prompt 组织（拼装"怎么问"，单次调用）
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ============================================================
# 通用记忆条目
# ============================================================

@dataclass
class MemoryEntry:
    """单条 key-value 记忆。"""
    key: str
    value: str
    scope: str = "session"
    agent_type: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    access_count: int = 0
    importance: int = 1


# ============================================================
# 角色扮演领域状态
# ============================================================

@dataclass
class CharacterState:
    """单个角色的当前状态。"""
    name: str
    mood: str = "平静"
    location: str = ""
    goals: list = field(default_factory=list)
    physical_state: str = "正常"


@dataclass
class ConversationTurn:
    """对话中的一轮。"""
    speaker: str
    message: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    emotion: str = ""
    action: str = ""


@dataclass
class RelationshipChange:
    """对话过程中记录的关系变化。"""
    from_char: str
    to_char: str
    field: str
    old_value: str
    new_value: str
    reason: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class RolePlayState:
    """角色扮演的完整领域状态。

    挂载在 AgentMemory.roleplay 下。
    切换 Agent 再切回来，状态不丢。
    """

    active_character: str = ""
    character_states: dict = field(default_factory=dict)   # {name: CharacterState}
    conversation_history: list = field(default_factory=list)  # [ConversationTurn]
    story_timeline_point: int = 0
    relationship_changes: list = field(default_factory=list)  # [RelationshipChange]
    world_context: str = ""
    character_knowledge: dict = field(default_factory=dict)   # {char_name: kg_text}

    # ── 角色状态 ──

    def get_character_state(self, name: str) -> CharacterState:
        if name not in self.character_states:
            self.character_states[name] = CharacterState(name=name)
        return self.character_states[name]

    def update_mood(self, character: str, new_mood: str, reason: str = ""):
        state = self.get_character_state(character)
        old_mood = state.mood
        state.mood = new_mood
        if reason:
            self.relationship_changes.append(RelationshipChange(
                from_char=character, to_char="self",
                field="mood", old_value=old_mood, new_value=new_mood,
                reason=reason,
            ))

    # ── 对话历史 ──

    def add_turn(self, speaker: str, message: str,
                 emotion: str = "", action: str = ""):
        self.conversation_history.append(ConversationTurn(
            speaker=speaker, message=message,
            emotion=emotion, action=action,
        ))

    def get_conversation_context(self, max_turns: int = 20) -> str:
        recent = self.conversation_history[-max_turns:]
        lines = []
        for turn in recent:
            suffix = ""
            if turn.emotion:
                suffix += f" [{turn.emotion}]"
            if turn.action:
                suffix += f" ({turn.action})"
            lines.append(f"{turn.speaker}: {turn.message}{suffix}")
        return "\n".join(lines)

    def get_character_context(self, name: str) -> str:
        state = self.get_character_state(name)
        return (
            f"角色: {name}\n"
            f"当前情绪: {state.mood}\n"
            f"当前位置: {state.location}\n"
            f"当前目标: {', '.join(state.goals) if state.goals else '无'}\n"
            f"身体状态: {state.physical_state}"
        )

    # ── 知识过滤 ──

    def build_knowledge_filter(self, name: str, graph) -> str:
        """角色只能知道自己经历过的事和认识的人。"""
        person = graph.get_person_node(name) if graph else None
        if not person:
            return ""

        max_ch = self.story_timeline_point or (person.first_appearance_chapter or 0) + 10

        known_events = graph.character_events(name) if graph else []

        known_persons = {name}
        if graph:
            for edge in graph.relationship_edges:
                if edge.from_char == name:
                    known_persons.add(edge.to_char)
                elif edge.to_char == name:
                    known_persons.add(edge.from_char)

        parts = [f"## {name} 的视角（截止第 {max_ch} 章）"]
        if known_persons:
            parts.append(f"\n### 认识的角色\n{', '.join(sorted(known_persons))}")
        if known_events:
            parts.append("\n### 经历的事件")
            for e in known_events[:10]:
                parts.append(f"- [{e.event_type}] {e.name} (第{e.chapter_start}章): {e.summary[:60]}")
        if graph:
            parts.append("\n### 人际关系")
            for edge in graph.relationship_edges:
                if (edge.from_char == name and edge.to_char in known_persons) or \
                   (edge.to_char == name and edge.from_char in known_persons):
                    target = edge.to_char if edge.from_char == name else edge.from_char
                    parts.append(
                        f"- {name} ←→ {target}: {edge.relation_type}"
                        f" | 亲密度: {edge.intimacy:+d}"
                        + (f" | {edge.current_tension}" if edge.current_tension else "")
                    )
        return "\n".join(parts)

    # ── 序列化 ──

    def to_dict(self) -> dict:
        return {
            "active_character": self.active_character,
            "character_states": {
                k: {"name": v.name, "mood": v.mood, "location": v.location,
                    "goals": v.goals, "physical_state": v.physical_state}
                for k, v in self.character_states.items()
            },
            "conversation_history": [
                {"speaker": t.speaker, "message": t.message,
                 "timestamp": t.timestamp, "emotion": t.emotion, "action": t.action}
                for t in self.conversation_history[-50:]  # 最多保存 50 轮
            ],
            "story_timeline_point": self.story_timeline_point,
            "relationship_changes": [
                {"from_char": r.from_char, "to_char": r.to_char,
                 "field": r.field, "old_value": r.old_value,
                 "new_value": r.new_value, "reason": r.reason,
                 "timestamp": r.timestamp}
                for r in self.relationship_changes[-30:]
            ],
            "world_context": self.world_context,
            "character_knowledge": self.character_knowledge,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RolePlayState":
        rp = cls(
            active_character=d.get("active_character", ""),
            story_timeline_point=d.get("story_timeline_point", 0),
            world_context=d.get("world_context", ""),
            character_knowledge=d.get("character_knowledge", {}),
        )
        for name, s in d.get("character_states", {}).items():
            rp.character_states[name] = CharacterState(
                name=s["name"], mood=s.get("mood", "平静"),
                location=s.get("location", ""), goals=s.get("goals", []),
                physical_state=s.get("physical_state", "正常"),
            )
        for t in d.get("conversation_history", []):
            rp.conversation_history.append(ConversationTurn(
                speaker=t["speaker"], message=t["message"],
                timestamp=t.get("timestamp", ""), emotion=t.get("emotion", ""),
                action=t.get("action", ""),
            ))
        for r in d.get("relationship_changes", []):
            rp.relationship_changes.append(RelationshipChange(
                from_char=r["from_char"], to_char=r["to_char"],
                field=r["field"], old_value=r["old_value"],
                new_value=r["new_value"], reason=r["reason"],
                timestamp=r.get("timestamp", ""),
            ))
        return rp


# ============================================================
# AgentMemory
# ============================================================

class AgentMemory:
    """AgentLLM 记忆管理器。

    通用记忆（key-value）:
    - Global  — 跨 Agent 共享（用户偏好、小说信息）
    - Agent   — Agent 内持久（历史决策）
    - Session — 当前会话（对话上下文）

    领域状态（结构化）:
    - roleplay — RolePlayState（角色扮演的完整状态）
    """

    MAX_GLOBAL = 100
    MAX_AGENT = 200
    MAX_SESSION = 50

    def __init__(self):
        self._global: dict[str, MemoryEntry] = {}
        self._agent: dict[str, dict[str, MemoryEntry]] = {}
        self._session: dict[str, MemoryEntry] = {}
        self.roleplay = RolePlayState()

    # ================================================================
    # Key-Value 记忆
    # ================================================================

    def remember(self, key: str, value: str, scope: str = "session",
                 agent_type: str = "", importance: int = 1):
        entry = MemoryEntry(key=key, value=value, scope=scope,
                            agent_type=agent_type, importance=importance)
        if scope == "global":
            if len(self._global) >= self.MAX_GLOBAL:
                self._evict(self._global)
            self._global[key] = entry
        elif scope == "agent":
            self._agent.setdefault(agent_type, {})
            if len(self._agent[agent_type]) >= self.MAX_AGENT:
                self._evict(self._agent[agent_type])
            self._agent[agent_type][key] = entry
        else:
            if len(self._session) >= self.MAX_SESSION:
                self._evict(self._session)
            self._session[key] = entry

    def recall(self, key: str, scope: str = "all") -> Optional[str]:
        if scope in ("all", "global") and key in self._global:
            self._global[key].access_count += 1
            return self._global[key].value
        if scope in ("all", "agent"):
            for entries in self._agent.values():
                if key in entries:
                    entries[key].access_count += 1
                    return entries[key].value
        if scope in ("all", "session") and key in self._session:
            self._session[key].access_count += 1
            return self._session[key].value
        return None

    def forget(self, key: str, scope: str = "all"):
        if scope in ("all", "global") and key in self._global:
            del self._global[key]
        if scope in ("all", "agent"):
            for entries in self._agent.values():
                if key in entries:
                    del entries[key]
        if scope in ("all", "session") and key in self._session:
            del self._session[key]

    def search(self, query: str) -> list[MemoryEntry]:
        results = []
        for store in [self._global, self._session]:
            for key, entry in store.items():
                if query in key or query in entry.value:
                    results.append(entry)
                    entry.access_count += 1
        for agent_entries in self._agent.values():
            for key, entry in agent_entries.items():
                if query in key or query in entry.value:
                    results.append(entry)
                    entry.access_count += 1
        results.sort(key=lambda e: (e.importance, e.access_count), reverse=True)
        return results[:10]

    # ================================================================
    # Agent 上下文构建
    # ================================================================

    def build_agent_context(self, agent_type: str) -> str:
        """为 Agent 构建记忆注入文本。"""
        parts = []

        if self._global:
            items = sorted(self._global.values(),
                           key=lambda e: e.importance, reverse=True)
            lines = ["## 用户偏好与全局记忆"]
            for e in items[:10]:
                lines.append(f"- {e.key}: {e.value}")
            parts.append("\n".join(lines))

        if agent_type in self._agent:
            items = sorted(self._agent[agent_type].values(),
                           key=lambda e: e.importance, reverse=True)
            lines = [f"## {agent_type} Agent 历史决策"]
            for e in items[:10]:
                lines.append(f"- [{e.timestamp[:16]}] {e.key}: {e.value}")
            parts.append("\n".join(lines))

        if self._session:
            lines = ["## 当前会话状态"]
            for e in self._session.values():
                lines.append(f"- {e.key}: {e.value}")
            parts.append("\n".join(lines))

        return "\n\n".join(parts) if parts else ""

    # ================================================================
    # 持久化
    # ================================================================

    def save(self, filepath: str):
        data = {
            "global": {k: self._entry_to_dict(v) for k, v in self._global.items()},
            "agent": {
                agent: {k: self._entry_to_dict(v) for k, v in entries.items()}
                for agent, entries in self._agent.items()
            },
            "session": {k: self._entry_to_dict(v) for k, v in self._session.items()},
            "roleplay": self.roleplay.to_dict(),
        }
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, filepath: str) -> "AgentMemory":
        memory = cls()
        if not os.path.exists(filepath):
            return memory
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in data.get("global", {}).items():
            memory._global[k] = cls._dict_to_entry(v)
        for agent, entries in data.get("agent", {}).items():
            memory._agent[agent] = {
                k: cls._dict_to_entry(v) for k, v in entries.items()
            }
        for k, v in data.get("session", {}).items():
            memory._session[k] = cls._dict_to_entry(v)
        if "roleplay" in data:
            memory.roleplay = RolePlayState.from_dict(data["roleplay"])
        return memory

    # ================================================================
    # 内部
    # ================================================================

    @staticmethod
    def _entry_to_dict(entry: MemoryEntry) -> dict:
        return {
            "key": entry.key, "value": entry.value,
            "scope": entry.scope, "agent_type": entry.agent_type,
            "timestamp": entry.timestamp, "access_count": entry.access_count,
            "importance": entry.importance,
        }

    @staticmethod
    def _dict_to_entry(d: dict) -> MemoryEntry:
        return MemoryEntry(
            key=d["key"], value=d["value"],
            scope=d.get("scope", "session"),
            agent_type=d.get("agent_type", ""),
            timestamp=d.get("timestamp", ""),
            access_count=d.get("access_count", 0),
            importance=d.get("importance", 1),
        )

    def _evict(self, store: dict):
        if not store:
            return
        candidates = sorted(
            store.values(),
            key=lambda e: (e.importance, e.access_count, e.timestamp),
        )
        del store[candidates[0].key]
