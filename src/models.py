# -*- coding: utf-8 -*-
"""Novel2Comic V2 数据模型——所有 dataclass 定义 + JSON 序列化。"""

import os
from dataclasses import dataclass, field, asdict
from typing import Optional
from datetime import datetime
import json


@dataclass
class StyleProfile:
    name: str                    # "manga" | "webtoon" | "gufeng"
    color_mode: str              # "bw_screentone" | "full_color" | "ink_wash"
    reading_direction: str       # "rtl_page" | "vertical_scroll" | "flexible"
    aspect_ratio: str            # "16:9" | "9:16" | "4:3" | "1:1"
    sd_base_prompt: str          # 注入每张图的风格基座
    speech_bubble_style: str     # 对话框样式
    sfx_style: str               # 特效字样式
    layout_mode: str             # "grid" (Manga 格阵) | "scroll" (条漫竖拼)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "StyleProfile":
        return cls(**d)


@dataclass
class CharacterAppearance:
    face: str = ""
    hair: str = ""
    build: str = ""
    clothing: str = ""
    accessories: str = ""
    distinctive_features: str = ""


@dataclass
class CharacterSheet:
    id: str
    name: str
    role: str
    appearance: CharacterAppearance = field(default_factory=CharacterAppearance)
    reference_image_path: str = ""
    sd_trigger_words: str = ""
    personality_notes: str = ""
    status: str = "draft"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["appearance"] = asdict(self.appearance)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "CharacterSheet":
        appearance = CharacterAppearance(**d.pop("appearance", {}))
        return cls(appearance=appearance, **d)


@dataclass
class AnalysisResult:
    genre_tags: list[str] = field(default_factory=list)
    style: str = "auto"
    tone: list[str] = field(default_factory=list)
    era: str = ""
    pace: str = ""
    characters_preview: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AnalysisResult":
        return cls(**d)


@dataclass
class Panel:
    panel_number: int = 0
    visual_description: str = ""
    character_action: str = ""
    dialogue: str = ""
    camera_angle: str = ""
    mood: str = ""
    sd_prompt: str = ""
    character_refs: list[str] = field(default_factory=list)
    generated_image_path: str = ""
    status: str = "pending"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Panel":
        return cls(**d)


@dataclass
class Scene:
    id: int = 0
    title: str = ""
    summary: str = ""
    characters_in_scene: list[str] = field(default_factory=list)
    emotion_arc: str = ""
    key_dialogue: str = ""
    panels: list[Panel] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["panels"] = [p.to_dict() for p in self.panels]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Scene":
        panels = [Panel.from_dict(p) for p in d.pop("panels", [])]
        return cls(panels=panels, **d)


@dataclass
class ComicPage:
    page_number: int = 0
    image_path: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ComicPage":
        return cls(**d)


# ============================================================
# 知识图谱
# ============================================================

@dataclass
class CharacterNode:
    """角色节点。"""
    id: str = ""                              # 唯一标识
    name: str = ""                            # 中文名
    role_type: str = ""                       # "protagonist" | "antagonist" | "supporting" | "minor"
    faction: str = ""                         # 所属势力/阵营
    importance: int = 5                       # 1-10 重要程度
    first_appearance_chapter: int = 0
    status: str = "active"                    # "active" | "dead" | "missing" | "unknown"
    description: str = ""                     # 一句话描述

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CharacterNode":
        return cls(**d)


@dataclass
class RelationshipEdge:
    """关系边。"""
    from_char: str = ""                       # → CharacterNode.id
    to_char: str = ""
    relation_type: str = ""                   # "血缘"|"爱情"|"友情"|"敌对"|"师徒"|"主仆"|"利用"|"同盟"
    sub_type: str = ""                        # "暗恋"|"杀父之仇"|"青梅竹马"|"背叛"|...
    intimacy: int = 0                         # -10(不共戴天) ~ +10(生死相依)
    power_dynamic: str = "平等"               # "平等"|"A主导"|"B主导"|"互相制衡"
    public_knowledge: bool = True             # 关系是否公开
    current_tension: str = "和谐"             # "和谐"|"紧张"|"暧昧"|"一触即发"|"冷战"
    shared_history: str = ""                  # 共同经历摘要
    established_chapter: int = 0              # 关系建立的章

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RelationshipEdge":
        return cls(**d)


