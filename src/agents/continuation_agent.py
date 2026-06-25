# -*- coding: utf-8 -*-
"""续写 Agent —— 使用 PromptContext 统一装配。"""

import json
from typing import TYPE_CHECKING

from agentflow.runtime.toolkit import tool

from novel2comic.src.agents.base_agent import BaseAgent
from novel2comic.src.prompt_context import PromptNeed

if TYPE_CHECKING:
    from novel2comic.src.context import GlobalContext, ServiceRegistry
    from novel2comic.src.llm import UnifiedLLM


class ContinuationAgent(BaseAgent):
    SKILL_NAME = "continuation"

    def __init__(self, ctx, services, llm, memory=None):
        super().__init__(ctx, services, llm, memory)
        self._kg = services.kg
        self._project = services.project

    def _build_tools(self) -> list:
        ctx = self._ctx
        kg = self._kg
        llm = self._llm
        build_prompt = self._build_prompt
        fetch_kg = self._fetch_kg

        @tool
        def plan_arc(goal: str, current_chapter: int) -> str:
            graph = ctx.novel.story_graph if ctx.novel else None
            if not graph:
                return json.dumps({"error": "请先加载小说和知识图谱"})

            active_conflicts = [
                {"from": a, "to": b,
                 "tension": (edge.current_tension if (edge := graph.get_relationship_edge(a, b)) else "?")}
                for a, b in kg.enemy_pairs(graph)[:5]
            ]

            causal_events = {}
            for er in graph.event_relation_edges:
                if er.relation_type == "causes":
                    ev = graph.get_event_node(er.from_event.split(":", 1)[-1])
                    if ev:
                        causal_events[er.from_event] = {
                            "event": ev.name, "chapter": ev.chapter_end or ev.chapter_start,
                            "effect": ev.effect,
                        }

            persons = kg.get_all_persons(graph)
            char_summaries = [
                {"name": p.name, "role": p.role_type, "importance": p.importance,
                 "status": p.status, "faction": p.faction,
                 "relations_count": len(kg.get_relations(graph, p.name)),
                 "first_appearance": p.first_appearance_chapter}
                for p in persons[:15]
            ]

            kg_text = fetch_kg(["context:1000", "causes", "enemies"])

            result = llm.chat_json(**build_prompt(PromptNeed.of(
                "plan_arc",
                inputs={
                    "goal": goal,
                    "current_chapter": str(current_chapter),
                    "active_conflicts": json.dumps(active_conflicts, ensure_ascii=False),
                    "causal_events": json.dumps(causal_events, ensure_ascii=False),
                    "character_summaries": json.dumps(char_summaries, ensure_ascii=False),
                    "kg_context": kg_text,
                },
            )).__dict__)
            result["status"] = "ok"
            result["current_chapter"] = current_chapter
            return json.dumps(result, ensure_ascii=False)

        @tool
        def write_draft(outline: str, previous_chapter_text: str = "") -> str:
            if not previous_chapter_text and ctx.novel:
                last_idx = ctx.novel.story_graph.last_updated_chapter if ctx.novel.story_graph else 0
                for ch in (ctx.novel.chapters or []):
                    if ch.index == last_idx:
                        previous_chapter_text = ch.content[-2000:]
                        break

            result = llm.chat_json(**build_prompt(PromptNeed.of(
                "write_draft",
                inputs={
                    "outline": outline,
                    "previous_chapter_ending": previous_chapter_text,
                },
                max_tokens_override=8000,
            )).__dict__)
            result["status"] = "ok"
            return json.dumps(result, ensure_ascii=False)

        @tool
        def review_consistency(draft: str) -> str:
            graph = ctx.novel.story_graph if ctx.novel else None
            if not graph:
                return json.dumps({"status": "ok", "issues": [],
                                   "message": "没有知识图谱数据"})

            persons = kg.get_all_persons(graph)
            mentioned = [p for p in persons if p.name in draft]
            char_states = {}
            for c in mentioned:
                rels = kg.get_relations(graph, c.name)
                char_states[c.name] = {
                    "status": c.status, "faction": c.faction,
                    "first_appearance": c.first_appearance_chapter,
                    "relations": [
                        {"with": r.to_char if r.from_char == c.name else r.from_char,
                         "type": r.relation_type, "intimacy": r.intimacy,
                         "tension": r.current_tension}
                        for r in rels[:5]
                    ],
                }

            kg_text = fetch_kg(["context:800"])

            result = llm.chat_json(**build_prompt(PromptNeed.of(
                "review_consistency",
                inputs={
                    "draft": draft[:3000],
                    "character_states": json.dumps(char_states, ensure_ascii=False),
                    "kg_context": kg_text,
                },
            )).__dict__)
            result["status"] = "ok"
            return json.dumps(result, ensure_ascii=False)

        @tool
        def revise_draft(draft: str, feedback: str) -> str:
            result = llm.chat_json(**build_prompt(PromptNeed.of(
                "revise_draft",
                inputs={"draft": draft, "feedback": feedback},
                max_tokens_override=8000,
            )).__dict__)
            result["status"] = "ok"
            return json.dumps(result, ensure_ascii=False)

        return [plan_arc, write_draft, review_consistency, revise_draft]
