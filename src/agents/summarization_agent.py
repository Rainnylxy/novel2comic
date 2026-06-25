# -*- coding: utf-8 -*-
"""摘要 Agent —— 使用 PromptContext 统一装配。"""

import json
from typing import TYPE_CHECKING

from agentflow.runtime.toolkit import tool

from novel2comic.src.agents.base_agent import BaseAgent
from novel2comic.src.prompt_context import PromptNeed

if TYPE_CHECKING:
    from novel2comic.src.context import GlobalContext, ServiceRegistry
    from novel2comic.src.llm import UnifiedLLM


class SummarizationAgent(BaseAgent):
    SKILL_NAME = "summarization"

    def _build_tools(self) -> list:
        ctx = self._ctx
        kg = self._services.kg
        llm = self._llm
        build_prompt = self._build_prompt
        fetch_kg = self._fetch_kg

        @tool
        def summarize_chapter(chapter_index: int, perspective: str = "") -> str:
            novel = ctx.novel
            if not novel:
                return json.dumps({"error": "请先加载小说"})

            chapter = None
            for ch in (novel.chapters or []):
                if ch.index == chapter_index:
                    chapter = ch
                    break
            if not chapter:
                return json.dumps({"error": f"章节 {chapter_index} 不存在"})

            chapter_text = chapter.content[:6000] if chapter.content else ""

            kg_text = ""
            if novel.story_graph:
                g = novel.story_graph
                events = [e for e in g.event_nodes
                          if e.chapter_start <= chapter_index <= (e.chapter_end or e.chapter_start)]
                if events:
                    kg_text = "## 本章事件\n" + "\n".join(
                        f"- [{e.event_type}] {e.name}: {e.summary[:80]}" for e in events[:5])

            if perspective == "layered":
                task_type = "summarize_chapter_layered"
            elif perspective:
                task_type = "summarize_chapter_default"
            else:
                task_type = "summarize_chapter_default"

            result = llm.chat_json(**build_prompt(PromptNeed.of(
                task_type,
                inputs={
                    "chapter_index": str(chapter_index),
                    "chapter_title": chapter.title,
                    "chapter_text": chapter_text,
                    "kg_context": kg_text,
                    "perspective": perspective,
                },
            )).__dict__)
            result["status"] = "ok"
            result["chapter_index"] = chapter_index
            return json.dumps(result, ensure_ascii=False)

        @tool
        def summarize_character(name: str) -> str:
            novel = ctx.novel
            if not novel or not novel.story_graph:
                return json.dumps({"error": "请先加载小说和知识图谱"})

            g = novel.story_graph
            person = kg.get_person(g, name)
            if not person:
                return json.dumps({"error": f"角色「{name}」不存在"})

            events = kg.get_events(g, name)
            relations = kg.get_relations(g, name)
            centrality = kg.centrality_ranking(g, top_k=50)
            rank = next((i+1 for i, (n, _) in enumerate(centrality) if n == name), None)

            char_data = {
                "name": person.name, "role": person.role_type,
                "faction": person.faction, "importance": person.importance,
                "status": person.status,
                "first_appearance": person.first_appearance_chapter,
                "description": person.description,
                "events_participated": len(events),
                "relationships_count": len(relations),
                "centrality_rank": rank,
            }
            event_timeline = [
                {"name": e.name, "type": e.event_type,
                 "chapter": e.chapter_start, "summary": e.summary[:80]}
                for e in sorted(events, key=lambda x: x.chapter_start)[:15]
            ]
            rel_summary = [
                {"with": r.to_char if r.from_char == name else r.from_char,
                 "type": r.relation_type, "intimacy": r.intimacy,
                 "tension": r.current_tension, "established": r.established_chapter,
                 "history": r.shared_history}
                for r in relations[:10]
            ]

            kg_text = fetch_kg(["context:600"])

            result = llm.chat_json(**build_prompt(PromptNeed.of(
                "summarize_character",
                inputs={
                    "character_data": json.dumps(char_data, ensure_ascii=False, indent=2),
                    "event_timeline": json.dumps(event_timeline, ensure_ascii=False, indent=2),
                    "relation_summary": json.dumps(rel_summary, ensure_ascii=False, indent=2),
                    "kg_context": kg_text,
                },
            )).__dict__)
            result["status"] = "ok"
            return json.dumps(result, ensure_ascii=False)

        @tool
        def analyze_theme() -> str:
            novel = ctx.novel
            if not novel or not novel.story_graph:
                return json.dumps({"error": "请先加载小说"})
            g = novel.story_graph

            stats = {
                "title": novel.title,
                "total_chapters": len(novel.chapters) if novel.chapters else 0,
                "character_count": len(kg.get_all_persons(g)),
                "event_count": len(kg.get_event_timeline(g)),
                "factions": list(kg.faction_groups(g).keys()),
                "most_central": kg.centrality_ranking(g, top_k=10),
            }

            kg_text = fetch_kg(["context:1200", "timeline", "factions"])

            result = llm.chat_json(**build_prompt(PromptNeed.of(
                "analyze_theme",
                inputs={
                    "novel_stats": json.dumps(stats, ensure_ascii=False, indent=2),
                    "kg_context": kg_text,
                },
                max_tokens_override=6000,
            )).__dict__)
            result["status"] = "ok"
            return json.dumps(result, ensure_ascii=False)

        return [summarize_chapter, summarize_character, analyze_theme]
