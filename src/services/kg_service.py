# -*- coding: utf-8 -*-
"""知识图谱服务 —— 所有 Agent 的"剧本圣经"。

封装 src/knowledge_graph.py 中的函数，
提供统一的查询/提取/序列化接口。
替代原来 7 个查询 Agent 工具。
"""

from typing import Optional, TYPE_CHECKING

from novel2comic.src.knowledge_graph import (
    extract_story_graph_from_text,
    update_story_graph_with_chapter,
    graph_to_context,
)

if TYPE_CHECKING:
    from novel2comic.src.models import (
        Novel, StoryGraph, CharacterNode, EventNode,
        LocationNode, OrganizationNode, ItemNode,
        RelationshipEdge, ChapterInfo,
    )
    from novel2comic.src.llm import UnifiedLLM


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
        """对全书进行首次知识图谱提取。

        复用 agent.py 中的采样策略：每章最多 10 段各 1500 字。
        """
        # 采样策略：每章取前 10 段，每段最多 1500 字符
        samples = []
        lines = text.split("\n")
        paragraph_count = 0
        current_sample = ""
        for line in lines:
            line = line.strip()
            if not line:
                if current_sample:
                    samples.append(current_sample[:1500])
                    current_sample = ""
                    paragraph_count += 1
                    if paragraph_count >= 10 * total_chapters:
                        break
            else:
                if current_sample:
                    current_sample += "\n" + line
                else:
                    current_sample = line
        if current_sample and paragraph_count < 10 * total_chapters:
            samples.append(current_sample[:1500])

        sample_text = "\n\n---\n\n".join(samples)

        # extract_story_graph_from_text 的真实签名：
        # (text, openai_client, model="deepseek-chat", temperature=0.3)
        return extract_story_graph_from_text(
            sample_text,
            openai_client=self._llm._client if self._llm else None,
            model=self._llm.model if self._llm else "deepseek-chat",
        )

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
