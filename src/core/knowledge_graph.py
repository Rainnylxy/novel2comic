# -*- coding: utf-8 -*-
"""知识图谱提取引擎——从小说文本中提取结构化故事知识图谱（v2 扩展本体）。"""

import json
from typing import Optional
from .core.models import (
    CharacterNode, RelationshipEdge, RelationEvent,
    StoryGraph,
    EventNode, LocationNode, OrganizationNode, ItemNode,
    AppearsInEdge, ParticipatesEdge, LocatedAtEdge,
    BelongsToEdge, OwnsEdge, EventRelationEdge, LocationHierarchyEdge,
)


# ============================================================
# 提取 Prompt（扩展本体：人物 + 事件 + 地点 + 组织 + 物品 + 跨类型边）
# ============================================================

FULL_EXTRACTION_PROMPT_V2 = """你是一位专业的小说分析师。你需要从小说文本中提取结构化故事知识图谱。

## 人物提取（最高优先级）

人物是知识图谱的核心，必须完整提取。description 字段是最关键的：
**description 必须包含: 职业/身份 + 性格特征 + 角色定位**
例如: "建宁市公安局刑侦支队副队长，经验丰富，观察力敏锐"
**绝对不能只写"主角"或"配角"——必须写明具体的职业身份**

## 返回格式

{
  "characters": [
    {
      "id": "英文id",
      "name": "中文名",
      "role_type": "protagonist|antagonist|supporting|minor",
      "faction": "所属势力/组织/阵营（如'建宁市公安局''XX宗门'）",
      "importance": 1-10（基于出场频率和对主线的推动力）,
      "status": "active|dead|missing|unknown",
      "description": "职业身份 + 性格特征 + 角色定位，一句话写清楚这个角色是干什么的"
    }
  ],
  "relationships": [
    {
      "from_char": "A",
      "to_char": "B",
      "relation_type": "血缘|爱情|友情|敌对|师徒|主仆|利用|同盟|陌生",
      "sub_type": "暗恋|杀父之仇|青梅竹马|背叛|上下级|同事|...",
      "intimacy": -10到10,
      "power_dynamic": "平等|A主导|B主导|互相制衡",
      "public_knowledge": true或false,
      "current_tension": "和谐|紧张|暧昧|一触即发|冷战",
      "shared_history": "共同经历（20字内）"
    }
  ],
  "events": [
    {
      "id": "event_01",
      "name": "事件名",
      "event_type": "战斗|对话|转折|修炼|获得物品|情感|阴谋|日常|其他",
      "chapter_start": 章节号,
      "chapter_end": 章节号,
      "location": "发生地点名",
      "participants": [{"name": "角色名", "role": "主导|参与|旁观|受害", "outcome": "结果"}],
      "cause": "前因",
      "effect": "后果",
      "summary": "一句话摘要",
      "importance": 1-10
    }
  ],
  "locations": [
    {
      "id": "loc_01",
      "name": "地名",
      "location_type": "世界|大陆|国家|城市|宗门|秘境|具体场所|其他",
      "parent": "父级地名（空字符串表示顶层）",
      "description": "描述",
      "factions": ["控制此地的势力"],
      "is_destroyed": false
    }
  ],
  "organizations": [
    {
      "id": "org_01",
      "name": "组织名",
      "org_type": "家族|宗门|帝国|佣兵团|商盟|其他",
      "leader": ["首领名"],
      "members": ["核心成员名"],
      "base": "总部地点名",
      "status": "鼎盛|衰落|已灭|发展中",
      "description": "描述"
    }
  ],
  "items": [
    {
      "id": "item_01",
      "name": "物品名",
      "item_type": "功法|斗技|丹药|武器|法宝|天材地宝|其他",
      "grade": "品阶",
      "owner_history": [{"person": "持有者", "chapter_start": 章节号, "chapter_end": 章节号}],
      "abilities": ["能力描述"],
      "source": "获得来源",
      "description": "描述"
    }
  ],
  "event_relations": [
    {
      "from_event": "事件A名",
      "to_event": "事件B名",
      "relation_type": "before|after|causes|part_of"
    }
  ],
  "location_hierarchy": [
    {
      "child": "子地点",
      "parent": "父地点"
    }
  ]
}

## 规则
1. **人物优先**: 先提取所有人物及其 description（职业+性格），再提取关系，最后才是事件和地点
2. **description 必须写职业**: 每个角色的 description 必须以职业/身份开头。如果是学生就写"XX大学学生"，警察写"XX公安局刑警"，不要写"主角"或"重要角色"
3. **不要漏角色**: 只要在文中出现了名字且有台词或行动的角色，都应该提取。出场少但有名字的角色 importance=2-4
4. relationships 中的 sub_type 要写具体的职业关系（如"同事""上下级""师生"）
5. 只提取文中实际出现的内容，不要凭空创造
6. organizations/items 只在明确出现时才提取，没有则返回空数组
7. 所有 name 字段用中文原名
8. 即使某些类型没有数据，也要返回对应的空数组 []"""


