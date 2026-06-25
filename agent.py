# -*- coding: utf-8 -*-
"""
Novel2Comic Agent V2
====================
Agent 驱动的"小说→漫画"生成系统。

使用 AgentFlow 框架：Skill + ToolKit + Memory + Thinking。
Agent 自主决策调用 6 个 Pipeline Tool，用户通过自然语言交互。

用法:
    python agent.py "小说文本内容"
    python agent.py chapter1.txt --title "月下归来"
    python agent.py --load projects/xxx/chapter_data.json
"""

import asyncio
import json
import os
import sys
from datetime import datetime
from typing import Optional

# Windows 修复
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# 路径：agentflow 安装为包后，以下导入自动生效
# 开发阶段：设置 PYTHONPATH=/path/to/AgentFlow 即可
from agentflow.runtime.builder import AgentBuilder
from agentflow.runtime.toolkit import tool
from agentflow.runtime.memory.manager import MemoryProfile
from agentflow.runtime.thinking import ThinkingMode
from agentflow.runtime.llm_client import OpenAIClient

from novel2comic.src.models import (
    ChapterData, AnalysisResult, CharacterSheet, CharacterAppearance,
    Scene, Panel, ComicPage, StyleProfile, Novel, ChapterInfo,
)
from novel2comic.src.styles import detect_style, BUILTIN_STYLES
from novel2comic.src.img_adapter import ImageGenAdapter
from novel2comic.src.chapter_parser import parse_novel_chapters
from novel2comic.src.novel_registry import (
    register_novel, find_novel, list_all_novels,
    update_novel_access, update_novel_style, update_novel_chapters,
)
from novel2comic.src.knowledge_graph import (
    extract_graph_from_text, update_graph_with_chapter, graph_to_context,
    extract_story_graph_from_text, update_story_graph_with_chapter,
)

# Phase 1: 导入新服务层
from novel2comic.src.llm import UnifiedLLM
from novel2comic.src.services import (
    KnowledgeGraphService,
    ImageGenerationService,
    ComicCompilationService,
    ProjectService,
    SearchService,
)
from novel2comic.src.context import ServiceRegistry

# ============================================================
# 共享上下文（Tool 通过此访问 LLM / ImageGen / Data）
# ============================================================

class AgentContext:
    """Tool 共享状态——在 Agent 启动前注入。"""
    def __init__(self):
        self.novel: Optional[Novel] = None         # 全书数据（章节列表 + 角色库）
        self.chapter_data: Optional[ChapterData] = None  # 当前章的 Pipeline 状态
        self.openai_client = None   # openai.OpenAI 同步客户端（供 Tool 内 LLM 调用）
        self.llm_model: str = ""
        self.img_gen: Optional[ImageGenAdapter] = None
        self._llm = None  # UnifiedLLM 实例（Phase 1 新增，可选）
        self.services = None  # ServiceRegistry 实例（Phase 1 新增，可选）

    @property
    def data(self) -> Optional[ChapterData]:
        """快捷访问当前章数据。"""
        return self.chapter_data

_ctx = AgentContext()


def _read_text_file(file_path: str) -> str:
    """读取文本文件，自动检测编码。

    依次尝试: UTF-8 → UTF-16 → GBK → GB18030 → latin-1(兜底)
    """
    encodings = ["utf-8", "utf-16", "gbk", "gb18030", "gb2312", "latin-1"]
    for enc in encodings:
        try:
            with open(file_path, "r", encoding=enc) as f:
                text = f.read()
            # 验证：utf-8/utf-16 读取的文本不应有明显乱码
            if enc in ("utf-8", "utf-16") and "�" in text:
                continue  # 有替换字符，说明编码不对
            return text
        except (UnicodeDecodeError, UnicodeError):
            continue
    # 最终兜底：errors='replace'
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _llm_chat_json(system_prompt: str, user_prompt: str, temperature: float = 0.3) -> dict:
    """Tool 内部使用的 LLM JSON 调用。

    委托给 UnifiedLLM 服务（src/llm.py）。
    """
    # 使用全局 _ctx 上的 UnifiedLLM（Phase 1 兼容模式）
    if hasattr(_ctx, '_llm') and _ctx._llm is not None:
        return _ctx._llm.chat_json(system_prompt, user_prompt, temperature)
    # 向后兼容：直接调用原始 OpenAI 客户端
    full_system = system_prompt + "\n\nYou MUST respond with valid JSON only. No markdown fences, no explanation."
    response = _ctx.openai_client.chat.completions.create(
        model=_ctx.llm_model,
        messages=[
            {"role": "system", "content": full_system},
            {"role": "user", "content": user_prompt},
        ],
        temperature=temperature,
        timeout=120,
        max_tokens=4096,
    )
    text = response.choices[0].message.content or ""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return json.loads(text)


# ============================================================
# Novel 级 Tool（全书管理）
# ============================================================