@dataclass
class RelationEvent:
    """关系变化事件——追踪关系随时间演变。"""
    chapter: int = 0
    from_char: str = ""
    to_char: str = ""
    field: str = ""                           # 变化的字段
    old_value: str = ""
    new_value: str = ""
    trigger_event: str = ""                   # 触发事件描述
    timestamp: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RelationEvent":
        return cls(**d)


@dataclass
class EventNode:
    """事件节点——小说中的关键剧情事件。"""
    id: str = ""
    name: str = ""                            # 事件名，如"三年之约大战"
    event_type: str = ""                      # 战斗|对话|转折|修炼|获得物品|情感|阴谋|日常|其他
    chapter_start: int = 0
    chapter_end: int = 0
    location: str = ""                        # 发生地点名
    participants: list[dict] = field(default_factory=list)
    # [{name, role: 主导|参与|旁观|受害, outcome}]
    cause: str = ""                           # 前因
    effect: str = ""                          # 后果
    summary: str = ""                         # 一句话摘要
    importance: int = 5                       # 1-10

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "EventNode":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class LocationNode:
    """地点节点——故事发生的地理位置。"""
    id: str = ""
    name: str = ""                            # 地名
    location_type: str = ""                   # 世界|大陆|国家|城市|宗门|秘境|具体场所|其他
    parent: str = ""                          # 父级地名（层级关系）
    description: str = ""                     # 地点描述
    factions: list[str] = field(default_factory=list)  # 控制此地的势力
    first_appear_chapter: int = 0
    is_destroyed: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "LocationNode":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class OrganizationNode:
    """组织/势力节点。"""
    id: str = ""
    name: str = ""                            # 组织名，如"萧家""云岚宗"
    org_type: str = ""                        # 家族|宗门|帝国|佣兵团|商盟|其他
    leader: list[str] = field(default_factory=list)   # 首领列表
    members: list[str] = field(default_factory=list)  # 核心成员
    base: str = ""                            # 总舵/总部地点名
    status: str = "鼎盛"                      # 鼎盛|衰落|已灭|发展中
    description: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "OrganizationNode":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class ItemNode:
    """物品/功法节点——小说中的关键物品、功法、法宝等。"""
    id: str = ""
    name: str = ""                            # 物品名，如"玄重尺""焚诀"
    item_type: str = ""                       # 功法|斗技|丹药|武器|法宝|天材地宝|其他
    grade: str = ""                           # 品阶，如"地阶低级"
    owner_history: list[dict] = field(default_factory=list)
    # [{person, chapter_start, chapter_end}]
    abilities: list[str] = field(default_factory=list)  # 能力/效果
    source: str = ""                          # 获得来源
    description: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ItemNode":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class ChapterNode:
    """章节节点——小说章节的结构化表示。"""
    index: int = 0
    title: str = ""
    summary: str = ""                         # 章节摘要
    key_events: list[str] = field(default_factory=list)  # 本章关键事件名列表
    appearing_characters: list[str] = field(default_factory=list)
    word_count: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ChapterNode":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ============================================================
# 扩展边类型
# ============================================================

@dataclass
class AppearsInEdge:
    """人物出场边：Person → Chapter。"""
    person: str = ""
    chapter: int = 0
    role: str = "出场"                        # 出场|提及

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AppearsInEdge":
        return cls(**d)


@dataclass
class ParticipatesEdge:
    """人物参与事件边：Person → Event。"""
    person: str = ""
    event: str = ""
    role: str = "参与"                        # 主导|参与|旁观|受害
    outcome: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ParticipatesEdge":
        return cls(**d)


@dataclass
class OccursInEdge:
    """事件发生于章节边：Event → Chapter。"""
    event: str = ""
    chapter_start: int = 0
    chapter_end: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "OccursInEdge":
        return cls(**d)


@dataclass
class LocatedAtEdge:
    """事件发生于地点边：Event → Location。"""
    event: str = ""
    location: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "LocatedAtEdge":
        return cls(**d)


@dataclass
class BelongsToEdge:
    """人物属于组织边：Person → Organization。"""
    person: str = ""
    organization: str = ""
    role: str = "成员"                        # 首领|成员|客卿|长老

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "BelongsToEdge":
        return cls(**d)