CHAPTER_UPDATE_PROMPT_V2 = """你是一位专业的小说分析师。以下是已有的故事知识图谱摘要，请根据新章节内容更新它。

## 已有图谱摘要
{existing_summary}

## 新章节内容（第{chapter_index}章）
{chapter_text}

## 任务
分析新章节中新增或变化的内容。**重点关注新角色：每个新角色的 description 必须包含职业/身份信息（不能只写"新角色"或"配角"）。**

返回 JSON：

{{
  "new_characters": [
    {{
      "id": "英文id",
      "name": "中文名",
      "role_type": "protagonist|antagonist|supporting|minor",
      "faction": "所属势力或阵营",
      "importance": 1-10,
      "status": "active|dead|missing|unknown",
      "description": "职业身份 + 性格特征（必须写明职业，不能写'新出场角色'）"
    }}
  ],
  "new_relationships": [...],
  "new_events": [...],
  "new_locations": [...],
  "new_organizations": [...],
  "new_items": [...],
  "updated_relationships": [
    {{
      "from_char": "A",
      "to_char": "B",
      "changes": {{
        "intimacy": {{"old": -5, "new": -8, "reason": "..."}},
        "current_tension": {{"old": "和谐", "new": "一触即发", "reason": "..."}}
      }}
    }}
  ],
  "new_event_relations": [...],
  "new_location_hierarchy": [...],
  "chapter_summary": "本章一句话摘要"
}}

每种类型没有变化就返回空数组 []。只返回有实际变化的数据。"""


# ============================================================
# V2 提取函数
# ============================================================