@tool
def load_novel(file_path: str) -> str:
    """加载一本小说文件。

    首次加载时解析章节并缓存。再次加载同一文件时直接从缓存恢复，
    无需重新解析。支持 .txt 格式。

    Args:
        file_path: 小说 .txt 文件的路径
    """
    if not os.path.isfile(file_path):
        return json.dumps({"error": f"文件不存在: {file_path}"})

    # 1. 检查注册表 —— 是否已解析过
    cached = find_novel(file_path)
    if cached:
        # 缓存命中！直接从 novel.json 恢复
        novel_json_path = os.path.join(cached.project_dir, "novel.json")
        if os.path.exists(novel_json_path):
            _ctx.novel = Novel.load(novel_json_path)
            _ctx.novel.output_dir = cached.project_dir
            update_novel_access(file_path)

            return json.dumps({
                "status": "ok",
                "cached": True,
                "title": cached.title,
                "total_chapters": cached.total_chapters,
                "style": cached.style,
                "project_dir": cached.project_dir,
                "message": (
                    f"[缓存命中]《{cached.title}》已恢复，共 {cached.total_chapters} 章。"
                    + (f" 全书风格: {cached.style}。" if cached.style else "")
                    + f" 请调用 list_chapters 查看目录，select_chapter(N) 选择章节。"
                ),
            }, ensure_ascii=False)

    # 2. 缓存未命中 —— 解析小说
    text = _read_text_file(file_path)

    base_name = os.path.splitext(os.path.basename(file_path))[0]
    chapters = parse_novel_chapters(text, base_name)

    # 创建项目目录
    project_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "projects", datetime.now().strftime("%Y%m%d_%H%M%S"),
    )
    os.makedirs(project_dir, exist_ok=True)

    _ctx.novel = Novel(
        title=base_name,
        file_path=file_path,
        chapters=chapters,
        output_dir=project_dir,
    )

    # 持久化
    novel_path = os.path.join(project_dir, "novel.json")

    # 构建初始故事知识图谱（v2 扩展本体）
    if _ctx.novel.total_chapters >= 1:
        print("  [Graph] 正在从全书提取故事知识图谱（人物+事件+地点+组织+物品）...")

        # 采样策略：取每章前 1500 字 + 均匀采样保证覆盖面
        total = _ctx.novel.total_chapters
        sample_count = min(total, 10)  # 最多采样 10 章
        step = max(1, total // sample_count)
        sampled_indices = list(range(0, total, step))[:sample_count]

        seed_text_parts = []
        for idx in sampled_indices:
            ch = _ctx.novel.chapters[idx]
            seed_text_parts.append(f"第{ch.index}章 {ch.title}\n{ch.content[:1500]}")
        seed_text = "\n\n".join(seed_text_parts)

        _ctx.novel.story_graph = extract_story_graph_from_text(
            seed_text, _ctx.openai_client, model=_ctx.llm_model,
        )
        _ctx.novel.story_graph.last_updated_chapter = total
        counts = _ctx.novel.story_graph.node_type_counts()
        print(f"  [Graph] 提取完成: {counts} (采样 {len(sampled_indices)}/{total} 章)")

    _ctx.novel.save(novel_path)

    # 注册到注册表
    register_novel(file_path, base_name, len(chapters), project_dir)

    ch_list = [f"第{ch.index}章: {ch.title} ({ch.word_count}字)" for ch in chapters[:20]]
    preview = "\n".join(ch_list)
    if len(chapters) > 20:
        preview += f"\n... 共 {len(chapters)} 章"

    return json.dumps({
        "status": "ok",
        "cached": False,
        "title": base_name,
        "total_chapters": len(chapters),
        "chapters": ch_list,
        "message": f"[首次解析]《{base_name}》已加载并缓存，共 {len(chapters)} 章。下次访问将直接恢复。请调用 list_chapters 查看目录，select_chapter(N) 选择章节。\n\n{preview}",
    }, ensure_ascii=False)


@tool
def list_novels() -> str:
    """列出所有已加载过的小说（支持从注册表恢复）。

    显示每本小说的标题、章节数、风格和最后访问时间。
    无需先调用 load_novel。
    """
    entries = list_all_novels()

    if not entries:
        return json.dumps({
            "status": "ok",
            "novels": [],
            "message": "还没有加载过任何小说。请用 load_novel(文件路径) 加载一本。",
        }, ensure_ascii=False)

    novel_list = []
    for e in entries:
        novel_list.append(
            f"《{e.title}》({e.total_chapters}章) | 风格: {e.style or '未设置'} | 最后访问: {e.last_accessed[:19] if e.last_accessed else '未知'}"
        )

    return json.dumps({
        "status": "ok",
        "count": len(entries),
        "novels": novel_list,
        "message": (
            f"共 {len(entries)} 本已加载的小说。"
            f" 用 resume_novel({len(entries)} 本中的序号从 0 开始) 恢复，"
            f"或用 load_novel(路径) 加载新的。"
        ),
    }, ensure_ascii=False)


@tool
def resume_novel(novel_index: int = 0) -> str:
    """恢复之前加载过的小说。

    从注册表中按索引恢复，自动加载所有章节和已设计的角色。

    Args:
        novel_index: 小说在列表中的索引（从 0 开始）。调用 list_novels 查看。
    """
    entries = list_all_novels()

    if not entries:
        return json.dumps({"error": "还没有加载过任何小说。请用 load_novel(路径) 加载。"})

    if novel_index < 0 or novel_index >= len(entries):
        return json.dumps({
            "error": f"索引 {novel_index} 无效。可用范围: 0-{len(entries)-1}",
            "available": [f"[{i}]《{e.title}》" for i, e in enumerate(entries)],
        })

    entry = entries[novel_index]

    # 从 novel.json 恢复
    novel_json_path = os.path.join(entry.project_dir, "novel.json")
    if not os.path.exists(novel_json_path):
        return json.dumps({"error": f"小说数据文件不存在: {novel_json_path}。请重新 load_novel('{entry.novel_path}')"})

    _ctx.novel = Novel.load(novel_json_path)
    _ctx.novel.output_dir = entry.project_dir
    update_novel_access(entry.novel_path)

    # 列出章节和角色状态
    completed = sum(1 for ch in _ctx.novel.chapters if ch.status == "completed")
    chars_known = [c.name for c in _ctx.novel.characters]

    return json.dumps({
        "status": "ok",
        "title": entry.title,
        "total_chapters": entry.total_chapters,
        "completed_chapters": completed,
        "style": entry.style,
        "characters_known": chars_known,
        "message": (
            f"已恢复《{entry.title}》，共 {entry.total_chapters} 章"
            f"（已完成 {completed} 章）。"
            + (f" 全书角色: {', '.join(chars_known)}。" if chars_known else "")
            + f" 请调用 list_chapters 查看详情，select_chapter(N) 选择要生成的章节。"
        ),
    }, ensure_ascii=False)


@tool
def list_chapters() -> str:
    """列出当前小说的所有章节及其状态。"""
    novel = _ctx.novel
    if not novel:
        return json.dumps({"error": "请先调用 load_novel 加载小说"})

    lines = []
    for ch in novel.chapters:
        status_icon = "[OK]" if ch.status == "completed" else ("[*]" if ch.status == "generating" else "[ ]")
        lines.append(f"{status_icon} 第{ch.index}章: {ch.title} ({ch.word_count}字)")

    return json.dumps({
        "status": "ok",
        "total": novel.total_chapters,
        "current": novel.current_chapter_index,
        "chapter_list": lines,
        "characters_known": [c.name for c in novel.characters],
        "message": f"当前选中: 第{novel.current_chapter_index}章。用 select_chapter(N) 切换章节。已发现角色: {', '.join(c.name for c in novel.characters) if novel.characters else '（无）'}",
    }, ensure_ascii=False)


@tool
def select_chapter(chapter_index: int) -> str:
    """选择要生成漫画的章节。

    选中后，后续的 analyze_text / design_characters 等工具将针对该章执行。
    之前章节已设计的角色会自动复用。

    Args:
        chapter_index: 章节编号 (1-based)
    """
    novel = _ctx.novel
    if not novel:
        return json.dumps({"error": "请先调用 load_novel 加载小说"})

    chapter = None
    for ch in novel.chapters:
        if ch.index == chapter_index:
            chapter = ch
            break

    if not chapter:
        return json.dumps({"error": f"第{chapter_index}章不存在。可用章节: 1-{novel.total_chapters}"})

    novel.current_chapter_index = chapter_index
    chapter.status = "generating"

    # 为该章创建 ChapterData，继承全书角色库和风格
    ch_output_dir = os.path.join(novel.output_dir, f"chapter_{chapter_index:04d}")
    os.makedirs(ch_output_dir, exist_ok=True)

    _ctx.chapter_data = ChapterData(
        title=f"第{chapter_index}章 {chapter.title}",
        source_text=chapter.content,
        output_dir=ch_output_dir,
        created_at=datetime.now().isoformat(),
    )

    # 继承全书角色库
    _ctx.chapter_data.characters = list(novel.characters)

    # 继承全书风格
    if novel.style_profile:
        _ctx.chapter_data.style_profile = novel.style_profile

    # 增量更新知识图谱（新章节到达时自动更新）
    if novel.story_graph and novel.story_graph.last_updated_chapter < chapter_index:
        print(f"  [Graph] 正在用第{chapter_index}章更新故事知识图谱...")
        try:
            novel.story_graph = update_story_graph_with_chapter(
                novel.story_graph, chapter.content, chapter_index,
                _ctx.openai_client, model=_ctx.llm_model,
            )
            print(f"  [Graph] 更新完成。")
        except Exception as e:
            print(f"  [Graph] 更新失败（非致命）: {e}")

    return json.dumps({
        "status": "ok",
        "chapter_index": chapter_index,
        "title": chapter.title,
        "word_count": chapter.word_count,
        "inherited_characters": [c.name for c in novel.characters],
        "inherited_style": novel.style_profile.name if novel.style_profile else "auto",
        "message": (
            f"已选中 第{chapter_index}章《{chapter.title}》({chapter.word_count}字)。"
            + (f" 已从前面章节继承 {len(novel.characters)} 个角色。" if novel.characters else "")
            + " 请调用 analyze_text 开始生成。"
        ),
    }, ensure_ascii=False)


# ============================================================
# 知识图谱查询 Tool
# ============================================================

@tool
def query_graph() -> str:
    """查看当前全书的故事知识图谱（v2 扩展本体）。

    返回：人物、事件、地点、组织、物品、关系边、事件因果链等信息。
    图谱会在加载小说和选择章节时自动更新。
    """
    novel = _ctx.novel
    if not novel or not novel.story_graph:
        return json.dumps({"error": "知识图谱尚未构建。请先 load_novel 加载小说。"})

    graph = novel.story_graph
    context = graph_to_context(graph)
    counts = graph.node_type_counts()

    # 人物
    persons_info = []
    for n in sorted(graph.person_nodes, key=lambda x: -x.importance):
        persons_info.append({
            "name": n.name, "role": n.role_type, "faction": n.faction,
            "importance": n.importance, "status": n.status,
            "description": n.description,
        })

    # 人物关系
    rels_info = []
    for e in graph.relationship_edges:
        rels_info.append({
            "from": e.from_char, "to": e.to_char,
            "type": e.relation_type, "sub_type": e.sub_type,
            "intimacy": e.intimacy, "power": e.power_dynamic,
            "tension": e.current_tension, "public": e.public_knowledge,
            "history": e.shared_history,
        })

    # 事件
    events_info = []
    for e in graph.event_timeline():
        events_info.append({
            "name": e.name, "type": e.event_type,
            "chapters": f"{e.chapter_start}-{e.chapter_end}",
            "location": e.location, "importance": e.importance,
            "summary": e.summary,
        })

    # 地点
    locations_info = [{"name": n.name, "type": n.location_type,
                       "parent": n.parent, "description": n.description}
                      for n in graph.location_nodes]

    # 组织
    orgs_info = [{"name": n.name, "type": n.org_type, "leader": n.leader,
                  "status": n.status, "description": n.description}
                 for n in graph.org_nodes]

    # 物品
    items_info = [{"name": n.name, "type": n.item_type, "grade": n.grade,
                   "description": n.description}
                  for n in graph.item_nodes]

    # 因果链
    causal_info = []
    for e in graph.event_relation_edges:
        if e.relation_type == "causes":
            causal_info.append(f"{e.from_event} → {e.to_event}")

    # 变化时间线
    timeline_info = []
    for t in graph.timeline[-10:]:
        timeline_info.append(
            f"第{t.chapter}章: {t.from_char}←→{t.to_char} {t.field} {t.old_value}→{t.new_value} ({t.trigger_event})"
        )

    return json.dumps({
        "status": "ok",
        "node_counts": counts,
        "total_nodes": graph.total_node_count,
        "total_edges": graph.total_edge_count,
        "persons": persons_info,
        "relationships": rels_info,
        "events": events_info,
        "locations": locations_info,
        "organizations": orgs_info,
        "items": items_info,
        "causal_chains": causal_info,
        "recent_changes": timeline_info,
        "context": context,
        "message": (
            f"故事图谱: {counts}，共 {graph.total_node_count} 节点, "
            f"{graph.total_edge_count} 条边。最近更新: 第{graph.last_updated_chapter}章。"
        ),
    }, ensure_ascii=False)


@tool
def query_character_relations(character_name: str) -> str:
    """查询指定角色在知识图谱中的所有关系。

    返回该角色与其他人物的关系类型、亲密度、权力动态、情感张力，
    以及用于分镜指导的镜头建议。

    Args:
        character_name: 角色中文名（如 "苏墨"）
    """
    novel = _ctx.novel
    if not novel or not novel.story_graph:
        return json.dumps({"error": "知识图谱尚未构建。"})

    graph = novel.story_graph
    node = graph.get_person_node(character_name)
    if not node:
        known = [n.name for n in graph.person_nodes]
        return json.dumps({
            "error": f"角色 '{character_name}' 不在图谱中。已知角色: {', '.join(known)}",
        })

    # 获取该角色的所有关系
    edges = [e for e in graph.relationship_edges
             if e.from_char == character_name or e.to_char == character_name]
    relations = []
    hints = []
    for e in edges:
        other = e.to_char if e.from_char == character_name else e.from_char
        relations.append({
            "with": other,
            "type": e.relation_type,
            "sub_type": e.sub_type,
            "intimacy": e.intimacy,
            "power": e.power_dynamic,
            "tension": e.current_tension,
            "public": e.public_knowledge,
            "history": e.shared_history,
        })
        hint = graph.get_storyboard_hints(character_name, other)
        if hint:
            hints.append(f"与{other}同框时: {hint}")

    return json.dumps({
        "status": "ok",
        "character": character_name,
        "role": node.role_type,
        "faction": node.faction,
        "importance": node.importance,
        "description": node.description,
        "relation_count": len(relations),
        "relations": relations,
        "storyboard_hints": hints,
        "message": f"{character_name} [{node.role_type}] 有 {len(relations)} 条关系。",
    }, ensure_ascii=False)


@tool
def query_events(character_name: str = "") -> str:
    """查询故事事件时间线，可按人物筛选。

    Args:
        character_name: 可选，按角色名筛选事件。留空则返回全部事件时间线。
    """
    novel = _ctx.novel
    if not novel or not novel.story_graph:
        return json.dumps({"error": "知识图谱尚未构建。"})

    graph = novel.story_graph

    if character_name:
        events = graph.character_events(character_name)
        return json.dumps({
            "status": "ok",
            "character": character_name,
            "event_count": len(events),
            "events": events,
            "message": f"{character_name} 参与了 {len(events)} 个事件。",
        }, ensure_ascii=False)

    events = graph.event_timeline()
    events_info = []
    for e in events:
        events_info.append({
            "name": e.name, "type": e.event_type,
            "chapters": f"{e.chapter_start}-{e.chapter_end}",
            "location": e.location, "importance": e.importance,
            "cause": e.cause, "effect": e.effect,
            "summary": e.summary,
            "participants": e.participants,
        })

    return json.dumps({
        "status": "ok",
        "event_count": len(events_info),
        "events": events_info,
        "message": f"共 {len(events_info)} 个事件。",
    }, ensure_ascii=False)


@tool
def query_location(location_name: str) -> str:
    """查询地点详情及层级关系。

    Args:
        location_name: 地点名
    """
    novel = _ctx.novel
    if not novel or not novel.story_graph:
        return json.dumps({"error": "知识图谱尚未构建。"})

    graph = novel.story_graph
    node = graph.get_location_node(location_name)
    if not node:
        known = [n.name for n in graph.location_nodes]
        return json.dumps({
            "error": f"地点 '{location_name}' 不在图谱中。已知地点: {', '.join(known) if known else '（无）'}",
        })

    # 查层级
    hierarchy = graph.location_hierarchy()
    children = hierarchy.get("children", {}).get(location_name, [])

    # 查该地点发生的事件
    events_here = []
    for e in graph.located_at_edges:
        if e.location == location_name:
            ev = graph.get_event_node(e.event)
            if ev:
                events_here.append({"name": ev.name, "chapters": f"{ev.chapter_start}-{ev.chapter_end}",
                                   "summary": ev.summary})

    return json.dumps({
        "status": "ok",
        "location": {
            "name": node.name, "type": node.location_type,
            "parent": node.parent, "description": node.description,
            "factions": node.factions, "is_destroyed": node.is_destroyed,
        },
        "parent": node.parent,
        "children": children,
        "events_here": events_here,
        "message": f"{location_name} [{node.location_type}]"
                   + (f" → 父级: {node.parent}" if node.parent else "（顶层地点）")
                   + (f" | 子地点: {', '.join(children)}" if children else ""),
    }, ensure_ascii=False)


@tool
def query_organization(org_name: str) -> str:
    """查询组织/势力详情及成员。

    Args:
        org_name: 组织名
    """
    novel = _ctx.novel
    if not novel or not novel.story_graph:
        return json.dumps({"error": "知识图谱尚未构建。"})

    graph = novel.story_graph
    result = graph.org_members(org_name)
    if not result:
        known = [n.name for n in graph.org_nodes]
        return json.dumps({
            "error": f"组织 '{org_name}' 不在图谱中。已知组织: {', '.join(known) if known else '（无）'}",
        })

    return json.dumps({
        "status": "ok",
        "organization": {
            "name": result["org"].name, "type": result["org"].org_type,
            "leader": result["declared_leaders"], "members": result["declared_members"],
            "base": result["org"].base, "status": result["org"].status,
            "description": result["org"].description,
        },
        "members_from_edges": result["members_from_edges"],
        "message": f"{org_name} [{result['org'].org_type}] 状态:{result['org'].status} | 首领:{', '.join(result['declared_leaders'])} | 成员:{len(result['declared_members'])}人",
    }, ensure_ascii=False)


@tool
def query_item(item_name: str) -> str:
    """查询物品/功法详情及归属历史。

    Args:
        item_name: 物品名
    """
    novel = _ctx.novel
    if not novel or not novel.story_graph:
        return json.dumps({"error": "知识图谱尚未构建。"})

    graph = novel.story_graph
    node = graph.get_item_node(item_name)
    if not node:
        known = [n.name for n in graph.item_nodes]
        return json.dumps({
            "error": f"物品 '{item_name}' 不在图谱中。已知物品: {', '.join(known) if known else '（无）'}",
        })

    ownership = graph.item_owners(item_name)

    return json.dumps({
        "status": "ok",
        "item": {
            "name": node.name, "type": node.item_type,
            "grade": node.grade, "abilities": node.abilities,
            "source": node.source, "description": node.description,
        },
        "ownership_history": ownership,
        "message": f"{item_name} [{node.item_type}] {node.grade} | {node.description}",
    }, ensure_ascii=False)


# ============================================================
# ============================================================
# 情节问答 Tool
# ============================================================

@tool
def ask_plot(question: str) -> str:
    """回答关于小说情节、角色、世界观的问题。

    会搜索全书章节内容，结合知识图谱，用 LLM 生成答案。
    可以问：角色背景、情节发展、人物关系、世界观设定等。

    Args:
        question: 关于小说的任何问题（如 "苏墨为什么回来？" "将军府的阴谋是什么？"）
    """
    novel = _ctx.novel
    if not novel:
        return json.dumps({"error": "请先 load_novel 加载小说"})

    # 1. 关键词搜索相关章节
    keywords = _extract_keywords(question)
    relevant_chapters = _search_chapters(novel, question, keywords)

    # 2. 收集知识图谱上下文
    graph_context = ""
    if novel.story_graph and novel.story_graph.total_node_count > 0:
        # 找出问题中提到的角色
        mentioned_chars = [n.name for n in novel.story_graph.person_nodes
                           if n.name in question]
        for char_name in mentioned_chars:
            graph_context += f"\n[图谱] {char_name}: "
            node = novel.story_graph.get_person_node(char_name)
            if node:
                graph_context += f"角色={node.role_type}, 阵营={node.faction}, {node.description}\n"
            edges = [e for e in novel.story_graph.relationship_edges
                     if e.from_char == char_name or e.to_char == char_name]
            for e in edges:
                other = e.to_char if e.from_char == char_name else e.from_char
                graph_context += f"  ←→ {other}: {e.relation_type}({e.sub_type}), 亲密度={e.intimacy:+d}, {e.shared_history}\n"

    # 3. 收集相关章节摘要
    chapter_context = ""
    for ch in relevant_chapters[:5]:
        snippet = ch.content[:500]
        chapter_context += f"\n--- 第{ch.index}章《{ch.title}》---\n{snippet}\n"

    if not chapter_context:
        # 没有任何匹配章节 → 搜全部章节目录
        chapter_context = "\n".join(
            f"第{ch.index}章《{ch.title}》: {ch.content[:100]}..."
            for ch in novel.chapters[:20]
        )

    # 4. 用 LLM 回答问题
    system_prompt = (
        "你是一位小说分析助手。根据提供的小说内容和角色关系图谱，回答用户的问题。\n"
        "只根据给定的内容回答，不要编造。如果不确定，说'书中未提及'。\n"
        "回答简洁，控制在 200 字以内。"
    )

    user_prompt = (
        f"## 问题\n{question}\n\n"
        f"## 角色关系图谱\n{graph_context}\n\n"
        f"## 相关章节内容\n{chapter_context[:4000]}\n\n"
        f"请回答。"
    )

    try:
        answer = _llm_chat_json(system_prompt, user_prompt, temperature=0.3)
        if isinstance(answer, dict):
            answer = answer.get("answer", str(answer))
    except Exception:
        answer = _ctx.openai_client.chat.completions.create(
            model=_ctx.llm_model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3, max_tokens=500,
        ).choices[0].message.content or ""

    return json.dumps({
        "status": "ok",
        "question": question,
        "searched_chapters": [f"第{ch.index}章《{ch.title}》" for ch in relevant_chapters[:5]],
        "characters_mentioned": [n.name for n in novel.story_graph.person_nodes if n.name in question] if novel.story_graph else [],
        "answer": answer,
    }, ensure_ascii=False)


def _extract_keywords(question: str) -> list[str]:
    """从问题中提取关键词。"""
    import re
    # 分词：中文按字符 n-gram，英文按空格
    words = []
    # 2-gram
    for i in range(len(question) - 1):
        bigram = question[i:i+2]
        if not re.match(r'[\s，。？?！!的了吗呢啊]', bigram):
            words.append(bigram)
    # 单字
    for ch in question:
        if ch not in '，。？?！!的了吗呢啊是什么怎么为什么':
            words.append(ch)
    # 去重取前 10 个
    seen = set()
    result = []
    for w in words:
        if w not in seen:
            seen.add(w)
            result.append(w)
    return result[:10]


def _search_chapters(novel, question: str, keywords: list[str]) -> list:
    """在章节中搜索与问题相关的内容。"""
    scores = []
    for ch in novel.chapters:
        score = 0
        content_lower = ch.content
        title_lower = ch.title
        for kw in keywords:
            score += content_lower.count(kw) * 3   # 内容匹配权重
            score += title_lower.count(kw) * 10     # 标题匹配权重
        # 问题整体匹配
        score += content_lower.count(question[:10]) * 20
        if score > 0:
            scores.append((score, ch))
    scores.sort(key=lambda x: x[0], reverse=True)
    return [ch for _, ch in scores]


# ============================================================
# Pipeline Tool（单章生成——共 7 个）
# ============================================================

@tool
def analyze_text(text: str) -> str:
    """分析小说文本：识别题材标签、漫画风格、人物预览、情感基调和时代背景。

    这是 Pipeline 的第一步。调用后会自动判断使用哪种漫画风格 (manga/webtoon/gufeng)。

    Args:
        text: 小说章节的完整文本（或前 3000 字符）
    """
    system_prompt = """你是一位资深的小说编辑和漫画改编顾问。
你需要分析一段小说文本，提取关键信息用于后续的漫画改编。

请分析以下维度并以 JSON 格式返回：
{
  "genre_tags": ["题材标签1", "题材标签2", ...],
  "style": "manga 或 webtoon 或 gufeng",
  "tone": ["情感基调1", "情感基调2", ...],
  "era": "时代背景",
  "pace": "叙事节奏（快节奏/慢热/张弛有度）",
  "characters_preview": [
    {"name": "角色名", "role": "主角/配角/反派/路人", "first_appearance_line": "首次出场的原文片段"}
  ]
}

题材标签从下列中选择：武侠, 仙侠, 玄幻, 都市, 校园, 科幻, 悬疑, 历史, 言情, 轻小说, 古装, 日常, 异世界, 职场, 恋爱

风格判断规则：
- 武侠/仙侠/玄幻/历史/古装 → gufeng
- 轻小说/校园/恋爱/日常/异世界 → manga
- 都市/职场/现实/娱乐圈 → webtoon
- 科幻/悬疑 → 快节奏用 manga，慢节奏用 webtoon"""

    sample = text[:3000]
    result = _llm_chat_json(system_prompt, f"请分析以下小说片段：\n\n{sample}")

    data = _ctx.data
    data.analysis = AnalysisResult(
        genre_tags=result.get("genre_tags", []),
        style=result.get("style", "auto"),
        tone=result.get("tone", []),
        era=result.get("era", ""),
        pace=result.get("pace", ""),
        characters_preview=result.get("characters_preview", []),
    )

    # 自动判断风格
    detected = detect_style(data.analysis.genre_tags, data.analysis.pace)
    data.analysis.style = detected.name
    data.style_profile = detected

    chars = [c["name"] for c in data.analysis.characters_preview]
    return json.dumps({
        "status": "ok",
        "style": detected.name,
        "genre_tags": data.analysis.genre_tags,
        "tone": data.analysis.tone,
        "era": data.analysis.era,
        "pace": data.analysis.pace,
        "characters_found": chars,
        "message": f"分析完成。风格={detected.name}，发现 {len(chars)} 个角色：{', '.join(chars)}。接下来请调用 design_characters 设计角色。",
    }, ensure_ascii=False)


@tool
def design_characters() -> str:
    """为分析阶段识别出的角色创建详细的 Character Sheet。

    每个角色包含外貌描述（脸型、发型、体型、服装、配饰）和 SD 生图触发词。
    首次出场角色从原文提取外貌，已设计过的角色自动跳过。
    必须在 analyze_text 之后调用。
    """
    data = _ctx.data
    if not data.analysis:
        return json.dumps({"error": "请先调用 analyze_text 分析文本"})

    existing_names = {c.name for c in data.characters}
    new_chars = [p for p in data.analysis.characters_preview if p["name"] not in existing_names]

    if not new_chars:
        return json.dumps({"status": "ok", "message": "所有角色已设计，跳过。", "characters": [c.name for c in data.characters]})

    # 提取外貌相关原文
    relevant_lines = []
    for line in data.source_text.split("\n"):
        for char in new_chars:
            if char["name"] in line:
                relevant_lines.append(line.strip())
                break
    text_context = "\n".join(relevant_lines[:30])

    system_prompt = """你是专业的漫画角色设计师。为每个角色创建详细的 Character Sheet。

返回 JSON 数组：
[
  {
    "id": "英文名_小写_下划线",
    "name": "中文名",
    "role": "protagonist/antagonist/supporting/minor",
    "appearance": {
      "face": "脸型、五官特征、肤色",
      "hair": "发型、发色、长度",
      "build": "体型（高矮胖瘦）",
      "clothing": "服装风格和细节",
      "accessories": "配饰（武器、首饰等）",
      "distinctive_features": "最独特的视觉特征（一句话概括）"
    },
    "sd_trigger_words": "英文触发词。格式: 'name, gender, hair description, clothing, distinctive feature, art style neutral'",
    "personality_notes": "性格特征对表情/姿态的影响"
  }
]

重要：sd_trigger_words 必须足够详细以确保每次生图角色外貌一致。"""

    # 从知识图谱获取角色上下文
    graph_context = ""
    if _ctx.novel and _ctx.novel.story_graph:
        graph_context = graph_to_context(_ctx.novel.story_graph)

    user_prompt = (
        f"## 人物列表\n" + "\n".join(f"- {c['name']} ({c['role']})" for c in new_chars) +
        f"\n\n## 原文片段（含外貌描写）\n{text_context}\n\n"
        + (f"## 人物关系知识图谱\n{graph_context}\n\n" if graph_context else "")
        + f"## 风格\n{data.style_profile.name if data.style_profile else 'auto'}\n\n"
        f"请为每个角色生成 Character Sheet（JSON 数组）。\n"
        f"注意：如果图谱中已有角色的 faction、描述等信息，请据此丰富角色设计。"
    )

    result = _llm_chat_json(system_prompt, user_prompt)

    for char_dict in result:
        appearance = CharacterAppearance(**char_dict.get("appearance", {}))
        sheet = CharacterSheet(
            id=char_dict.get("id", ""),
            name=char_dict.get("name", ""),
            role=char_dict.get("role", ""),
            appearance=appearance,
            sd_trigger_words=char_dict.get("sd_trigger_words", ""),
            personality_notes=char_dict.get("personality_notes", ""),
            status="draft",
        )
        data.characters.append(sheet)

    names = [c.name for c in data.characters]

    # 同步到全书角色库
    if _ctx.novel:
        _ctx.novel.add_characters(data.characters)

    return json.dumps({
        "status": "ok",
        "characters": names,
        "message": f"角色设计完成。共 {len(data.characters)} 个角色：{', '.join(names)}。接下来请调用 extract_scenes 拆分场景。",
    }, ensure_ascii=False)


@tool
def extract_scenes() -> str:
    """将小说文本拆分为 3-8 个关键叙事场景。

    按地点变换、时间跳跃、情绪转折切分场景。
    每个场景包含标题、摘要、出场角色、情绪变化和关键台词。
    必须在 design_characters 之后调用。
    """
    data = _ctx.data
    if not data.characters:
        return json.dumps({"error": "请先调用 design_characters 设计角色"})

    char_names = [c.name for c in data.characters]
    style_name = data.style_profile.name if data.style_profile else "auto"

    system_prompt = """你是专业的漫画改编编剧，将小说文本拆分为适合漫画表现的场景。

拆分规则：
- 按地点变换切分（从街上到屋内 = 新场景）
- 按时间跳跃切分（"三天后" = 新场景）
- 按情绪转折切分（从平静到冲突爆发）
- 一章通常 3-8 个场景，不要超过 8 个

返回 JSON 数组：
[
  {
    "id": 1,
    "title": "场景标题",
    "summary": "1-2句话概述",
    "characters_in_scene": ["角色名1"],
    "emotion_arc": "情绪变化（如：平静→紧张）",
    "key_dialogue": "该场景最重要的台词"
  }
]"""

    user_prompt = (
        f"## 原文\n{data.source_text}\n\n"
        f"## 已识别角色\n{', '.join(char_names)}\n\n"
        f"## 风格\n{style_name}\n\n"
        f"请拆分为关键场景（3-8个）。"
    )

    result = _llm_chat_json(system_prompt, user_prompt)

    data.scenes = []
    for sd in result:
        scene = Scene(
            id=sd.get("id", len(data.scenes) + 1),
            title=sd.get("title", ""),
            summary=sd.get("summary", ""),
            characters_in_scene=sd.get("characters_in_scene", []),
            emotion_arc=sd.get("emotion_arc", ""),
            key_dialogue=sd.get("key_dialogue", ""),
        )
        data.scenes.append(scene)

    scene_list = [f"场景{s.id}: {s.title}" for s in data.scenes]
    return json.dumps({
        "status": "ok",
        "scene_count": len(data.scenes),
        "scenes": scene_list,
        "message": f"场景拆分完成，共 {len(data.scenes)} 个场景。接下来请逐个调用 storyboard_scene(scene_id) 为每个场景生成分镜。",
    }, ensure_ascii=False)


@tool
def storyboard_scene(scene_id: int) -> str:
    """为指定场景生成漫画分镜脚本。

    每个场景生成 3-6 个格子，每格包含：
    - 中文画面描述（含前景/中景/背景构图）
    - 角色动作和表情
    - 台词
    - 镜头角度（特写/近景/中景/远景/俯视/仰视/POV）
    - 情绪氛围
    - 英文 SD 生图 prompt（自动注入风格基座 + 角色触发词 + 画幅比例）

    Args:
        scene_id: 场景的 id 编号（从 1 开始）
    """
    data = _ctx.data
    scene = next((s for s in data.scenes if s.id == scene_id), None)
    if not scene:
        return json.dumps({"error": f"场景 {scene_id} 不存在。可用场景：{[s.id for s in data.scenes]}"})

    char_info = "\n".join(
        f"- {c.name} [{c.role}]: {c.appearance.distinctive_features} | trigger: {c.sd_trigger_words}"
        for c in data.characters
    )

    # 找到场景相关原文
    scene_chars = scene.characters_in_scene
    relevant_lines = []
    for line in data.source_text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if any(ch in line for ch in scene_chars):
            relevant_lines.append(line)
    scene_text = "\n".join(relevant_lines[:20])

    system_prompt = """你是专业的漫画分镜师，精通日本漫画和韩式条漫的分镜设计。

为给定的场景生成 3-6 个格子的分镜脚本。每个格子返回：
{
  "panel_number": 1,
  "visual_description": "中文画面描述，必须有构图信息（前景/中景/背景）",
  "character_action": "角色的动作和表情",
  "dialogue": "台词（无则为空字符串）",
  "camera_angle": "特写/近景/中景/远景/俯视/仰视/主观POV/鸟瞰",
  "mood": "情绪氛围",
  "sd_prompt": "英文 SD prompt，含画风关键词 + 场景描述 + 构图提示",
  "character_refs": ["角色名列表"]
}

要求：
1. 每场景 3-6 格
2. 画面描述必须有构图感
3. 关键对话不能遗漏
4. 相邻格之间景别/视角要有变化"""

    # 从知识图谱获取分镜指导
    graph_hints = ""
    if _ctx.novel and _ctx.novel.story_graph:
        # 为场景中出场的每对角色生成分镜指导
        chars = scene.characters_in_scene
        for i, a in enumerate(chars):
            for b in chars[i+1:]:
                hint = _ctx.novel.story_graph.get_storyboard_hints(a, b)
                if hint:
                    graph_hints += f"- {a} ←→ {b}: {hint}\n"
        if graph_hints:
            graph_hints = f"## 人物关系分镜指导\n{graph_hints}\n\n"
            graph_hints += "注意：上述关系指导是基于全书的，如果你认为当前场景需要不同的情感表达，可以灵活调整。"

    user_prompt = (
        f"## 场景信息\n- 标题: {scene.title}\n- 摘要: {scene.summary}\n"
        f"- 情绪: {scene.emotion_arc}\n- 关键台词: {scene.key_dialogue}\n\n"
        f"## 场景原文\n{scene_text}\n\n"
        + (graph_hints if graph_hints else "")
        + (f"{graph_hints}" if graph_hints else "")
        + f"## 角色信息\n{char_info}\n\n"
        f"## 风格\n{data.style_profile.name if data.style_profile else 'auto'}\n\n"
        f"请生成 3-6 格分镜脚本（JSON 数组）。"
    )

    # 去掉重复的 graph_hints（上面的拼接有问题）
    user_prompt = (
        f"## 场景信息\n- 标题: {scene.title}\n- 摘要: {scene.summary}\n"
        f"- 情绪: {scene.emotion_arc}\n- 关键台词: {scene.key_dialogue}\n\n"
        f"## 场景原文\n{scene_text}\n\n"
        + (graph_hints if graph_hints else "")
        + f"## 角色信息\n{char_info}\n\n"
        f"## 风格\n{data.style_profile.name if data.style_profile else 'auto'}\n\n"
        f"请生成 3-6 格分镜脚本（JSON 数组）。"
    )

    result = _llm_chat_json(system_prompt, user_prompt)

    def _build_prompt(panel_dict: dict) -> str:
        parts = []
        if data.style_profile:
            parts.append(data.style_profile.sd_base_prompt)
        refs = panel_dict.get("character_refs", [])
        for ref_name in refs:
            for c in data.characters:
                if c.name == ref_name and c.sd_trigger_words:
                    parts.append(c.sd_trigger_words)
        if panel_dict.get("sd_prompt"):
            parts.append(panel_dict["sd_prompt"])
        if data.style_profile:
            parts.append(f"aspect ratio {data.style_profile.aspect_ratio}")
        return ", ".join(parts)

    scene.panels = []
    for pd in result:
        panel = Panel(
            panel_number=pd.get("panel_number", len(scene.panels) + 1),
            visual_description=pd.get("visual_description", ""),
            character_action=pd.get("character_action", ""),
            dialogue=pd.get("dialogue", ""),
            camera_angle=pd.get("camera_angle", ""),
            mood=pd.get("mood", ""),
            sd_prompt=_build_prompt(pd),
            character_refs=pd.get("character_refs", scene.characters_in_scene),
        )
        scene.panels.append(panel)

    return json.dumps({
        "status": "ok",
        "scene_id": scene_id,
        "panel_count": len(scene.panels),
        "panels": [f"格{p.panel_number}: [{p.camera_angle}] {p.visual_description[:50]}..." for p in scene.panels],
        "message": f"场景{scene_id} 分镜完成，{len(scene.panels)} 格。如需调整某格，请告诉我；否则继续 storyboard_scene 下一个场景。",
    }, ensure_ascii=False)


@tool
def generate_images(scene_id: int = 0) -> str:
    """为分镜格子生成漫画图片。

    根据 sd_prompt 调用云端生图 API（或生成占位图）。
    每格生成一张图，自动匹配风格对应的画幅比例。
    生成完成后图片路径保存在对应的 Panel 中。

    Args:
        scene_id: 场景 id。0 表示生成全部场景的图片。
    """
    data = _ctx.data
    img_gen = _ctx.img_gen

    ratio_map = {"9:16": (576, 1024), "4:3": (1024, 768), "16:9": (1024, 576), "1:1": (1024, 1024)}
    sp = data.style_profile
    width, height = ratio_map.get(sp.aspect_ratio, (1024, 1024)) if sp else (1024, 1024)

    images_dir = os.path.join(data.output_dir, "images")
    os.makedirs(images_dir, exist_ok=True)

    scenes_to_process = [s for s in data.scenes if scene_id == 0 or s.id == scene_id]
    if not scenes_to_process:
        return json.dumps({"error": f"场景 {scene_id} 不存在"})

    generated = 0
    for scene in scenes_to_process:
        for panel in scene.panels:
            ref_path = ""
            for char_name in panel.character_refs:
                for c in data.characters:
                    if c.name == char_name and c.reference_image_path:
                        ref_path = c.reference_image_path
                        break

            path = img_gen.generate(
                prompt=panel.sd_prompt,
                output_dir=images_dir,
                width=width, height=height,
                reference_image_path=ref_path,
            )
            panel.generated_image_path = path
            panel.status = "generated"
            generated += 1

    return json.dumps({
        "status": "ok",
        "generated": generated,
        "message": f"生成了 {generated} 张图片。接下来请调用 compile_comic 排版输出。",
    }, ensure_ascii=False)


@tool
def compile_comic() -> str:
    """将已生成的图片拼接为最终漫画。

    根据风格选择排版模式：
    - webtoon/gufeng: 条漫纵向拼接 + 场景标题 + 对话框 + 格编号
    - manga: 格阵排版（暂回退为条漫模式）

    必须在 generate_images 之后调用。
    """
    import os as _os
    from PIL import Image, ImageDraw, ImageFont

    data = _ctx.data

    PANEL_GAP = 20
    MARGIN = 40
    BUBBLE_PADDING = 12
    MAX_SCROLL_WIDTH = 800

    def _load_font(size: int):
        for fp in ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simsun.ttc", "C:/Windows/Fonts/arial.ttf"]:
            if _os.path.exists(fp):
                try:
                    return ImageFont.truetype(fp, size)
                except Exception:
                    continue
        return ImageFont.load_default()

    pages = []
    for scene in data.scenes:
        panel_imgs = []
        for panel in scene.panels:
            if panel.generated_image_path and _os.path.exists(panel.generated_image_path):
                panel_imgs.append((panel, Image.open(panel.generated_image_path)))

        if not panel_imgs:
            continue

        scene_width = MAX_SCROLL_WIDTH
        resized = []
        total_h = 0
        for panel, img in panel_imgs:
            ratio = scene_width / img.width
            nh = int(img.height * ratio)
            img = img.resize((scene_width, nh), Image.LANCZOS)
            resized.append((panel, img))
            total_h += nh + PANEL_GAP

        font = _load_font(18)
        font_small = _load_font(14)
        total_h += 80 * len(resized)

        canvas = Image.new("RGB", (scene_width, total_h + MARGIN * 2), color=(30, 30, 40))
        draw = ImageDraw.Draw(canvas)
        y = MARGIN

        for panel, img in resized:
            canvas.paste(img, (0, y))
            ph = img.height

            if panel == resized[0][0]:
                title_font = _load_font(22)
                draw.text((20, y + 10), f"场景: {scene.title}", fill=(255, 255, 255), font=title_font)

            if panel.dialogue:
                text = panel.dialogue
                max_tw = scene_width - MARGIN * 2 - BUBBLE_PADDING * 2 - 40
                lines = []
                cur = ""
                for ch in list(text):
                    test = cur + ch
                    if draw.textbbox((0, 0), test, font=font)[2] > max_tw:
                        lines.append(cur)
                        cur = ch
                    else:
                        cur = test
                if cur:
                    lines.append(cur)

                lh = draw.textbbox((0, 0), "啊", font=font)[3] + 4
                th = lh * len(lines)
                bh = th + BUBBLE_PADDING * 2
                bx, bw = MARGIN + 20, scene_width - MARGIN * 2 - 40

                draw.rounded_rectangle(
                    [bx, y + ph + 10, bx + bw, y + ph + 10 + bh],
                    radius=16, fill=(255, 255, 255, 230), outline=(60, 60, 60), width=2,
                )
                ty = y + ph + 10 + BUBBLE_PADDING
                for line in lines:
                    tw = draw.textbbox((0, 0), line, font=font)[2]
                    draw.text(((scene_width - tw) // 2, ty), line, fill=(20, 20, 20), font=font)
                    ty += lh
                y += ph + bh + PANEL_GAP + 10
            else:
                y += ph + PANEL_GAP

            draw.text((scene_width - 80, y - 30), f"格{panel.panel_number}", fill=(150, 150, 170), font=font_small)

        comics_dir = _os.path.join(data.output_dir, "comics")
        _os.makedirs(comics_dir, exist_ok=True)
        op = _os.path.join(comics_dir, f"scene_{scene.id:02d}.png")
        canvas.save(op, "PNG")
        pages.append(ComicPage(page_number=scene.id, image_path=op))

    data.pages = pages
    return json.dumps({
        "status": "ok",
        "page_count": len(pages),
        "files": [p.image_path for p in pages],
        "message": f"漫画排版完成！共 {len(pages)} 页，输出目录：{data.output_dir}",
    }, ensure_ascii=False)


@tool
def save_project() -> str:
    """将当前项目状态保存到 JSON 文件。可在任何阶段调用。"""
    saved = []

    # 保存全书数据 + 同步注册表
    if _ctx.novel:
        novel_path = os.path.join(_ctx.novel.output_dir, "novel.json")
        _ctx.novel.save(novel_path)
        saved.append(novel_path)

        # 同步注册表（更新章节数、风格、访问时间）
        style = _ctx.novel.style_profile.name if _ctx.novel.style_profile else ""
        try:
            register_novel(
                _ctx.novel.file_path,
                _ctx.novel.title,
                _ctx.novel.total_chapters,
                _ctx.novel.output_dir,
                style,
            )
        except Exception:
            pass  # 注册表更新失败不影响主流程

    # 保存当前章数据
    if _ctx.chapter_data:
        ch_path = os.path.join(_ctx.chapter_data.output_dir, "chapter_data.json")
        _ctx.chapter_data.save(ch_path)
        saved.append(ch_path)

    return json.dumps({
        "status": "ok",
        "saved_files": saved,
        "novel_title": _ctx.novel.title if _ctx.novel else "",
        "chapter": _ctx.chapter_data.title if _ctx.chapter_data else "",
        "stage": _ctx.chapter_data.current_stage if _ctx.chapter_data else 0,
    }, ensure_ascii=False)


# ============================================================
# Agent 构建
# ============================================================

def build_agent():
    """使用 AgentFlow AgentBuilder 构建 Novel2Comic Agent。"""
    api_key = os.getenv("AGENTFLOW_API_KEY", "")
    base_url = os.getenv("AGENTFLOW_BASE_URL", "https://api.deepseek.com/v1")
    model = os.getenv("AGENTFLOW_MODEL", "deepseek-chat")
    proxy = os.getenv("AGENTFLOW_PROXY", "")

    if not api_key:
        print("[!] AGENTFLOW_API_KEY not set.")
        print("    请设置环境变量: $env:AGENTFLOW_API_KEY='sk-your-key'")
        sys.exit(1)

    # AgentFlow 的 LLM Client（Agent 的"大脑"）
    llm = OpenAIClient(api_key=api_key, model=model, base_url=base_url, proxy=proxy or None)

    # Tool 内部用的同步 OpenAI Client
    import httpx
    import openai
    http_client = httpx.Client(proxy=proxy) if proxy else None
    _ctx.openai_client = openai.OpenAI(api_key=api_key, base_url=base_url, http_client=http_client)
    _ctx.llm_model = model

    # Phase 1: 创建 UnifiedLLM（封装 _llm_chat_json）
    _ctx._llm = UnifiedLLM(_ctx.openai_client, model)

    # Phase 1: 创建 ServiceRegistry（供 Agent 工具使用）
    kg_service = KnowledgeGraphService(llm=_ctx._llm)
    _ctx.services = ServiceRegistry(
        kg=kg_service,
        image=ImageGenerationService(),
        comic=ComicCompilationService(),
        project=ProjectService(),
        search=SearchService(),
    )

    # ImageGen Adapter
    img_api_key = os.getenv("N2C_IMG_API_KEY", "")
    img_base_url = os.getenv("N2C_IMG_BASE_URL", "")
    _ctx.img_gen = ImageGenAdapter(api_key=img_api_key, base_url=img_base_url)

    # 构建 Agent
    agent = (AgentBuilder("novel2comic")
        .with_llm(llm)
        .with_skills_dir(os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills"))
        .with_skill("novel2comic")
        .with_tools(
            load_novel,
            list_novels,
            resume_novel,
            list_chapters,
            select_chapter,
            query_graph,
            query_character_relations,
            query_events,
            query_location,
            query_organization,
            query_item,
            ask_plot,
            analyze_text,
            design_characters,
            extract_scenes,
            storyboard_scene,
            generate_images,
            compile_comic,
            save_project,
        )
        .with_memory(MemoryProfile.standard())
        .with_thinking(ThinkingMode.REACT)
        .with_max_iterations(30)
        .build())

    return agent


# ============================================================
# 运行入口
# ============================================================

async def _chat_loop(agent, first_task: str, label: str = ""):
    """交互式对话循环——Agent 处理任务，然后用户可以继续对话。"""
    print(f"\n[Agent] 模式: REACT | {label}")
    print("[Agent] 输入 'quit' 或 'exit' 退出\n")

    user_input = first_task

    while True:
        result = await agent.run(user_input)

        # 打印步骤摘要
        for i, step in enumerate(result.steps):
            step_type = step.get("type", step.get("phase", "?"))
            if step_type == "tool_call":
                calls = step.get("calls", [])
                if isinstance(calls, list):
                    for c in calls:
                        name = c.get("name", c) if isinstance(c, dict) else str(c)
                        print(f"  [TOOL] {name}")
            elif step_type in ("final", "output"):
                output_preview = str(step.get("output", ""))[:150]
                if output_preview:
                    print(f"  [OUT] {output_preview}...")

        print(f"\n{result.output}\n")

        # 等待用户下一轮输入
        try:
            user_input = input("[You] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[Agent] 再见！")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("[Agent] 再见！")
            break

    return result.output


async def run_novel_agent(novel_path: str):
    """启动 Agent：加载整本小说，进入交互对话循环。"""
    agent = build_agent()

    first_task = (
        f'小说文件路径：{novel_path}\n\n'
        f'请执行以下步骤：\n'
        f"1. 调用 load_novel('{novel_path}') 加载小说\n"
        f'2. 调用 list_chapters 查看章节列表\n'
        f'3. 告诉我有哪些章节，等待我选择要生成第几章\n'
        f'4. 我选择后，用 select_chapter(N) 选中，然后按顺序执行：\n'
        f'   analyze_text -> design_characters -> extract_scenes\n'
        f'   -> storyboard_scene(每个场景) -> generate_images\n'
        f'   -> compile_comic -> save_project\n\n'
        f'注意：每步完成后简短汇报，不要长篇大论。'
        f'我说[继续第N章]你就 select_chapter(N) 然后走上述管线。'
        f'我说哪里不满意你就调整重做对应步骤。'
    )

    await _chat_loop(agent, first_task, f"加载: {os.path.basename(novel_path)}")


async def run_single_chapter(text: str, title: str = "未命名章节"):
    """启动 Agent：单章模式（兼容旧版用法）。"""
    agent = build_agent()

    project_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "projects",
        datetime.now().strftime("%Y%m%d_%H%M%S"),
    )
    os.makedirs(project_dir, exist_ok=True)

    _ctx.chapter_data = ChapterData(
        title=title,
        source_text=text,
        output_dir=project_dir,
        created_at=datetime.now().isoformat(),
    )

    first_task = (
        f'章节标题：{title}\n\n'
        f'### 小说原文\n{text[:3000]}\n\n'
        f'请按顺序执行：\n'
        f'1. analyze_text(text=原文)\n'
        f'2. design_characters()\n'
        f'3. extract_scenes()\n'
        f'4. 对每个场景调用 storyboard_scene(scene_id=N)\n'
        f'5. generate_images(scene_id=0)\n'
        f'6. compile_comic()\n'
        f'7. save_project()\n\n'
        f'每步完成后简短汇报。我说哪里不满意你就调整。'
    )

    await _chat_loop(agent, first_task, f'单章: {title}')


# ============================================================
# CLI 入口（已废弃，请使用 main.py）
# ============================================================

if __name__ == "__main__":
    import warnings
    print("=" * 60)
    print("[!] agent.py 已废弃，请使用新的入口：")
    print()
    print("  python main.py comic --novel 小说.txt")
    print("  python main.py comic --text '小说内容' --title 标题")
    print()
    print("更多功能：")
    print("  python main.py continue --novel 小说.txt")
    print("  python main.py roleplay --novel 小说.txt --character 角色名")
    print("  python main.py recommend --novel 小说.txt")
    print("  python main.py summarize --novel 小说.txt")
    print()
    print("将继续以兼容模式运行...")
    print("=" * 60)

    if len(sys.argv) < 2:
        print()
        print("用法:")
        print("  全书模式: python main.py comic --novel 小说.txt")
        print("  单章模式: python main.py comic --text '小说内容' --title 标题")
        sys.exit(1)

    if sys.argv[1] == "--novel":
        if len(sys.argv) < 3:
            print("[!] 请指定小说文件路径: python main.py comic --novel 小说.txt")
            sys.exit(1)
        novel_path = sys.argv[2]
        asyncio.run(run_novel_agent(novel_path))
    else:
        input_text = sys.argv[1]
        chapter_title = sys.argv[2] if len(sys.argv) > 2 else "未命名章节"

        if os.path.isfile(input_text):
            input_text = _read_text_file(input_text)
            if len(sys.argv) <= 2:
                chapter_title = os.path.splitext(os.path.basename(sys.argv[1]))[0]

        asyncio.run(run_single_chapter(input_text, chapter_title))
