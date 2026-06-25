# -*- coding: utf-8 -*-
"""角色扮演 Agent —— 使用 PromptContext 统一装配。"""

import json
from typing import TYPE_CHECKING

from agentflow.runtime.toolkit import tool

from novel2comic.src.agents.base_agent import BaseAgent
from novel2comic.src.prompt_context import PromptNeed

if TYPE_CHECKING:
    from novel2comic.src.context import GlobalContext, ServiceRegistry
    from novel2comic.src.llm import UnifiedLLM


class RolePlayAgent(BaseAgent):
    """角色扮演 Agent。

    角色扮演的完整状态存储在 self._memory.roleplay (RolePlayState) 中。
    切换 Agent 再切回来，对话历史和角色情感状态不丢失。
    """

    SKILL_NAME = "roleplay"

    def __init__(self, ctx, services, llm, memory=None):
        super().__init__(ctx, services, llm, memory)
        self._kg = services.kg

    @property
    def rp(self):
        """快捷访问角色扮演状态。"""
        return self._memory.roleplay

    def _build_tools(self) -> list:
        ctx = self._ctx
        kg = self._kg
        llm = self._llm
        build_prompt = self._build_prompt
        rp = self._memory.roleplay

        @tool
        def start_conversation(character_name: str, scenario: str = "") -> str:
            graph = ctx.novel.story_graph if ctx.novel else None
            if not graph:
                return json.dumps({"error": "请先加载小说"})

            person = kg.get_person(graph, character_name)
            if not person:
                return json.dumps({"error": f"角色「{character_name}」不存在"})

            person_data = {
                "name": person.name, "role": person.role_type,
                "faction": person.faction, "importance": person.importance,
                "status": person.status, "description": person.description,
                "first_appearance_chapter": person.first_appearance_chapter,
            }

            rp.active_character = character_name
            rp.story_timeline_point = person.first_appearance_chapter
            state = rp.get_character_state(character_name)
            state.location = scenario or "未知地点"

            if graph:
                knowledge = rp.build_knowledge_filter(character_name, graph)
                rp.character_knowledge[character_name] = knowledge
            else:
                knowledge = ""

            relations_text = ""
            if graph:
                for r in kg.get_relations(graph, character_name)[:10]:
                    target = r.to_char if r.from_char == character_name else r.from_char
                    relations_text += (
                        f"- 与 {target}: {r.relation_type}"
                        + (f" ({r.current_tension})" if r.current_tension else "")
                        + (f" | 亲密度:{r.intimacy:+d}" if r.intimacy else "")
                        + (" [隐藏]" if not r.public_knowledge else "") + "\n"
                    )

            result = llm.chat_json(**build_prompt(PromptNeed.of(
                "roleplay_system",
                inputs={
                    "character_name": character_name,
                    "character_profile": json.dumps(person_data, ensure_ascii=False, indent=2),
                    "relations_text": relations_text,
                    "world_context": ctx.novel.title if ctx.novel else "未知",
                    "knowledge_section": knowledge[:2000],
                },
            )).__dict__)

            rp.add_turn("system", f"[对话开始] 场景: {scenario or '未指定'}")
            rp.add_turn(character_name, f"(进入场景{'：' + scenario if scenario else ''})")

            return json.dumps({
                "status": "ok", "character": character_name,
                "role": person_data.get("role", ""), "scenario": scenario,
                "system_prompt": result.get("system_prompt", ""),
                "message": f"已进入角色「{character_name}」。请开始对话。",
            }, ensure_ascii=False)

        @tool
        def respond(message: str) -> str:
            char_name = rp.active_character
            if not char_name:
                return json.dumps({"error": "没有活跃角色"})
            state = rp.get_character_state(char_name)
            recent = rp.get_conversation_context(max_turns=15)
            rp.add_turn("user", message)

            result = llm.chat_json(**build_prompt(PromptNeed.of(
                "roleplay_respond",
                inputs={
                    "character_name": char_name,
                    "mood": state.mood, "location": state.location,
                    "goals": ", ".join(state.goals) if state.goals else "无",
                    "physical_state": state.physical_state,
                    "recent_history": recent,
                    "knowledge_section": rp.character_knowledge.get(char_name, "")[:1500],
                    "user_message": message,
                },
            )).__dict__)

            reply = result.get("reply", "")
            emotion = result.get("emotion", state.mood)
            action = result.get("action", "")
            rp.add_turn(char_name, reply, emotion=emotion, action=action)
            if result.get("mood_change"):
                rp.update_mood(char_name, result["mood_change"])

            return json.dumps({"status": "ok", "character": char_name,
                               "reply": reply, "emotion": emotion, "action": action},
                              ensure_ascii=False)

        @tool
        def switch_character(character_name: str) -> str:
            old = rp.active_character
            if old:
                old_state = rp.get_character_state(old)
                rp.add_turn("system", f"[{old} 暂时离开，情绪: {old_state.mood}]")

            graph = ctx.novel.story_graph if ctx.novel else None
            if not graph:
                return json.dumps({"error": "请先加载小说"})
            person = kg.get_person(graph, character_name)
            if not person:
                return json.dumps({"error": f"角色「{character_name}」不存在"})

            rp.active_character = character_name
            rp.story_timeline_point = person.first_appearance_chapter
            state = rp.get_character_state(character_name)
            if not state.location:
                state.location = "未知地点"
            if character_name not in rp.character_knowledge:
                rp.character_knowledge[character_name] = (
                    rp.build_knowledge_filter(character_name, graph))
            rp.add_turn("system", f"[切换到 {character_name}]")

            return json.dumps({"status": "ok", "previous_character": old,
                               "current_character": character_name,
                               "current_mood": state.mood,
                               "message": f"已从 {old} 切换到 {character_name}"},
                              ensure_ascii=False)

        @tool
        def reflect_on_relationship(target_name: str) -> str:
            char_name = rp.active_character
            if not char_name:
                return json.dumps({"error": "没有活跃角色"})
            graph = ctx.novel.story_graph if ctx.novel else None

            rel_data = None
            if graph:
                edge = graph.get_relationship_edge(char_name, target_name)
                if edge:
                    rel_data = {
                        "type": edge.relation_type, "intimacy": edge.intimacy,
                        "tension": edge.current_tension, "power": edge.power_dynamic,
                        "public": edge.public_knowledge, "history": edge.shared_history,
                    }

            state = rp.get_character_state(char_name)

            result = llm.chat_json(**build_prompt(PromptNeed.of(
                "roleplay_reflect",
                inputs={
                    "character_name": char_name,
                    "mood": state.mood,
                    "target_name": target_name,
                    "relationship_data": json.dumps(rel_data, ensure_ascii=False) if rel_data else f"你和 {target_name} 没有直接的已知关系。",
                },
            )).__dict__)

            reflection = result.get("reflection", "")
            rp.add_turn(char_name, f"[谈起 {target_name}]: {reflection}")

            return json.dumps({"status": "ok", "character": char_name,
                               "target": target_name, "reflection": reflection,
                               "relationship_data": rel_data}, ensure_ascii=False)

        @tool
        def advance_scenario(event_description: str) -> str:
            char_name = rp.active_character
            if not char_name:
                return json.dumps({"error": "没有活跃角色"})
            state = rp.get_character_state(char_name)
            recent = rp.get_conversation_context(max_turns=10)

            result = llm.chat_json(**build_prompt(PromptNeed.of(
                "roleplay_advance",
                inputs={
                    "character_name": char_name,
                    "mood": state.mood, "location": state.location,
                    "goals": ", ".join(state.goals) if state.goals else "无",
                    "recent_history": recent,
                    "event_description": event_description,
                },
            )).__dict__)

            reaction = result.get("reaction", "")
            dialogue = result.get("dialogue", "")
            new_mood = result.get("new_mood", state.mood)

            rp.add_turn("system", f"[事件]: {event_description}")
            if dialogue:
                rp.add_turn(char_name, dialogue, emotion=new_mood,
                            action=result.get("action_taken", ""))
            else:
                rp.add_turn(char_name, f"({reaction})", emotion=new_mood)

            if new_mood != state.mood:
                rp.update_mood(char_name, new_mood, event_description)

            return json.dumps({"status": "ok", "character": char_name,
                               "reaction": reaction, "dialogue": dialogue,
                               "action_taken": result.get("action_taken", ""),
                               "new_mood": new_mood,
                               "thoughts": result.get("thoughts", "")},
                              ensure_ascii=False)

        return [start_conversation, respond, switch_character,
                reflect_on_relationship, advance_scenario]