def extract_story_graph_from_text(
    text: str,
    openai_client,
    model: str = "deepseek-chat",
    temperature: float = 0.3,
) -> StoryGraph:
    """从小说文本中提取完整故事知识图谱（v2）。

    一次 LLM 调用提取：人物、关系、事件、地点、组织、物品、跨类型边。

    Args:
        text: 小说文本（可跨多章）
        openai_client: OpenAI 兼容客户端
        model: LLM 模型名
        temperature: 生成温度

    Returns:
        StoryGraph 实例
    """
    response = openai_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": FULL_EXTRACTION_PROMPT_V2},
            {"role": "user", "content": f"请分析以下小说文本，提取完整故事知识图谱：\n\n{text[:12000]}"},
        ],
        temperature=temperature,
        timeout=180,
        max_tokens=8192,
    )

    content = response.choices[0].message.content or ""
    content = _clean_json(content)

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        print(f"[KG] LLM 返回非 JSON，前 300 字符: {content[:300]}")
        return StoryGraph()

    # 空结果警告
    total_entities = sum(len(data.get(k, [])) for k in
        ["characters", "relationships", "events", "locations", "organizations", "items"])
    if total_entities == 0:
        print(f"[KG] LLM 返回空结果，前 300 字符: {content[:300]}")

    graph = StoryGraph()

    # --- 人物 ---
    for nd in data.get("characters", []):
        node = CharacterNode(
            id=nd.get("id", f"char_{len(graph.person_nodes):03d}"),
            name=nd.get("name", ""),
            role_type=nd.get("role_type", ""),
            faction=nd.get("faction", ""),
            importance=nd.get("importance", 5),
            status=nd.get("status", "active"),
            description=nd.get("description", ""),
        )
        graph.add_person_node(node)

    # --- 人物关系 ---
    for ed in data.get("relationships", []):
        edge = RelationshipEdge(
            from_char=ed.get("from_char", ""),
            to_char=ed.get("to_char", ""),
            relation_type=ed.get("relation_type", ""),
            sub_type=ed.get("sub_type", ""),
            intimacy=ed.get("intimacy", 0),
            power_dynamic=ed.get("power_dynamic", "平等"),
            public_knowledge=ed.get("public_knowledge", True),
            current_tension=ed.get("current_tension", "和谐"),
            shared_history=ed.get("shared_history", ""),
        )
        graph.add_relationship_edge(edge)

    # --- 事件 ---
    for ev in data.get("events", []):
        node = EventNode(
            id=ev.get("id", ""),
            name=ev.get("name", ""),
            event_type=ev.get("event_type", ""),
            chapter_start=ev.get("chapter_start", 0),
            chapter_end=ev.get("chapter_end", ev.get("chapter_start", 0)),
            location=ev.get("location", ""),
            participants=ev.get("participants", []),
            cause=ev.get("cause", ""),
            effect=ev.get("effect", ""),
            summary=ev.get("summary", ""),
            importance=ev.get("importance", 5),
        )
        graph.add_event_node(node)
        # 从 participants 生成参加边
        for p in ev.get("participants", []):
            pe = ParticipatesEdge(
                person=p.get("name", ""),
                event=ev.get("name", ""),
                role=p.get("role", "参与"),
                outcome=p.get("outcome", ""),
            )
            graph.add_participates_edge(pe)
        # 从 location 生成地点边
        if ev.get("location"):
            le = LocatedAtEdge(event=ev.get("name", ""), location=ev["location"])
            graph.add_located_at_edge(le)

    # --- 地点 ---
    for lo in data.get("locations", []):
        node = LocationNode(
            id=lo.get("id", ""),
            name=lo.get("name", ""),
            location_type=lo.get("location_type", ""),
            parent=lo.get("parent", ""),
            description=lo.get("description", ""),
            factions=lo.get("factions", []),
            is_destroyed=lo.get("is_destroyed", False),
        )
        graph.add_location_node(node)

    # --- 组织 ---
    for og in data.get("organizations", []):
        node = OrganizationNode(
            id=og.get("id", ""),
            name=og.get("name", ""),
            org_type=og.get("org_type", ""),
            leader=og.get("leader", []),
            members=og.get("members", []),
            base=og.get("base", ""),
            status=og.get("status", "鼎盛"),
            description=og.get("description", ""),
        )
        graph.add_org_node(node)
        # 从 leader/members 生成归属边
        for leader_name in og.get("leader", []):
            be = BelongsToEdge(person=leader_name, organization=og.get("name", ""), role="首领")
            graph.add_belongs_to_edge(be)
        for member_name in og.get("members", []):
            be = BelongsToEdge(person=member_name, organization=og.get("name", ""), role="成员")
            graph.add_belongs_to_edge(be)

    # --- 物品 ---
    for it in data.get("items", []):
        node = ItemNode(
            id=it.get("id", ""),
            name=it.get("name", ""),
            item_type=it.get("item_type", ""),
            grade=it.get("grade", ""),
            owner_history=it.get("owner_history", []),
            abilities=it.get("abilities", []),
            source=it.get("source", ""),
            description=it.get("description", ""),
        )
        graph.add_item_node(node)
        # 从 owner_history 生成拥有边
        for oh in it.get("owner_history", []):
            oe = OwnsEdge(
                person=oh.get("person", ""),
                item=it.get("name", ""),
                chapter_start=oh.get("chapter_start", 0),
                chapter_end=oh.get("chapter_end", 0),
            )
            graph.add_owns_edge(oe)

    # --- 事件关系 ---
    for er in data.get("event_relations", []):
        edge = EventRelationEdge(
            from_event=er.get("from_event", ""),
            to_event=er.get("to_event", ""),
            relation_type=er.get("relation_type", "before"),
        )
        graph.add_event_relation_edge(edge)

    # --- 地点层级 ---
    for lh in data.get("location_hierarchy", []):
        edge = LocationHierarchyEdge(
            child=lh.get("child", ""),
            parent=lh.get("parent", ""),
        )
        graph.add_location_hierarchy_edge(edge)

    return graph