@dataclass
class OwnsEdge:
    """人物拥有物品边：Person → Item。"""
    person: str = ""
    item: str = ""
    chapter_start: int = 0
    chapter_end: int = 0                      # 0 = 仍持有

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "OwnsEdge":
        return cls(**d)


@dataclass
class EventRelationEdge:
    """事件关系边：Event → Event。"""
    from_event: str = ""
    to_event: str = ""
    relation_type: str = "before"             # before|after|causes|part_of

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "EventRelationEdge":
        return cls(**d)


@dataclass
class LocationHierarchyEdge:
    """地点层级边：子地点 → 父地点。"""
    child: str = ""
    parent: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "LocationHierarchyEdge":
        return cls(**d)


# ============================================================
# StoryGraph —— 完整故事知识图谱
# ============================================================

# 节点类型 → Dataclass 映射
_NODE_TYPE_MAP = {
    "person": CharacterNode,
    "event": EventNode,
    "location": LocationNode,
    "org": OrganizationNode,
    "item": ItemNode,
    "chapter": ChapterNode,
}

# 边类型 → Dataclass 映射
_EDGE_TYPE_MAP = {
    "relationship": RelationshipEdge,
    "appears_in": AppearsInEdge,
    "participates": ParticipatesEdge,
    "occurs_in": OccursInEdge,
    "located_at": LocatedAtEdge,
    "belongs_to": BelongsToEdge,
    "owns": OwnsEdge,
    "event_relation": EventRelationEdge,
    "location_hierarchy": LocationHierarchyEdge,
}


