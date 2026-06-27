# -*- coding: utf-8 -*-
"""角色蒸馏数据模型 —— CharacterProfile 及其子组件。

从 src/models.py 中抽取，避免单文件过大。
"""

from dataclasses import dataclass, field, asdict


@dataclass
class VoiceProfile:
    """角色表达特征——从原文对话/内心独白中蒸馏。

    统计部分由代码直接计算，主观部分由 LLM 标注。
    """
    # — 统计特征（代码计算）—
    avg_sentence_length: float = 0.0
    sentence_range: list = field(default_factory=lambda: [4, 22])
    exclamation_density: float = 0.0
    ellipsis_density: float = 0.0
    question_density: float = 0.0
    first_person: str = "我"
    sentence_types: dict = field(default_factory=dict)

    # — 语气光谱（LLM 标注）—
    tone_cold_warm: float = 0.5
    tone_hard_soft: float = 0.5
    tone_distant_close: float = 0.5

    # — 禁忌 —
    taboo_words: list[str] = field(default_factory=list)
    taboo_patterns: list[str] = field(default_factory=list)

    # — 结构特征 —
    response_pattern: str = ""
    rhythm: str = ""

    # — 对不同对象的表达差异 —
    voice_shift: dict = field(default_factory=dict)

    # — 一句话总结 —
    summary: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "VoiceProfile":
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


@dataclass
class BoundaryProfile:
    """角色行为边界。"""
    hard_rules: list[str] = field(default_factory=list)
    tendencies: list[str] = field(default_factory=list)
    relationship_behaviors: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "BoundaryProfile":
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


@dataclass
class StateProfile:
    """角色心理变量基线。"""
    baseline: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"baseline": self.baseline}

    @classmethod
    def from_dict(cls, d: dict) -> "StateProfile":
        return cls(baseline=d.get("baseline", {}))


@dataclass
class SensitivityEntry:
    """单条敏感度规则。"""
    triggers: list[str] = field(default_factory=list)
    effects: dict = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    confidence: str = "推测"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SensitivityEntry":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class SensitivityProfile:
    """敏感度系数集合。"""
    entries: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"entries": [
            e.to_dict() if isinstance(e, SensitivityEntry) else e
            for e in self.entries
        ]}

    @classmethod
    def from_dict(cls, d: dict) -> "SensitivityProfile":
        entries = []
        for e in d.get("entries", []):
            if isinstance(e, SensitivityEntry):
                entries.append(e)
            else:
                entries.append(SensitivityEntry.from_dict(e))
        return cls(entries=entries)


@dataclass
class RecoveryProfile:
    """恢复速率。"""
    rates: dict = field(default_factory=dict)
    triggers: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RecoveryProfile":
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


@dataclass
class PolicyAnchor:
    """行为锚点。"""
    state_snapshot: dict = field(default_factory=dict)
    situation: str = ""
    action: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PolicyAnchor":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class CharacterProfile:
    """角色的完整蒸馏定义。"""
    name: str = ""
    voice: VoiceProfile = field(default_factory=VoiceProfile)
    boundary: BoundaryProfile = field(default_factory=BoundaryProfile)
    state: StateProfile = field(default_factory=StateProfile)
    sensitivity: SensitivityProfile = field(default_factory=SensitivityProfile)
    recovery: RecoveryProfile = field(default_factory=RecoveryProfile)
    policy_anchors: list = field(default_factory=list)
    distilled_at: str = ""
    confidence: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "voice": self.voice.to_dict(),
            "boundary": self.boundary.to_dict(),
            "state": self.state.to_dict(),
            "sensitivity": self.sensitivity.to_dict(),
            "recovery": self.recovery.to_dict(),
            "policy_anchors": [
                a.to_dict() if isinstance(a, PolicyAnchor) else a
                for a in self.policy_anchors
            ],
            "distilled_at": self.distilled_at,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CharacterProfile":
        profile = cls(
            name=d.get("name", ""),
            distilled_at=d.get("distilled_at", ""),
            confidence=d.get("confidence", ""),
        )
        if d.get("voice"):
            profile.voice = VoiceProfile.from_dict(d["voice"])
        if d.get("boundary"):
            profile.boundary = BoundaryProfile.from_dict(d["boundary"])
        if d.get("state"):
            profile.state = StateProfile.from_dict(d["state"])
        if d.get("sensitivity"):
            profile.sensitivity = SensitivityProfile.from_dict(d["sensitivity"])
        if d.get("recovery"):
            profile.recovery = RecoveryProfile.from_dict(d["recovery"])
        anchors = d.get("policy_anchors", [])
        profile.policy_anchors = [
            PolicyAnchor.from_dict(a) if not isinstance(a, PolicyAnchor) else a
            for a in anchors
        ]
        return profile