def update_story_graph_with_chapter(
    graph: StoryGraph,
    chapter_text: str,
    chapter_index: int,
    openai_client,
    model: str = "deepseek-chat",
) -> StoryGraph:
    """用新章节增量更新故事知识图谱。

    Args:
        graph: 现有的 StoryGraph
        chapter_text: 新章节文本
        chapter_index: 章节编号
        openai_client: LLM 客户端
        model: 模型名

    Returns:
        更新后的 StoryGraph（直接修改传入的 graph）
    """
    # 构建已有图谱摘要
    existing_summary = json.dumps({
        "characters": [
            {"name": n.name, "role": n.role_type, "faction": n.faction,
             "importance": n.importance, "status": n.status}
            for n in graph.person_nodes
        ],
        "relationships": [
            {"from": e.from_char, "to": e.to_char, "type": e.relation_type,
             "intimacy": e.intimacy, "tension": e.current_tension}
            for e in graph.relationship_edges
        ],
        "events": [
            {"name": e.name, "type": e.event_type, "chapters": f"{e.chapter_start}-{e.chapter_end}",
             "importance": e.importance, "summary": e.summary}
            for e in graph.event_nodes[-20:]  # 只带最近 20 个事件
        ],
        "locations": [{"name": n.name, "type": n.location_type} for n in graph.location_nodes],
        "organizations": [{"name": n.name, "type": n.org_type, "leader": n.leader} for n in graph.org_nodes],
        "items": [{"name": n.name, "type": n.item_type} for n in graph.item_nodes],
    }, ensure_ascii=False, indent=2)

    response = openai_client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": CHAPTER_UPDATE_PROMPT_V2.format(
                existing_summary=existing_summary,
                chapter_index=chapter_index,
                chapter_text=chapter_text[:8000],
            )},
            {"role": "user", "content": "请分析新章节并返回图谱更新。"},
        ],
        temperature=0.3,
        timeout=180,
        max_tokens=8192,
    )

    content = response.choices[0].message.content or ""
    content = _clean_json(content)

    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return graph

    # --- 添加新人物 ---
    for nd in data.get("new_characters", []):
        name = nd.get("name", "")
        if name and not graph.get_person_node(name):
            node = CharacterNode(
                id=nd.get("id", f"char_{len(graph.person_nodes):03d}"),
                name=name,
                role_type=nd.get("role_type", ""),
                faction=nd.get("faction", ""),
                importance=nd.get("importance", 5),
                status=nd.get("status", "active"),
                first_appearance_chapter=chapter_index,
                description=nd.get("description", ""),
            )
            graph.add_person_node(node)

    # --- 新人物关系 ---
    for ed in data.get("new_relationships", []):
        edge = RelationshipEdge(
            from_char=ed.get("from_char", ""),
            to_char=ed.get("to_char", ""),
            relation_type=ed.get("relation_type", ""),
            sub_type=ed.get("sub_type", ""),
            intimacy=ed.get("intimacy", 0),
            power_dynamic=ed.get("power_dynamic", "平等"),
            public_knowledge=ed.get("public_knowledge", True),
            current_tension=ed.get("current_tension", "和谐"),
            shared_history=ed.get("shared_history", ""),
            established_chapter=chapter_index,
        )
        graph.add_relationship_edge(edge)

    # --- 更新已有关系 ---
    for upd in data.get("updated_relationships", []):
        edge = graph.get_relationship_edge(upd.get("from_char", ""), upd.get("to_char", ""))
        if not edge:
            edge = graph.get_relationship_edge(upd.get("to_char", ""), upd.get("from_char", ""))
        if edge:
            changes = upd.get("changes", {})
            for field, change in changes.items():
                if hasattr(edge, field):
                    new_val = change.get("new", getattr(edge, field))
                    old_val = getattr(edge, field)
                    if str(old_val) != str(new_val):
                        setattr(edge, field, new_val)
                        graph.timeline.append(RelationEvent(
                            chapter=chapter_index,
                            from_char=upd.get("from_char", ""),
                            to_char=upd.get("to_char", ""),
                            field=field, old_value=str(old_val), new_value=str(new_val),
                        ))
            # 重新写入更新后的边
            graph.add_relationship_edge(edge)

    # --- 新事件 ---
    for ev in data.get("new_events", []):
        node = EventNode(
            id=ev.get("id", ""),
            name=ev.get("name", ""),
            event_type=ev.get("event_type", ""),
            chapter_start=ev.get("chapter_start", chapter_index),
            chapter_end=ev.get("chapter_end", chapter_index),
            location=ev.get("location", ""),
            participants=ev.get("participants", []),
            cause=ev.get("cause", ""),
            effect=ev.get("effect", ""),
            summary=ev.get("summary", ""),
            importance=ev.get("importance", 5),
        )
        graph.add_event_node(node)
        for p in ev.get("participants", []):
            pe = ParticipatesEdge(
                person=p.get("name", ""), event=ev.get("name", ""),
                role=p.get("role", "参与"), outcome=p.get("outcome", ""),
            )
            graph.add_participates_edge(pe)
        if ev.get("location"):
            le = LocatedAtEdge(event=ev.get("name", ""), location=ev["location"])
            graph.add_located_at_edge(le)

    # --- 新地点 ---
    for lo in data.get("new_locations", []):
        node = LocationNode(
            id=lo.get("id", ""), name=lo.get("name", ""),
            location_type=lo.get("location_type", ""), parent=lo.get("parent", ""),
            description=lo.get("description", ""), factions=lo.get("factions", []),
            first_appear_chapter=chapter_index,
            is_destroyed=lo.get("is_destroyed", False),
        )
        graph.add_location_node(node)

    # --- 新组织 ---
    for og in data.get("new_organizations", []):
        node = OrganizationNode(
            id=og.get("id", ""), name=og.get("name", ""),
            org_type=og.get("org_type", ""), leader=og.get("leader", []),
            members=og.get("members", []), base=og.get("base", ""),
            status=og.get("status", "鼎盛"), description=og.get("description", ""),
        )
        graph.add_org_node(node)

    # --- 新物品 ---
    for it in data.get("new_items", []):
        node = ItemNode(
            id=it.get("id", ""), name=it.get("name", ""),
            item_type=it.get("item_type", ""), grade=it.get("grade", ""),
            owner_history=it.get("owner_history", []),
            abilities=it.get("abilities", []), source=it.get("source", ""),
            description=it.get("description", ""),
        )
        graph.add_item_node(node)

    # --- 新事件关系 ---
    for er in data.get("new_event_relations", []):
        edge = EventRelationEdge(
            from_event=er.get("from_event", ""),
            to_event=er.get("to_event", ""),
            relation_type=er.get("relation_type", "before"),
        )
        graph.add_event_relation_edge(edge)

    # --- 新地点层级 ---
    for lh in data.get("new_location_hierarchy", []):
        edge = LocationHierarchyEdge(
            child=lh.get("child", ""), parent=lh.get("parent", ""),
        )
        graph.add_location_hierarchy_edge(edge)

    # --- 章节摘要 ---
    ch_summary = data.get("chapter_summary", "")
    if ch_summary:
        chapter_node = graph.get_chapter_node(chapter_index)
        if chapter_node:
            chapter_node.summary = ch_summary
            graph.add_chapter_node(chapter_node)

    graph.last_updated_chapter = chapter_index
    return graph