@dataclass
class StoryGraph:
    """完整故事知识图谱——基于 NetworkX MultiDiGraph。

    支持异构节点（人物/事件/地点/组织/物品/章节）和多种边类型。
    内部节点 key 格式："{type}:{name}"，确保不同类型同名实体不冲突。
    """
    last_updated_chapter: int = 0
    timeline: list[RelationEvent] = field(default_factory=list)
    _schema_version: int = 2

    def __post_init__(self):
        import networkx as nx
        self._g = nx.MultiDiGraph()

    # ================================================================
    # Key 工具
    # ================================================================

    @staticmethod
    def _key(node_type: str, name: str) -> str:
        return f"{node_type}:{name}"

    @staticmethod
    def _parse_key(key: str) -> tuple:
        parts = key.split(":", 1)
        if len(parts) == 2:
            return parts[0], parts[1]
        return "person", key  # 兼容旧格式（无前缀）

    # ================================================================
    # 通用节点操作
    # ================================================================

    def _add_typed_node(self, node_type: str, node):
        """添加任意类型的节点。"""
        key = self._key(node_type, node.name if hasattr(node, 'name') else str(node.index))
        attrs = {k: v for k, v in asdict(node).items()}
        attrs["node_type"] = node_type
        self._g.add_node(key, **attrs)

    def _get_typed_node(self, node_type: str, name: str):
        """获取任意类型的节点。"""
        key = self._key(node_type, name)
        if key not in self._g:
            return None
        attrs = dict(self._g.nodes[key])
        attrs.pop("node_type", None)
        cls = _NODE_TYPE_MAP.get(node_type)
        if cls:
            # 过滤 dataclass 支持的字段
            valid_fields = cls.__dataclass_fields__
            filtered = {k: v for k, v in attrs.items() if k in valid_fields}
            return cls(**filtered)
        return None

    def _get_nodes_by_type(self, node_type: str) -> list:
        """获取指定类型的所有节点。"""
        prefix = f"{node_type}:"
        cls = _NODE_TYPE_MAP.get(node_type)
        result = []
        for key in self._g.nodes:
            if key.startswith(prefix):
                name = key[len(prefix):]
                node = self._get_typed_node(node_type, name)
                if node:
                    result.append(node)
        return result

    # ================================================================
    # 人物节点（保持与 CharacterGraph 兼容的 API）
    # ================================================================

    def add_person_node(self, node: CharacterNode):
        self._add_typed_node("person", node)

    def get_person_node(self, name: str):
        return self._get_typed_node("person", name)

    @property
    def person_nodes(self) -> list:
        return self._get_nodes_by_type("person")

    # ================================================================
    # 事件节点
    # ================================================================

    def add_event_node(self, node: EventNode):
        self._add_typed_node("event", node)

    def get_event_node(self, name: str):
        return self._get_typed_node("event", name)

    @property
    def event_nodes(self) -> list:
        return self._get_nodes_by_type("event")

    # ================================================================
    # 地点节点
    # ================================================================

    def add_location_node(self, node: LocationNode):
        self._add_typed_node("location", node)

    def get_location_node(self, name: str):
        return self._get_typed_node("location", name)

    @property
    def location_nodes(self) -> list:
        return self._get_nodes_by_type("location")

    # ================================================================
    # 组织节点
    # ================================================================

    def add_org_node(self, node: OrganizationNode):
        self._add_typed_node("org", node)

    def get_org_node(self, name: str):
        return self._get_typed_node("org", name)

    @property
    def org_nodes(self) -> list:
        return self._get_nodes_by_type("org")

    # ================================================================
    # 物品节点
    # ================================================================

    def add_item_node(self, node: ItemNode):
        self._add_typed_node("item", node)

    def get_item_node(self, name: str):
        return self._get_typed_node("item", name)

    @property
    def item_nodes(self) -> list:
        return self._get_nodes_by_type("item")

    # ================================================================
    # 章节节点
    # ================================================================

    def add_chapter_node(self, node: ChapterNode):
        self._add_typed_node("chapter", str(node.index))

    def get_chapter_node(self, index: int):
        return self._get_typed_node("chapter", str(index))

    @property
    def chapter_nodes(self) -> list:
        return self._get_nodes_by_type("chapter")

    # ================================================================
    # 节点计数
    # ================================================================

    @property
    def total_node_count(self) -> int:
        return self._g.number_of_nodes()

    def node_type_counts(self) -> dict:
        counts = {}
        for key in self._g.nodes:
            nt = key.split(":", 1)[0] if ":" in key else "person"
            counts[nt] = counts.get(nt, 0) + 1
        return counts

    # ================================================================
    # 通用边操作
    # ================================================================

    def _add_typed_edge(self, edge_type: str, from_key: str, to_key: str, edge):
        """添加任意类型的边。"""
        attrs = {k: v for k, v in asdict(edge).items()}
        attrs["edge_type"] = edge_type
        # MultiDiGraph 需要唯一 key，用 edge_type + 序号确保不冲突
        self._g.add_edge(from_key, to_key, key=edge_type, **attrs)

    def _get_edges_by_type(self, edge_type: str) -> list:
        """获取指定类型的所有边。"""
        cls = _EDGE_TYPE_MAP.get(edge_type)
        result = []
        for u, v, k, data in self._g.edges(data=True, keys=True):
            if data.get("edge_type") == edge_type:
                if cls:
                    valid_fields = cls.__dataclass_fields__
                    filtered = {k2: v2 for k2, v2 in data.items()
                               if k2 in valid_fields and k2 != "edge_type"}
                    result.append(cls(**filtered))
        return result

    # ================================================================
    # 人物关系边（与 CharacterGraph 兼容）
    # ================================================================

    def add_relationship_edge(self, edge: RelationshipEdge):
        from_key = self._key("person", edge.from_char)
        to_key = self._key("person", edge.to_char)
        # 确保两端节点存在
        if from_key not in self._g:
            self._g.add_node(from_key, node_type="person", name=edge.from_char)
        if to_key not in self._g:
            self._g.add_node(to_key, node_type="person", name=edge.to_char)
        # 记录变化到 timeline
        existing = self.get_relationship_edge(edge.from_char, edge.to_char)
        if existing:
            for field_name in ["relation_type", "sub_type", "intimacy", "power_dynamic",
                               "public_knowledge", "current_tension", "shared_history"]:
                new_val = getattr(edge, field_name, None)
                default_map = {"intimacy": 0, "power_dynamic": "平等",
                               "current_tension": "和谐", "public_knowledge": True}
                default = default_map.get(field_name)
                if new_val not in (None, "", default):
                    old_val = getattr(existing, field_name)
                    if str(old_val) != str(new_val):
                        self.timeline.append(RelationEvent(
                            chapter=edge.established_chapter,
                            from_char=edge.from_char, to_char=edge.to_char,
                            field=field_name, old_value=str(old_val),
                            new_value=str(new_val),
                        ))
        self._g.add_edge(from_key, to_key, key="relationship",
            relation_type=edge.relation_type, sub_type=edge.sub_type,
            intimacy=edge.intimacy, power_dynamic=edge.power_dynamic,
            public_knowledge=edge.public_knowledge, current_tension=edge.current_tension,
            shared_history=edge.shared_history, established_chapter=edge.established_chapter,
            edge_type="relationship",
        )

    def get_relationship_edge(self, from_char: str, to_char: str):
        from_key = self._key("person", from_char)
        to_key = self._key("person", to_char)
        if not self._g.has_edge(from_key, to_key):
            return None
        # MultiDiGraph: 可能有多个边，找到 relationship 类型的
        for k, data in self._g.get_edge_data(from_key, to_key).items():
            if data.get("edge_type") == "relationship":
                return RelationshipEdge(
                    from_char=from_char, to_char=to_char,
                    relation_type=data.get("relation_type", ""),
                    sub_type=data.get("sub_type", ""),
                    intimacy=data.get("intimacy", 0),
                    power_dynamic=data.get("power_dynamic", "平等"),
                    public_knowledge=data.get("public_knowledge", True),
                    current_tension=data.get("current_tension", "和谐"),
                    shared_history=data.get("shared_history", ""),
                    established_chapter=data.get("established_chapter", 0),
                )
        return None

    @property
    def relationship_edges(self) -> list:
        return self._get_edges_by_type("relationship")

    # ================================================================
    # 其他边类型（便捷方法）
    # ================================================================

    def add_appears_in_edge(self, edge: AppearsInEdge):
        from_key = self._key("person", edge.person)
        to_key = self._key("chapter", str(edge.chapter))
        self._ensure_node_exists(from_key, "person", edge.person)
        self._ensure_node_exists(to_key, "chapter", str(edge.chapter))
        self._g.add_edge(from_key, to_key, key="appears_in",
            person=edge.person, chapter=edge.chapter, role=edge.role,
            edge_type="appears_in",
        )

    def add_participates_edge(self, edge: ParticipatesEdge):
        from_key = self._key("person", edge.person)
        to_key = self._key("event", edge.event)
        self._ensure_node_exists(from_key, "person", edge.person)
        self._ensure_node_exists(to_key, "event", edge.event)
        self._g.add_edge(from_key, to_key, key="participates",
            person=edge.person, event=edge.event, role=edge.role,
            outcome=edge.outcome, edge_type="participates",
        )

    def add_occurs_in_edge(self, edge: OccursInEdge):
        from_key = self._key("event", edge.event)
        to_key = self._key("chapter", str(edge.chapter_start))
        self._ensure_node_exists(from_key, "event", edge.event)
        self._ensure_node_exists(to_key, "chapter", str(edge.chapter_start))
        self._g.add_edge(from_key, to_key, key="occurs_in",
            event=edge.event, chapter_start=edge.chapter_start,
            chapter_end=edge.chapter_end, edge_type="occurs_in",
        )

    def add_located_at_edge(self, edge: LocatedAtEdge):
        from_key = self._key("event", edge.event)
        to_key = self._key("location", edge.location)
        self._ensure_node_exists(from_key, "event", edge.event)
        self._ensure_node_exists(to_key, "location", edge.location)
        self._g.add_edge(from_key, to_key, key="located_at",
            event=edge.event, location=edge.location, edge_type="located_at",
        )

    def add_belongs_to_edge(self, edge: BelongsToEdge):
        from_key = self._key("person", edge.person)
        to_key = self._key("org", edge.organization)
        self._ensure_node_exists(from_key, "person", edge.person)
        self._ensure_node_exists(to_key, "org", edge.organization)
        self._g.add_edge(from_key, to_key, key="belongs_to",
            person=edge.person, organization=edge.organization,
            role=edge.role, edge_type="belongs_to",
        )

    def add_owns_edge(self, edge: OwnsEdge):
        from_key = self._key("person", edge.person)
        to_key = self._key("item", edge.item)
        self._ensure_node_exists(from_key, "person", edge.person)
        self._ensure_node_exists(to_key, "item", edge.item)
        self._g.add_edge(from_key, to_key, key="owns",
            person=edge.person, item=edge.item,
            chapter_start=edge.chapter_start, chapter_end=edge.chapter_end,
            edge_type="owns",
        )

    def add_event_relation_edge(self, edge: EventRelationEdge):
        from_key = self._key("event", edge.from_event)
        to_key = self._key("event", edge.to_event)
        self._ensure_node_exists(from_key, "event", edge.from_event)
        self._ensure_node_exists(to_key, "event", edge.to_event)
        self._g.add_edge(from_key, to_key, key=f"event_rel_{edge.relation_type}",
            from_event=edge.from_event, to_event=edge.to_event,
            relation_type=edge.relation_type, edge_type="event_relation",
        )

    def add_location_hierarchy_edge(self, edge: LocationHierarchyEdge):
        from_key = self._key("location", edge.child)
        to_key = self._key("location", edge.parent)
        self._ensure_node_exists(from_key, "location", edge.child)
        self._ensure_node_exists(to_key, "location", edge.parent)
        self._g.add_edge(from_key, to_key, key="location_hierarchy",
            child=edge.child, parent=edge.parent, edge_type="location_hierarchy",
        )

    def _ensure_node_exists(self, key: str, node_type: str, name: str):
        """确保节点存在于图中，不存在则创建占位节点。"""
        if key not in self._g:
            self._g.add_node(key, node_type=node_type, name=name)

    # ================================================================
    # 各类型边查询
    # ================================================================

    @property
    def appears_in_edges(self) -> list:
        return self._get_edges_by_type("appears_in")

    @property
    def participates_edges(self) -> list:
        return self._get_edges_by_type("participates")

    @property
    def occurs_in_edges(self) -> list:
        return self._get_edges_by_type("occurs_in")

    @property
    def located_at_edges(self) -> list:
        return self._get_edges_by_type("located_at")

    @property
    def belongs_to_edges(self) -> list:
        return self._get_edges_by_type("belongs_to")

    @property
    def owns_edges(self) -> list:
        return self._get_edges_by_type("owns")

    @property
    def event_relation_edges(self) -> list:
        return self._get_edges_by_type("event_relation")

    @property
    def location_hierarchy_edges(self) -> list:
        return self._get_edges_by_type("location_hierarchy")

    @property
    def total_edge_count(self) -> int:
        return self._g.number_of_edges()

    # ================================================================
    # 高级查询
    # ================================================================

    def event_timeline(self) -> list[EventNode]:
        """按章节顺序返回事件时间线。"""
        events = self.event_nodes
        events.sort(key=lambda e: (e.chapter_start, -e.importance))
        return events

    def character_events(self, name: str) -> list[dict]:
        """查询某角色的所有参与事件。"""
        results = []
        for edge in self.participates_edges:
            if edge.person == name:
                event = self.get_event_node(edge.event)
                if event:
                    results.append({
                        "event": edge.event,
                        "role": edge.role,
                        "outcome": edge.outcome,
                        "chapter_start": event.chapter_start,
                        "importance": event.importance,
                        "summary": event.summary,
                    })
        results.sort(key=lambda x: x["chapter_start"])
        return results

    def location_hierarchy(self) -> dict:
        """返回地点层级树。"""
        children: dict[str, list[str]] = {}
        all_locations = {n.name for n in self.location_nodes}
        roots = set(all_locations)

        for edge in self.location_hierarchy_edges:
            if edge.parent not in children:
                children[edge.parent] = []
            children[edge.parent].append(edge.child)
            roots.discard(edge.child)

        return {
            "roots": list(roots),
            "children": children,
        }

    def org_members(self, org_name: str) -> dict:
        """查询组织及其成员。"""
        org = self.get_org_node(org_name)
        if not org:
            return {}
        members_from_edges = []
        for edge in self.belongs_to_edges:
            if edge.organization == org_name:
                members_from_edges.append({"name": edge.person, "role": edge.role})
        return {
            "org": org,
            "members_from_edges": members_from_edges,
            "declared_members": org.members,
            "declared_leaders": org.leader,
        }

    def item_owners(self, item_name: str) -> list[dict]:
        """查询物品的归属历史。"""
        item = self.get_item_node(item_name)
        results = []
        for edge in self.owns_edges:
            if edge.item == item_name:
                results.append({
                    "person": edge.person,
                    "chapter_start": edge.chapter_start,
                    "chapter_end": edge.chapter_end,
                })
        results.sort(key=lambda x: x["chapter_start"])
        if item and item.owner_history:
            results.extend(item.owner_history)
        return results

    # ================================================================
    # 图算法（复用 NetworkX）
    # ================================================================

    def shortest_path(self, from_name: str, to_name: str,
                      from_type: str = "person", to_type: str = "person") -> list:
        """两实体之间的最短路径。"""
        import networkx as nx
        try:
            u = self._key(from_type, from_name)
            v = self._key(to_type, to_name)
            # 用无向版本找路径（忽略边方向）
            ug = self._g.to_undirected()
            return nx.shortest_path(ug, u, v)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return []

    def centrality_ranking(self, top_k: int = 10) -> list:
        """节点中心度排名。"""
        import networkx as nx
        ug = self._g.to_undirected()
        dc = nx.degree_centrality(ug)
        return sorted(dc.items(), key=lambda x: x[1], reverse=True)[:top_k]

    def enemy_pairs(self) -> list:
        """列出所有敌对关系（人物间）。"""
        return [(e.from_char, e.to_char) for e in self.relationship_edges
                if e.relation_type == "敌对"]

    def faction_groups(self) -> dict:
        """按阵营分组人物。"""
        groups: dict[str, list[str]] = {}
        for node in self.person_nodes:
            faction = node.faction or "无阵营"
            if faction not in groups:
                groups[faction] = []
            groups[faction].append(node.name)
        return groups

    # ================================================================
    # 分镜指导（从人物关系推导）
    # ================================================================

    def get_storyboard_hints(self, char_a: str, char_b: str) -> str:
        """根据两个角色的关系生成分镜指导提示。"""
        edge = self.get_relationship_edge(char_a, char_b)
        if not edge:
            edge = self.get_relationship_edge(char_b, char_a)
        if not edge:
            return ""

        hints = []
        if edge.intimacy >= 7:
            hints.append("两人亲近，同框时距离近，用双人中近景，眼神交流，柔和光线")
        elif edge.intimacy <= -7:
            hints.append("两人敌对，同框时用对峙构图、低角度仰拍、特写眼神交锋、sd_prompt加'dramatic shadows'")

        if edge.power_dynamic == "A主导":
            hints.append(f"{edge.from_char}是上位者→仰拍显高大, {edge.to_char}俯拍显弱小")
        elif edge.power_dynamic == "B主导":
            hints.append(f"{edge.to_char}是上位者→仰拍显高大, {edge.from_char}俯拍显弱小")

        if not edge.public_knowledge:
            hints.append("关系隐藏→公开场合两人站远、表情克制、只在对视瞬间流露微表情")

        tension_map = {
            "暧昧": "避免直视、侧脸和偷看视角、sd_prompt加'shy glance, soft focus'",
            "紧张": "身体语言僵硬、避免眼神接触、画面留白营造窒息感",
            "一触即发": "动作预备姿态、面部紧绷、sd_prompt加'tense atmosphere, ready to strike'",
            "冷战": "背对背站位、各自看向不同方向、中间留空",
        }
        if edge.current_tension in tension_map:
            hints.append(tension_map[edge.current_tension])

        type_hints = {
            "爱情": "关注手部细节和微表情, sd_prompt加'romantic atmosphere'",
            "敌对": "多用斜线构图和速度线, sd_prompt加'confrontation'",
            "师徒": "A略高于B的站位、B带敬意的眼神",
        }
        if edge.relation_type in type_hints:
            hints.append(type_hints[edge.relation_type])

        return " | ".join(hints)

