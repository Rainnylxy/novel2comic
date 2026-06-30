# -*- coding: utf-8 -*-
"""Golden 数据集数据模型。

每个 GoldenCase 是一个可复用的评估样本，包含:
- 对话上下文 + 用户输入 → Golden 期望行为
- 评估维度和评分标准
"""

from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class GoldenCase:
    """一条 Golden 测试用例。

    包含完整的评估所需信息：输入、期望输出、评分维度。
    """

    # ── 标识 ──
    id: str = ""                              # 唯一 ID，如 "poyun_ch001_d01"
    source: str = "extracted"                 # "extracted" | "synthesized"
    novel: str = ""                           # 来源小说

    # ── 角色信息 ──
    character_name: str = ""                  # 被测角色
    chapter_start: int = 0                    # 场景所在章节（知识边界）
    chapter_end: int = 0                      # 场景结束章节

    # ── 场景上下文 ──
    scenario_description: str = ""            # 场景自然语言描述
    location: str = ""                        # 场景地点
    involved_characters: list[str] = field(default_factory=list)  # 在场角色

    # ── 对话数据 ──
    speaker_identity: str = ""                 # user_input 的说话人（如"杨媚"）
    conversation_context: list[dict] = field(default_factory=list)
    # [{"speaker": "杨媚", "role": "对方", "content": "..."}, ...]
    user_input: str = ""                      # 用户对角色说的话
    golden_response: str = ""                 # 原著中的实际回复（extracted 时有效）

    # ── 评估标准 ──
    evaluation_dimensions: list[str] = field(default_factory=list)
    # e.g. ["character_consistency", "voice_fidelity", "emotion_dynamics",
    #       "knowledge_boundary", "relationship_accuracy"]

    expected_behaviors: list[str] = field(default_factory=list)
    """行为期望描述，每项是 '维度: 期望描述' 格式：
    - "character_consistency: 江停在生死关头仍然保持冷静克制"
    - "knowledge_boundary: 不应知道严峫的真实身份背景"
    """

    forbidden_behaviors: list[str] = field(default_factory=list)
    """绝对不能出现的行为：
    - "voice_fidelity: 不能说脏话或粗口"
    - "knowledge_boundary: 不应提及第10章之后的事件"
    """

    # ── 元数据 ──
    difficulty: str = "medium"                # "easy" | "medium" | "hard"
    tags: list[str] = field(default_factory=list)
    # e.g. ["emotional_stress", "relationship_test", "ooc_resistance"]

    # ── 关系上下文 ──
    target_relationship: Optional[dict] = None
    """如果测试涉及角色关系:
    {"target": "严峫", "relation_type": "搭档", "intimacy": 3}
    """

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.target_relationship is None:
            d["target_relationship"] = None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "GoldenCase":
        # 过滤掉不在 fields 里的 key
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**valid)


@dataclass
class GoldenDataset:
    """一条完整的 Golden 评估数据集。"""

    name: str = ""                            # 数据集名称
    novel: str = ""                           # 来源小说
    created_at: str = ""                      # ISO 时间戳
    total_cases: int = 0

    cases: list[GoldenCase] = field(default_factory=list)

    # 统计
    dimension_distribution: dict = field(default_factory=dict)
    difficulty_distribution: dict = field(default_factory=dict)
    character_distribution: dict = field(default_factory=dict)

    def add_case(self, case: GoldenCase):
        self.cases.append(case)
        self.total_cases = len(self.cases)
        self._update_stats()

    def _update_stats(self):
        """更新分布统计。"""
        dims = {}
        diffs = {}
        chars = {}
        for c in self.cases:
            for dim in c.evaluation_dimensions:
                dims[dim] = dims.get(dim, 0) + 1
            diffs[c.difficulty] = diffs.get(c.difficulty, 0) + 1
            chars[c.character_name] = chars.get(c.character_name, 0) + 1
        self.dimension_distribution = dims
        self.difficulty_distribution = diffs
        self.character_distribution = chars

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "novel": self.novel,
            "created_at": self.created_at,
            "total_cases": self.total_cases,
            "dimension_distribution": self.dimension_distribution,
            "difficulty_distribution": self.difficulty_distribution,
            "character_distribution": self.character_distribution,
            "cases": [c.to_dict() for c in self.cases],
        }

    def to_json(self, path: str):
        import json
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, d: dict) -> "GoldenDataset":
        dataset = cls(
            name=d.get("name", ""),
            novel=d.get("novel", ""),
            created_at=d.get("created_at", ""),
            total_cases=d.get("total_cases", 0),
            dimension_distribution=d.get("dimension_distribution", {}),
            difficulty_distribution=d.get("difficulty_distribution", {}),
            character_distribution=d.get("character_distribution", {}),
        )
        for c in d.get("cases", []):
            dataset.cases.append(GoldenCase.from_dict(c))
        return dataset

    @classmethod
    def from_json(cls, path: str) -> "GoldenDataset":
        import json
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))

    def summary(self) -> str:
        """打印数据集概览。"""
        lines = [
            f"GoldenDataset: {self.name}",
            f"  小说: {self.novel}",
            f"  用例总数: {self.total_cases}",
            f"  角色分布: {self.character_distribution}",
            f"  难度分布: {self.difficulty_distribution}",
            f"  维度分布: {self.dimension_distribution}",
        ]
        return "\n".join(lines)
