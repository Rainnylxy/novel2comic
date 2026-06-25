# -*- coding: utf-8 -*-
"""推荐 Agent —— 使用 PromptContext 统一装配。"""

import json
from typing import TYPE_CHECKING

from agentflow.runtime.toolkit import tool

from novel2comic.src.agents.base_agent import BaseAgent
from novel2comic.src.prompt_context import PromptNeed

if TYPE_CHECKING:
    from novel2comic.src.context import GlobalContext, ServiceRegistry
    from novel2comic.src.llm import UnifiedLLM


class RecommendationAgent(BaseAgent):
    SKILL_NAME = "recommendation"

    def _build_tools(self) -> list:
        ctx = self._ctx
        kg = self._services.kg
        llm = self._llm
        build_prompt = self._build_prompt

        @tool
        def search_catalog(preferences: str) -> str:
            novel = ctx.novel
            features = {}
            if novel and novel.story_graph:
                g = novel.story_graph
                persons = kg.get_all_persons(g)
                features = {
                    "title": novel.title,
                    "total_chapters": len(novel.chapters) if novel.chapters else 0,
                    "character_count": len(persons),
                    "main_characters": [p.name for p in sorted(
                        persons, key=lambda x: x.importance, reverse=True)[:5]],
                    "factions": list(kg.faction_groups(g).keys())[:5],
                    "key_events": [{"name": e.name, "type": e.event_type}
                                   for e in kg.get_event_timeline(g)[:10]],
                    "event_types": {t: sum(1 for e in g.event_nodes
                                           if e.event_type == t)
                                    for t in set(e.event_type for e in g.event_nodes)},
                }

            result = llm.chat_json(**build_prompt(PromptNeed.of(
                "search_catalog",
                inputs={
                    "preferences": preferences,
                    "reference_features": json.dumps(features, ensure_ascii=False, indent=2),
                },
            )).__dict__)
            result["status"] = "ok"
            return json.dumps(result, ensure_ascii=False)

        @tool
        def explain_match(novel_title: str) -> str:
            result = llm.chat_json(**build_prompt(PromptNeed.of(
                "explain_match",
                inputs={"novel_title": novel_title},
            )).__dict__)
            result["status"] = "ok"
            result["title"] = novel_title
            return json.dumps(result, ensure_ascii=False)

        @tool
        def compare_novels(title_a: str, title_b: str) -> str:
            result = llm.chat_json(**build_prompt(PromptNeed.of(
                "compare_novels",
                inputs={"title_a": title_a, "title_b": title_b},
            )).__dict__)
            result["status"] = "ok"
            return json.dumps(result, ensure_ascii=False)

        return [search_catalog, explain_match, compare_novels]