@dataclass
class ChapterInfo:
    """章节元数据——从小说中解析出的章节信息。"""
    index: int = 0                  # 第几章 (1-based)
    title: str = ""                 # 章节标题
    content: str = ""               # 章节正文
    word_count: int = 0             # 字数
    status: str = "pending"         # "pending" | "generating" | "completed"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ChapterInfo":
        return cls(**d)


@dataclass
class Novel:
    """全书——顶层数据模型，包含章节列表和跨章节共享的角色库。"""
    title: str = ""                          # 书名
    file_path: str = ""                      # 原始文件路径
    chapters: list[ChapterInfo] = field(default_factory=list)  # 章节列表
    characters: list[CharacterSheet] = field(default_factory=list)  # 全书角色库（跨章节共享）
    story_graph: Optional[StoryGraph] = None     # 完整故事知识图谱（v2）
    style_profile: Optional[StyleProfile] = None  # 全书风格（首次分析后锁定）
    current_chapter_index: int = 0           # 当前选中的章节 (1-based)
    output_dir: str = ""

    @property
    def current_chapter(self) -> Optional[ChapterInfo]:
        """当前选中的章节。"""
        for ch in self.chapters:
            if ch.index == self.current_chapter_index:
                return ch
        return None

    @property
    def total_chapters(self) -> int:
        return len(self.chapters)

    def get_characters_by_name(self, name: str) -> list[CharacterSheet]:
        """按名称查找角色（支持模糊匹配）。"""
        return [c for c in self.characters if c.name == name]

    def has_character(self, name: str) -> bool:
        return any(c.name == name for c in self.characters)

    def add_characters(self, new_chars: list[CharacterSheet]):
        """添加角色到全书库（同名跳过）。"""
        existing = {c.name for c in self.characters}
        for char in new_chars:
            if char.name not in existing:
                self.characters.append(char)
                existing.add(char.name)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "file_path": self.file_path,
            "chapters": [ch.to_dict() for ch in self.chapters],
            "characters": [c.to_dict() for c in self.characters],
            "story_graph": self.story_graph.to_dict() if self.story_graph else None,
            "style_profile": self.style_profile.to_dict() if self.style_profile else None,
            "current_chapter_index": self.current_chapter_index,
            "output_dir": self.output_dir,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Novel":
        novel = cls(
            title=d.get("title", ""),
            file_path=d.get("file_path", ""),
            current_chapter_index=d.get("current_chapter_index", 0),
            output_dir=d.get("output_dir", ""),
        )
        novel.chapters = [ChapterInfo.from_dict(ch) for ch in d.get("chapters", [])]
        novel.characters = [CharacterSheet.from_dict(c) for c in d.get("characters", [])]
        if d.get("story_graph"):
            novel.story_graph = StoryGraph.from_dict(d["story_graph"])
        elif d.get("character_graph"):
            # 迁移旧格式
            novel.story_graph = StoryGraph.from_dict(d["character_graph"])
        if d.get("style_profile"):
            novel.style_profile = StyleProfile.from_dict(d["style_profile"])
        return novel

    def save(self, filepath: str):
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, filepath: str) -> "Novel":
        with open(filepath, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))


