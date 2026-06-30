# -*- coding: utf-8 -*-
"""知识图谱服务 —— 所有 Agent 的"剧本圣经"。

封装 src/knowledge_graph.py 中的函数，
提供统一的查询/提取/序列化接口。
替代原来 7 个查询 Agent 工具。
"""

from typing import Optional, TYPE_CHECKING

from ..knowledge_graph import (
    extract_story_graph_from_text,
    update_story_graph_with_chapter,
    graph_to_context,
)
from ..models import StoryGraph

if TYPE_CHECKING:
    from ..models import (
        Novel, CharacterNode, EventNode,
        LocationNode, OrganizationNode, ItemNode,
        RelationshipEdge, ChapterInfo,
    )
    from ..llm import UnifiedLLM


class KnowledgeGraphService:
    """共享知识图谱服务。

    封装 knowledge_graph.py 中的 LLM 提示词、提取函数和图算法。
    由 Agent 用于 KG 读取/查询。不维护独立持久化——KG 挂载在 Novel 上。
    """

    def __init__(self, llm: "UnifiedLLM" = None):
        self._llm = llm

    # ================================================================
    # 生命周期
    # ================================================================

    def extract_initial(
        self,
        text: str,
        total_chapters: int = 1,
        chapter_indices: list = None,
    ) -> "StoryGraph":
        """[deprecated] 全量采样提取。请使用 extract_incremental。"""
        return self._do_extract(text[:12000])

    def extract_incremental(
        self,
        chapters: list,
        batch_size: int = 10,
        max_chars_per_batch: int = 12000,
    ) -> "StoryGraph":
        """分批增量构建知识图谱。

        - 第一批做全量提取建立基础 KG
        - 后续批次做增量更新
        - batch_size 控制每批包含几章，平衡速度与精度

        Args:
            chapters: ChapterInfo 列表
            batch_size: 每批处理的章节数（默认 10）
            max_chars_per_batch: 每批最大字符数

        Returns:
            完整的 StoryGraph
        """
        graph = StoryGraph()
        total = len(chapters)
        if total == 0:
            return graph

        client = self._llm._client if self._llm else None
        model = self._llm.model if self._llm else "deepseek-chat"

        # 将章节分组
        batches = []
        for i in range(0, total, batch_size):
            batch = chapters[i:i + batch_size]
            batches.append(batch)

        for bi, batch in enumerate(batches):
            ch_range = f"第{batch[0].index}-{batch[-1].index}章"
            batch_text = self._join_chapters(batch, max_chars_per_batch)
            progress = f"[KG] ({bi+1}/{len(batches)}) {ch_range}"

            if bi == 0:
                # 第一批：全量提取
                print(f"{progress} 全量提取...", end=" ", flush=True)
                try:
                    partial = extract_story_graph_from_text(
                        batch_text, openai_client=client, model=model,
                    )
                    graph = self._merge_graphs(graph, partial)
                    print(f"✓ ({partial.total_node_count} 节点)")
                except Exception as e:
                    print(f"✗ ({e})")
            else:
                # 后续批次：增量更新
                print(f"{progress} 增量更新...", end=" ", flush=True)
                try:
                    before = graph.total_node_count
                    last_ch = batch[-1].index
                    graph = update_story_graph_with_chapter(
                        graph, batch_text, last_ch,
                        openai_client=client, model=model,
                    )
                    added = graph.total_node_count - before
                    print(f"✓ (+{added} 节点)")
                except Exception as e:
                    print(f"✗ ({e})")

            # 每批后打印当前 KG 摘要
            if graph.person_nodes:
                top = sorted(graph.person_nodes, key=lambda n: -n.importance)[:5]
                names = ", ".join(f"{n.name}({n.importance})" for n in top)
                print(f"      角色: {names} | "
                      f"事件: {len(graph.event_nodes)} | "
                      f"关系: {len(graph.relationship_edges)}")

        return graph

    @staticmethod
    def _join_chapters(chapters: list, max_chars: int) -> str:
        """将多个章节拼接为一批文本，不超过 max_chars。"""
        parts = []
        total = 0
        per_ch = max_chars // max(len(chapters), 1)
        for ch in chapters:
            text = (ch.content or "")[:per_ch]
            if text.strip():
                parts.append(f"第{ch.index}章 {ch.title}\n{text}")
                total += len(text)
                if total >= max_chars:
                    break
        return "\n\n".join(parts)

    def update_with_chapter(
        self,
        graph: "StoryGraph",
        chapter_text: str,
        chapter_index: int,
    ) -> "StoryGraph":
        """对新章节进行增量知识图谱更新。"""
        return update_story_graph_with_chapter(
            graph, chapter_text, chapter_index,
            openai_client=self._llm._client if self._llm else None,
            model=self._llm.model if self._llm else "deepseek-chat",
        )

    # ── 内部 ──

    def _do_extract(self, text: str) -> "StoryGraph":
        """单次全量提取。"""
        return extract_story_graph_from_text(
            text,
            openai_client=self._llm._client if self._llm else None,
            model=self._llm.model if self._llm else "deepseek-chat",
        )

    @staticmethod
    def _merge_graphs(base: "StoryGraph", addition: "StoryGraph") -> "StoryGraph":
        """合并两个 StoryGraph（将 addition 的节点和边加入 base）。"""
        for pn in addition.person_nodes:
            if not base.get_person_node(pn.name):
                base.add_person_node(pn)
        for en in addition.event_nodes:
            if not base.get_event_node(en.name):
                base.add_event_node(en)
        for ln in addition.location_nodes:
            if not base.get_location_node(ln.name):
                base.add_location_node(ln)
        for on in addition.org_nodes:
            if not base.get_org_node(on.name):
                base.add_org_node(on)
        for it in addition.item_nodes:
            if not base.get_item_node(it.name):
                base.add_item_node(it)
        for re in addition.relationship_edges:
            existing = base.get_relationship_edge(re.from_char, re.to_char)
            if not existing:
                base.add_relationship_edge(re)
        for pe in addition.participates_edges:
            base.add_participates_edge(pe)
        for le in addition.located_at_edges:
            base.add_located_at_edge(le)
        for er in addition.event_relation_edges:
            base.add_event_relation_edge(er)
        base.last_updated_chapter = max(
            base.last_updated_chapter, addition.last_updated_chapter,
        )
        return base

    # ================================================================
    # 上下文生成
    # ================================================================

    def get_context(self, graph, max_chars: int = 800) -> str:
        """获取格式化的 LLM 上下文（调用 graph_to_context）。"""
        return graph_to_context(graph, max_chars_per_section=max_chars)

    # ================================================================
    # 人物查询
    # ================================================================

    def get_person(self, graph, name: str):
        """按名称获取人物节点。"""
        return graph.get_person_node(name) if graph else None

    def get_all_persons(self, graph) -> list:
        """获取所有人物节点。"""
        return graph.person_nodes if graph else []

    def get_relations(self, graph, name: str) -> list:
        """获取指定人物的所有关系边。"""
        if not graph:
            return []
        results = []
        for edge in graph.relationship_edges:
            if edge.from_char == name or edge.to_char == name:
                results.append(edge)
        return results

    # ================================================================
    # 事件查询
    # ================================================================

    def get_events(self, graph, character_name: str = "") -> list:
        """获取事件列表，可按角色过滤。"""
        if not graph:
            return []
        if character_name:
            return graph.character_events(character_name)
        return graph.event_nodes

    def get_event_timeline(self, graph) -> list:
        """获取按章节排序的事件时间线。"""
        return graph.event_timeline() if graph else []

    # ================================================================
    # 地点查询
    # ================================================================

    def get_location(self, graph, name: str):
        """按名称获取地点节点。"""
        return graph.get_location_node(name) if graph else None

    def get_location_hierarchy(self, graph) -> dict:
        """获取地点层级树。"""
        return graph.location_hierarchy() if graph else {}

    # ================================================================
    # 组织查询
    # ================================================================

    def get_org_members(self, graph, name: str) -> dict:
        """获取组织成员信息。"""
        return graph.org_members(name) if graph else {}

    # ================================================================
    # 物品查询
    # ================================================================

    def get_item(self, graph, name: str):
        """按名称获取物品节点。"""
        return graph.get_item_node(name) if graph else None

    def get_item_owners(self, graph, name: str) -> list:
        """获取物品所有权历史。"""
        return graph.item_owners(name) if graph else []

    # ================================================================
    # 分镜指导
    # ================================================================

    def get_storyboard_hints(self, graph, char_a: str, char_b: str) -> str:
        """获取两个角色之间的分镜指导。"""
        return graph.get_storyboard_hints(char_a, char_b) if graph else ""

    # ================================================================
    # 图算法
    # ================================================================

    def shortest_path(self, graph, from_name: str, to_name: str,
                      from_type: str = "person", to_type: str = "person") -> list:
        """查找两个实体间的最短路径。"""
        return graph.shortest_path(from_name, to_name, from_type, to_type) if graph else []

    def centrality_ranking(self, graph, top_k: int = 10) -> list:
        """度中心性排名。"""
        return graph.centrality_ranking(top_k) if graph else []

    def faction_groups(self, graph) -> dict:
        """按派系分组角色。"""
        return graph.faction_groups() if graph else {}

    def enemy_pairs(self, graph) -> list:
        """获取所有敌对关系对。"""
        return graph.enemy_pairs() if graph else []

    # ================================================================
    # 情节问答
    # ================================================================

    def ask_plot(self, graph, novel, question: str) -> dict:
        """基于知识图谱回答情节问题。

        Note: 需要外部注入 search 和 llm 服务
        """
        # 此方法在 Phase 1 暂不依赖外部服务，
        # 保留接口供 Agent 调用
        raise NotImplementedError(
            "ask_plot 需要 LLM 客户端支持，请在 Agent 层调用"
        )

    # ================================================================
    # 兼容旧代码的原始图访问
    # ================================================================

    def get_raw_graph(self, novel) -> Optional["StoryGraph"]:
        """获取原始 StoryGraph 对象（兼容旧代码）。"""
        if novel and novel.story_graph:
            return novel.story_graph
        return None
