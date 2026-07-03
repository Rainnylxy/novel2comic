# -*- coding: utf-8 -*-
"""AgentMemory —— 精简记忆层。

职责：
- RolePlayState: 角色扮演领域状态（数字化心理引擎 + 知识过滤）
- AgentMemory: 持有 RolePlayState，提供持久化

对话管理委托给 AgentFlow 的 WorkingMemory / EpisodicMemory。
通用 KV 记忆已移除——跨 Agent 共享状态走 GlobalContext。
"""

import json
import os
from dataclasses import dataclass, field
from .scene_engine import SceneContext


# ============================================================
# 角色扮演领域状态
# ============================================================

@dataclass
class RolePlayState:
    """角色扮演的领域状态。

    只保留 AgentFlow 原生记忆系统不覆盖的内容：
    - 数字化心理引擎（runtime_state + sensitivity + recovery）
    - 知识过滤（KG 视角缓存）
    - 基础元数据
    """

    active_character: str = ""
    story_timeline_point: int = 0
    character_knowledge: dict = field(default_factory=dict)   # {char_name: kg_text}

    # 当前角色的简单属性（替代原 character_states 的非冗余部分）
    active_location: str = ""
    active_goals: list = field(default_factory=list)
    active_physical_state: str = "正常"

    # ── 数字化心理引擎 ──
    runtime_state: dict = field(default_factory=dict)
    # {"Trust": 65, "Fear": 15, "Anger": 25, ...}
    state_history: list = field(default_factory=list)
    # [{"event": "...", "deltas": {...}, "timestamp": "..."}]
    sensitivity_profile: dict = field(default_factory=dict)
    recovery_profile: dict = field(default_factory=dict)

    # ── 场景锚定 ──
    scene: "SceneContext" = field(default_factory=SceneContext)

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
                parts.append(
                    f"- [{e['event_type']}] {e['name']} "
                    f"(第{e['chapter_start']}章): {e['summary'][:60]}"
                )
        if graph:
            parts.append("\n### 人际关系")
            for edge in graph.relationship_edges:
                if (edge.from_char == name and edge.to_char in known_persons) or \
                   (edge.to_char == name and edge.from_char in known_persons):
                    target = edge.to_char if edge.from_char == name else edge.from_char
                    parts.append(
                        f"- {name} ←→ {target}: {edge.relation_type}"
                        f" | 亲密度: {edge.intimacy:+d}"
                        + (f" | {edge.power_dynamic}" if edge.power_dynamic and edge.power_dynamic != "平等" else "")
                        + (f" | {edge.current_tension}" if edge.current_tension and edge.current_tension != "和谐" else "")
                        + (" [隐藏关系，不可主动提及]" if not edge.public_knowledge else "")
                        + (f"\n  共同经历: {edge.shared_history}" if edge.shared_history else "")
                    )
        return "\n".join(parts)

    # ── 运行时 State 引擎 ──

    def set_profile(self, sensitivity: dict = None, recovery: dict = None,
                    baseline: dict = None):
        """从 CharacterProfile 加载参数，初始化 runtime_state。"""
        if sensitivity:
            self.sensitivity_profile = sensitivity
        if recovery:
            self.recovery_profile = recovery
        if baseline and not self.runtime_state:
            self.runtime_state = dict(baseline)

    def apply_event(self, event_type: str, intensity: float = 1.0) -> dict:
        """计算事件对 runtime_state 的影响。"""
        deltas = {}
        entries = self.sensitivity_profile.get("entries", [])
        for entry in entries:
            triggers = entry.get("triggers", [])
            if any(t.lower() in event_type.lower() for t in triggers):
                effects = entry.get("effects", {})
                for dim, coef in effects.items():
                    delta = coef * intensity
                    deltas[dim] = delta
                    current = self.runtime_state.get(dim, 50)
                    new_val = max(0, min(100, current + delta))
                    self.runtime_state[dim] = round(new_val, 1)
                break

        if deltas:
            from datetime import datetime
            self.state_history.append({
                "event": event_type,
                "deltas": deltas,
                "timestamp": datetime.now().isoformat(),
            })
            if len(self.state_history) > 30:
                self.state_history = self.state_history[-30:]

        return deltas

    def decay_state(self, delta_days: float = 0.0):
        """应用状态恢复（时间衰减）。"""
        if delta_days <= 0:
            return
        rates = self.recovery_profile.get("rates", {})
        for dim, rate in rates.items():
            if dim in self.runtime_state:
                baseline = self._get_baseline(dim)
                current = self.runtime_state[dim]
                if current > baseline:
                    decay = (current - baseline) * rate * delta_days
                    self.runtime_state[dim] = round(max(baseline, current - decay), 1)
                elif current < baseline:
                    recovery = (baseline - current) * rate * delta_days
                    self.runtime_state[dim] = round(min(baseline, current + recovery), 1)

    def _get_baseline(self, dim: str) -> float:
        return 50.0

    def format_state_for_prompt(self) -> str:
        """格式化当前 runtime_state 为 prompt 文本。"""
        if not self.runtime_state:
            return ""
        items = [f"{k}:{v:.0f}" for k, v in self.runtime_state.items()]
        return "当前心理状态: " + ", ".join(items)

    def get_emotion_summary(self) -> str:
        """基于 runtime_state 生成情绪描述。"""
        if not self.runtime_state:
            return "情绪平稳"
        anger = self.runtime_state.get("Anger", 25)
        fear = self.runtime_state.get("Fear", 15)
        trust = self.runtime_state.get("Trust", 65)
        love = self.runtime_state.get("Love", 60)

        parts = []
        if anger > 60:
            parts.append("愤怒难抑")
        elif anger > 40:
            parts.append("带点火气")
        if fear > 60:
            parts.append("恐惧不安")
        elif fear > 40:
            parts.append("有些警惕")
        if trust < 20:
            parts.append("不信任周围人")
        if love > 80:
            parts.append("心中温暖")
        elif love < 20:
            parts.append("心灰意冷")
        return ", ".join(parts) if parts else "情绪平稳"

    # ── 序列化 ──

    def to_dict(self) -> dict:
        return {
            "active_character": self.active_character,
            "story_timeline_point": self.story_timeline_point,
            "character_knowledge": self.character_knowledge,
            "active_location": self.active_location,
            "active_goals": self.active_goals,
            "active_physical_state": self.active_physical_state,
            "runtime_state": self.runtime_state,
            "state_history": self.state_history[-30:],
            "sensitivity_profile": self.sensitivity_profile,
            "recovery_profile": self.recovery_profile,
            "scene": self.scene.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RolePlayState":
        scene_data = d.get("scene", {})
        return cls(
            active_character=d.get("active_character", ""),
            story_timeline_point=d.get("story_timeline_point", 0),
            character_knowledge=d.get("character_knowledge", {}),
            active_location=d.get("active_location", ""),
            active_goals=d.get("active_goals", []),
            active_physical_state=d.get("active_physical_state", "正常"),
            runtime_state=d.get("runtime_state", {}),
            state_history=d.get("state_history", []),
            sensitivity_profile=d.get("sensitivity_profile", {}),
            recovery_profile=d.get("recovery_profile", {}),
            scene=SceneContext.from_dict(scene_data) if scene_data else SceneContext(),
        )


# ============================================================
# AgentMemory —— 只持有 RolePlayState + 持久化
# ============================================================

class AgentMemory:
    """精简后的记忆管理器。

    只持有 RolePlayState 并提供持久化。
    对话上下文由 AgentFlow 的 WorkingMemory/EpisodicMemory 管理。
    跨 Agent 共享状态走 GlobalContext。
    """

    def __init__(self):
        self.roleplay = RolePlayState()

    def save(self, filepath: str):
        data = {"roleplay": self.roleplay.to_dict()}
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
        if "roleplay" in data:
            memory.roleplay = RolePlayState.from_dict(data["roleplay"])
        return memory