# ============================================================
# V2 graph_to_context —— 格式化图谱为 LLM 可用的上下文
# ============================================================

def graph_to_context(graph, max_chars_per_section: int = 800) -> str:
    """将 StoryGraph 格式化为 LLM prompt 可用的文本上下文。

    Args:
        graph: StoryGraph 实例
        max_chars_per_section: 每段最大字符数，防止上下文过长

    Returns:
        格式化的文本上下文
    """
    if graph is None:
        return ""
    return _graph_to_context_v2(graph, max_chars_per_section)


def _graph_to_context_v2(graph: StoryGraph, max_chars: int = 800) -> str:
    """V2 StoryGraph 的完整格式化。"""
    lines = ["[故事知识图谱]"]

    # 1. 人物
    persons = sorted(graph.person_nodes, key=lambda n: -n.importance)
    if persons:
        lines.append("\n## 人物")
        for n in persons[:30]:
            status_mark = {"active": "", "dead": "[已死]", "missing": "[失踪]", "unknown": "[未知]"}.get(n.status, "")
            line = (f"- {n.name} [{n.role_type}] {status_mark}"
                    + (f" | {n.faction}" if n.faction else "")
                    + (f" | {n.description}" if n.description else ""))
            lines.append(line[:max_chars])

    # 2. 人物关系
    rels = graph.relationship_edges
    if rels:
        lines.append("\n## 人物关系")
        for e in rels[:20]:
            line = (f"- {e.from_char} ←→ {e.to_char}: {e.relation_type}"
                    + (f"({e.sub_type})" if e.sub_type else "")
                    + f" | 亲密度:{e.intimacy:+d}"
                    + (f" | {e.current_tension}" if e.current_tension != "和谐" else "")
                    + (" [隐藏]" if not e.public_knowledge else ""))
            lines.append(line[:max_chars])
        if len(rels) > 20:
            lines.append(f"... 还有 {len(rels) - 20} 条关系")

    # 3. 事件时间线
    events = graph.event_timeline()
    if events:
        lines.append("\n## 事件时间线")
        for e in events[:15]:
            ch_range = f"第{e.chapter_start}章" if e.chapter_start == e.chapter_end else f"第{e.chapter_start}-{e.chapter_end}章"
            line = (f"- [{ch_range}] [{e.event_type}] {e.name}"
                    + (f" @{e.location}" if e.location else "")
                    + (f" | {e.summary}" if e.summary else ""))
            lines.append(line[:max_chars])
        if len(events) > 15:
            lines.append(f"... 还有 {len(events) - 15} 个事件")

    # 4. 地点
    locs = graph.location_nodes
    if locs:
        lines.append("\n## 地点")
        hierarchy = graph.location_hierarchy()
        for root in hierarchy.get("roots", [])[:10]:
            lines.append(_format_location_tree(root, hierarchy.get("children", {}), graph, max_chars, 0))

    # 5. 组织
    orgs = graph.org_nodes
    if orgs:
        lines.append("\n## 组织/势力")
        for o in orgs[:10]:
            line = (f"- {o.name} [{o.org_type}] 状态:{o.status}"
                    + (f" | 首领: {', '.join(o.leader)}" if o.leader else "")
                    + (f" | {o.description}" if o.description else ""))
            lines.append(line[:max_chars])

    # 6. 物品
    items = graph.item_nodes
    if items:
        lines.append("\n## 关键物品/功法")
        for it in items[:10]:
            line = (f"- {it.name} [{it.item_type}]"
                    + (f" {it.grade}" if it.grade else "")
                    + (f" | {it.description}" if it.description else ""))
            lines.append(line[:max_chars])

    # 7. 因果链
    event_rels = graph.event_relation_edges
    if event_rels:
        causes = [e for e in event_rels if e.relation_type == "causes"]
        if causes:
            lines.append("\n## 因果链")
            for e in causes[:10]:
                line = f"- {e.from_event} → 导致 → {e.to_event}"
                lines.append(line[:max_chars])

    return "\n".join(lines)


def _format_location_tree(name: str, children: dict, graph: StoryGraph,
                          max_chars: int, depth: int) -> str:
    """递归格式化地点层级树。"""
    indent = "  " * depth
    node = graph.get_location_node(name)
    desc = f" - {node.description}" if node and node.description else ""
    line = f"{indent}- {name}{desc[:max_chars - len(indent) - len(name) - 5]}"
    result = [line]
    for child_name in children.get(name, [])[:5]:
        result.append(_format_location_tree(child_name, children, graph, max_chars, depth + 1))
    return "\n".join(result)


def _clean_json(content: str) -> str:
    """清理 LLM 返回的 JSON（去 markdown fence、去注释等）。"""
    content = content.strip()
    if content.startswith("```"):
        lines = content.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        content = "\n".join(lines)
    return content
