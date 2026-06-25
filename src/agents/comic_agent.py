# -*- coding: utf-8 -*-
"""漫画改编 Agent —— 使用 PromptContext 统一装配。

工具声明"需要什么"→ PromptContext 装配 prompt → llm.chat_json() 调用。
"""

import json
import os
from typing import TYPE_CHECKING

from agentflow.runtime.toolkit import tool

from novel2comic.src.agents.base_agent import BaseAgent
from novel2comic.src.prompt_context import PromptNeed
from novel2comic.src.models import (
    AnalysisResult, CharacterAppearance, CharacterSheet,
    Scene, Panel, StyleProfile,
)
from novel2comic.src.styles import detect_style

if TYPE_CHECKING:
    from novel2comic.src.context import GlobalContext, ServiceRegistry
    from novel2comic.src.llm import UnifiedLLM


class ComicAdaptationAgent(BaseAgent):
    """漫画改编 Agent —— 5 个工具，PromptContext 统一装配。"""

    SKILL_NAME = "comic_adaptation"

    def __init__(self, ctx, services, llm, memory=None):
        super().__init__(ctx, services, llm, memory)
        self._kg = services.kg
        self._image = services.image
        self._comic = services.comic
        self._project = services.project

    def _build_tools(self) -> list:
        ctx = self._ctx
        llm = self._llm
        kg = self._kg
        build_prompt = self._build_prompt     # 统一装配入口
        fetch_kg = self._fetch_kg             # KG 精确取用
        remember = self._remember

        # ============================================================
        @tool
        def analyze_text(text: str) -> str:
            """分析小说文本：风格、题材、角色预览。"""
            data = ctx.chapter_data
            sample = text[:3000]

            result = llm.chat_json(
                **build_prompt(PromptNeed.of(
                    "analyze_text",
                    inputs={"source_text": sample},
                )).__dict__,
            )

            data.analysis = AnalysisResult(
                genre_tags=result.get("genre_tags", []),
                style=result.get("style", "auto"),
                tone=result.get("tone", []),
                era=result.get("era", ""),
                pace=result.get("pace", ""),
                characters_preview=result.get("characters_preview", []),
            )

            detected = detect_style(data.analysis.genre_tags, data.analysis.pace)
            data.analysis.style = detected.name
            data.style_profile = detected

            chars = [c["name"] for c in data.analysis.characters_preview]
            remember("detected_style", detected.name, scope="agent")
            remember("characters_found", ", ".join(chars), scope="session")

            return json.dumps({
                "status": "ok", "style": detected.name,
                "genre_tags": data.analysis.genre_tags,
                "tone": data.analysis.tone, "era": data.analysis.era,
                "pace": data.analysis.pace, "characters_found": chars,
                "message": f"分析完成。风格={detected.name}，发现 {len(chars)} 个角色。"
                           f"接下来请调用 design_characters。",
            }, ensure_ascii=False)

        # ============================================================
        @tool
        def design_characters() -> str:
            """为角色创建 Character Sheet。"""
            data = ctx.chapter_data
            if not data.analysis or not data.analysis.characters_preview:
                return json.dumps({"error": "请先调用 analyze_text"})

            existing_names = {c.name for c in (data.characters or [])}
            if ctx.novel:
                existing_names.update(c.name for c in (ctx.novel.characters or []))

            pending = [
                c for c in data.analysis.characters_preview
                if c["name"] not in existing_names
            ]
            if not pending:
                return json.dumps({"status": "ok", "characters": [],
                                   "message": "所有角色已有设计"})

            kg_context = fetch_kg(["context:600", "relations:" + ",".join(
                c["name"] for c in pending)])

            result = llm.chat_json(
                **build_prompt(PromptNeed.of(
                    "design_characters",
                    inputs={
                        "pending_characters": json.dumps(pending, ensure_ascii=False),
                        "source_preview": data.source_text[:2000],
                        "kg_context": kg_context,
                    },
                )).__dict__,
            )

            chars = []
            for cd in result.get("characters", []):
                appearance = CharacterAppearance(
                    face=cd.get("appearance", {}).get("face", ""),
                    hair=cd.get("appearance", {}).get("hair", ""),
                    eyes=cd.get("appearance", {}).get("eyes", ""),
                    height_build=cd.get("appearance", {}).get("height_build", ""),
                    clothing=cd.get("appearance", {}).get("clothing", ""),
                    distinctive_features=cd.get("appearance", {}).get("distinctive_features", ""),
                    overall_impression=cd.get("appearance", {}).get("overall_impression", ""),
                )
                cs = CharacterSheet(
                    id=cd.get("id", f"char_{cd['name']}"),
                    name=cd["name"], role=cd.get("role", "supporting"),
                    appearance=appearance,
                    sd_trigger_words=cd.get("sd_trigger_words", ""),
                    design_notes=cd.get("design_notes", ""),
                )
                chars.append(cs)
                data.characters.append(cs)

            if ctx.novel:
                for cs in chars:
                    if not any(c.name == cs.name for c in (ctx.novel.characters or [])):
                        ctx.novel.characters.append(cs)

            remember("designed_characters",
                     ", ".join(c.name for c in chars), scope="session")

            return json.dumps({
                "status": "ok",
                "characters": [c.to_dict() for c in chars],
                "message": f"设计了 {len(chars)} 个角色。接下来请调用 extract_scenes。",
            }, ensure_ascii=False)

        # ============================================================
        @tool
        def extract_scenes() -> str:
            """将文本拆分为 3-8 个叙事场景。"""
            data = ctx.chapter_data

            result = llm.chat_json(
                **build_prompt(PromptNeed.of(
                    "extract_scenes",
                    inputs={
                        "chapter_title": data.title,
                        "source_text": data.source_text[:8000],
                    },
                )).__dict__,
            )

            scenes = []
            for sd in result.get("scenes", []):
                scene = Scene(
                    id=sd["id"], title=sd.get("title", ""),
                    summary=sd.get("summary", ""),
                    characters_in_scene=sd.get("characters_in_scene", []),
                    emotion_arc=sd.get("emotion_arc", ""),
                    key_dialogue=sd.get("key_dialogue", ""),
                )
                scenes.append(scene)
            data.scenes = scenes

            return json.dumps({
                "status": "ok", "scene_count": len(scenes),
                "scenes": [{"id": s.id, "title": s.title,
                            "characters": s.characters_in_scene}
                           for s in scenes],
                "message": f"拆分出 {len(scenes)} 个场景。"
                           f"接下来请用 storyboard_scene(N) 为每个场景生成分镜。",
            }, ensure_ascii=False)

        # ============================================================
        @tool
        def storyboard_scene(scene_id: int) -> str:
            """为指定场景生成 3-6 格分镜。"""
            data = ctx.chapter_data
            scene = next((s for s in data.scenes if s.id == scene_id), None)
            if not scene:
                return json.dumps({"error": f"场景 {scene_id} 不存在"})

            char_info = "\n".join(
                f"- {c.name} [{c.role}]: {c.appearance.distinctive_features}"
                f" | trigger: {c.sd_trigger_words}"
                for c in data.characters
            )

            # 场景原文
            scene_chars = scene.characters_in_scene
            relevant_lines = [
                line.strip() for line in data.source_text.split("\n")
                if line.strip() and any(ch in line for ch in scene_chars)
            ][:20]
            scene_text = "\n".join(relevant_lines)

            # KG 分镜指导
            graph_hints = ""
            for i, a in enumerate(scene_chars):
                for b in scene_chars[i+1:]:
                    hint = self._kg.get_storyboard_hints(
                        ctx.novel.story_graph, a, b,
                    ) if ctx.novel and ctx.novel.story_graph else ""
                    if hint:
                        graph_hints += f"- {a} ←→ {b}: {hint}\n"
            if graph_hints:
                graph_hints = f"## 人物关系分镜指导\n{graph_hints}\n"

            style_base = data.style_profile.sd_base_prompt if data.style_profile else ""
            ar = data.style_profile.aspect_ratio if data.style_profile else "16:9"

            result = llm.chat_json(
                **build_prompt(PromptNeed.of(
                    "storyboard_scene",
                    inputs={
                        "scene_title": scene.title,
                        "scene_summary": scene.summary,
                        "scene_emotion": scene.emotion_arc,
                        "key_dialogue": scene.key_dialogue,
                        "scene_text": scene_text,
                        "character_info": char_info,
                        "graph_hints": graph_hints,
                        "style_base": style_base,
                        "aspect_ratio": ar,
                    },
                )).__dict__,
            )

            panels = []
            for pd in result.get("panels", []):
                sd_prompt = pd.get("sd_prompt", "")
                if style_base and style_base not in sd_prompt:
                    sd_prompt = f"{style_base}, {sd_prompt}"
                for ref_name in pd.get("character_refs", []):
                    for c in data.characters:
                        if c.name == ref_name and c.sd_trigger_words:
                            if c.sd_trigger_words not in sd_prompt:
                                sd_prompt += f", {c.sd_trigger_words}"
                panel = Panel(
                    panel_number=pd.get("panel_number", 1),
                    visual_description=pd.get("visual_description", ""),
                    character_action=pd.get("character_action", ""),
                    dialogue=pd.get("dialogue", ""),
                    camera_angle=pd.get("camera_angle", "中景"),
                    mood=pd.get("mood", ""), sd_prompt=sd_prompt,
                    character_refs=pd.get("character_refs", []),
                )
                panels.append(panel)
            scene.panels = panels

            remember("last_storyboarded_scene", str(scene_id), scope="session")

            return json.dumps({
                "status": "ok", "scene_id": scene_id,
                "panel_count": len(panels),
                "panels": [{"panel": p.panel_number,
                            "description": p.visual_description[:50],
                            "camera": p.camera_angle,
                            "dialogue": p.dialogue[:30] if p.dialogue else ""}
                           for p in panels],
                "message": f"场景 {scene_id}「{scene.title}」生成了 {len(panels)} 格分镜。",
            }, ensure_ascii=False)

        # ============================================================
        @tool
        def revise_scene(scene_id: int, feedback: str) -> str:
            """根据反馈修改指定场景分镜。"""
            data = ctx.chapter_data
            scene = next((s for s in data.scenes if s.id == scene_id), None)
            if not scene:
                return json.dumps({"error": f"场景 {scene_id} 不存在"})
            if not scene.panels:
                return json.dumps({"error": f"场景 {scene_id} 还没有分镜"})

            current_panels = [
                {"panel_number": p.panel_number,
                 "visual_description": p.visual_description,
                 "character_action": p.character_action,
                 "dialogue": p.dialogue,
                 "camera_angle": p.camera_angle,
                 "mood": p.mood, "sd_prompt": p.sd_prompt}
                for p in scene.panels
            ]

            style_base = data.style_profile.sd_base_prompt if data.style_profile else ""

            result = llm.chat_json(
                **build_prompt(PromptNeed.of(
                    "revise_scene",
                    inputs={
                        "scene_title": scene.title,
                        "current_panels": json.dumps(current_panels, ensure_ascii=False, indent=2),
                        "feedback": feedback,
                    },
                )).__dict__,
            )

            new_panels = []
            for pd in result.get("panels", []):
                sd_prompt = pd.get("sd_prompt", "")
                if style_base and style_base not in sd_prompt:
                    sd_prompt = f"{style_base}, {sd_prompt}"
                for ref_name in pd.get("character_refs", []):
                    for c in data.characters:
                        if c.name == ref_name and c.sd_trigger_words:
                            if c.sd_trigger_words not in sd_prompt:
                                sd_prompt += f", {c.sd_trigger_words}"
                panel = Panel(
                    panel_number=pd.get("panel_number", 1),
                    visual_description=pd.get("visual_description", ""),
                    character_action=pd.get("character_action", ""),
                    dialogue=pd.get("dialogue", ""),
                    camera_angle=pd.get("camera_angle", "中景"),
                    mood=pd.get("mood", ""), sd_prompt=sd_prompt,
                    character_refs=pd.get("character_refs", []),
                )
                new_panels.append(panel)
            scene.panels = new_panels

            return json.dumps({
                "status": "ok", "scene_id": scene_id,
                "panel_count": len(new_panels),
                "message": f"场景 {scene_id} 已根据反馈修改。",
            }, ensure_ascii=False)

        return [
            analyze_text, design_characters, extract_scenes,
            storyboard_scene, revise_scene,
        ]

    def execute_pipeline(self) -> dict:
        """Agent 完成创意决策后，自动执行管线。"""
        data = self._ctx.chapter_data
        if not data or not data.scenes:
            return {"status": "error", "message": "没有场景数据"}

        gen_count = self._image.generate_all_panels(
            data.scenes, data.style_profile, data.output_dir)
        pages = self._comic.compile_all(data.scenes, data.output_dir)

        saved = []
        if self._ctx.novel:
            saved.append(self._project.save_novel(self._ctx.novel))
        saved.append(self._project.save_chapter_data(data))

        return {
            "status": "ok",
            "images_generated": gen_count,
            "pages_compiled": len(pages),
            "files_saved": saved,
            "message": f"管线完成：生成 {gen_count} 张图片，"
                       f"编译 {len(pages)} 页漫画，保存 {len(saved)} 个文件。",
        }