@dataclass
class ChapterData:
    """Pipeline 数据总线——单章生成的共享状态（6 阶段）。"""
    title: str = ""
    source_text: str = ""
    analysis: Optional[AnalysisResult] = None
    characters: list[CharacterSheet] = field(default_factory=list)
    scenes: list[Scene] = field(default_factory=list)
    pages: list[ComicPage] = field(default_factory=list)
    style_profile: Optional[StyleProfile] = None
    current_stage: int = 0
    created_at: str = ""
    output_dir: str = ""

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "source_text": self.source_text,
            "analysis": self.analysis.to_dict() if self.analysis else None,
            "characters": [c.to_dict() for c in self.characters],
            "scenes": [s.to_dict() for s in self.scenes],
            "pages": [p.to_dict() for p in self.pages],
            "style_profile": self.style_profile.to_dict() if self.style_profile else None,
            "current_stage": self.current_stage,
            "created_at": self.created_at,
            "output_dir": self.output_dir,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ChapterData":
        data = cls(
            title=d.get("title", ""),
            source_text=d.get("source_text", ""),
            current_stage=d.get("current_stage", 0),
            created_at=d.get("created_at", ""),
            output_dir=d.get("output_dir", ""),
        )
        if d.get("analysis"):
            data.analysis = AnalysisResult.from_dict(d["analysis"])
        if d.get("style_profile"):
            data.style_profile = StyleProfile.from_dict(d["style_profile"])
        data.characters = [CharacterSheet.from_dict(c) for c in d.get("characters", [])]
        data.scenes = [Scene.from_dict(s) for s in d.get("scenes", [])]
        data.pages = [ComicPage.from_dict(p) for p in d.get("pages", [])]
        return data

    def save(self, filepath: str):
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, filepath: str) -> "ChapterData":
        with open(filepath, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))
