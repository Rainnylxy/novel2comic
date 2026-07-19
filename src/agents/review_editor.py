# -*- coding: utf-8 -*-
"""ReviewEditor —— 审校 + 修订合并 Agent。

一次 LLM 调用完成:
  1. 角色 OOC 检查（对照 Voice / Boundary）
  2. 时间线一致性检查（对照 KG 事件）
  3. 设定矛盾检查（死活状态、派系从属）
  4. 对有问题的 fragment 做局部修订

不走 ReAct —— 审校和修订是一次性的约束检查 + 修正任务。
"""

import json
import logging
from typing import TYPE_CHECKING

from .base_agent import BaseAgent

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..pipeline.state import PipelineState


class ReviewEditor(BaseAgent):
    """审校修订 Agent —— 合并了原 ConsistencyReviewer + RevisionEditor。

    单次 LLM 调用：检查草稿 → 发现问题 → 就地修订 → 输出终稿。
    """

    SKILL_NAME = "review_editor"

    def _get_system_prompt(self) -> str:
        """热加载：skill body 直接注入 system prompt。"""
        return self._load_skill_body()

    def __init__(self, agent_llm, kg, state: "PipelineState"):
        super().__init__(agent_llm, kg, state)
        self._kg = kg
        self._draft_fragments: list = []
        self._chapter_plan: dict = {}

    def set_context(self, draft_fragments: list, chapter_plan: dict = None):
        """设置审校上下文。"""
        self._draft_fragments = draft_fragments
        self._chapter_plan = chapter_plan or {}
        # 从 PipelineState 同步
        self._character_profiles = self._state.character_profiles
        self._style_profile = self._state.style_profile

    async def run(self, task: str = ""):
        """审校并修订草稿。单次 LLM 调用，不走 ReAct。"""
        draft = self._draft_fragments
        if not draft:
            return json.dumps({
                "revised_fragments": [], "changes": [],
                "overall_score": 10, "status": "empty_draft",
            }, ensure_ascii=False)

        # 构建草稿文本（供 LLM 审查）
        draft_text = "\n".join(
            f"[{i}] {{{f.type}}} "
            + (f"{f.character}: " if f.character else "")
            + f.text
            for i, f in enumerate(draft)
        )

        # 构建角色约束（供 OOC 检查）
        char_specs = {}
        for name, profile in self._character_profiles.items():
            spec = {}
            if hasattr(profile, 'voice') and profile.voice:
                v = profile.voice
                if v.summary:
                    spec["voice"] = v.summary
                if v.taboo_words:
                    spec["taboo_words"] = v.taboo_words
            if hasattr(profile, 'boundary') and profile.boundary:
                b = profile.boundary
                if b.hard_rules:
                    spec["hard_rules"] = b.hard_rules
            if spec:
                char_specs[name] = spec

        # 提取 KG 事件时间线
        graph = self._state.graph
        timeline_text = ""
        if graph and graph.event_nodes:
            events = sorted(graph.event_nodes, key=lambda e: e.chapter_start or 0)
            timeline_text = "\n".join(
                f"- 第{ev.chapter_start or '?'}章「{ev.name or '?'}」: {(ev.summary or '')[:100]}"
                for ev in events[-15:]
            )

        # 提取角色状态（死活约束）
        status_text = ""
        if graph:
            persons = self._kg.get_all_persons(graph)[:20]
            status_lines = []
            for p in persons:
                if p.status and p.status != "active":
                    status_lines.append(f"- {p.name}: {p.status}")
            if status_lines:
                status_text = "已知非活跃角色:\n" + "\n".join(status_lines)

        # 风格概要
        style_text = self._style_profile.summary() if self._style_profile else ""

        cp = self._chapter_plan

        prompt = (
            f"## 待审校草稿\n{draft_text[:6000]}\n\n"
            + (f"## 角色设定（对照检查 OOC）\n{json.dumps(char_specs, ensure_ascii=False, indent=2)}\n\n" if char_specs else "")
            + (f"## 已有事件时间线（对照检查时间线矛盾）\n{timeline_text}\n\n" if timeline_text else "")
            + (f"## 角色状态约束\n{status_text}\n\n" if status_text else "")
            + (f"## 文风约束\n{style_text}\n\n" if style_text else "")
            + "## 本章规划（契约）\n"
            + f"emotion_arc: {cp.get('emotion_arc', '未标注')}\n"
            + f"rhythm_position: {cp.get('rhythm_position', '未标注')}\n"
            + f"closing_hook_type: {cp.get('closing_hook_type', '未标注')}\n"
            + f"forbidden_releases: {json.dumps(cp.get('forbidden_releases', []), ensure_ascii=False)}\n"
            + f"foreshadowing_resolved: {json.dumps(cp.get('foreshadowing_resolved', []), ensure_ascii=False)}\n"
            + f"foreshadowing_advanced: {json.dumps(cp.get('foreshadowing_advanced', []), ensure_ascii=False)}\n"
            + f"key_beats: {json.dumps([b for s in cp.get('sections', []) for b in s.get('key_beats', [])], ensure_ascii=False)}\n"
            + f"character_beats: {json.dumps(cp.get('character_beats', {}), ensure_ascii=False)}\n\n"
            + "## 任务\n"
            + "你是专业的审校编辑。请对照本章规划（契约），逐条验收草稿：\n"
            + "1. emotion_arc: 正文是否实际交付了目标情绪路径（warning）\n"
            + "2. rhythm_position: 叙事节奏是否与定位一致（warning）\n"
            + "3. forbidden_releases: 是否出现禁止释放的信息（critical，强制删除/改写）\n"
            + "4. foreshadowing: 承诺回收/推进的伏笔有无兑现（warning）\n"
            + "5. key_beats: 关键情节点是否都写到了（warning）\n"
            + "6. character_beats: 角色情绪变化是否在正文中可感知（warning）\n"
            + "7. 角色 OOC / 时间线 / 死活约束（已有，critical）\n\n"
            + "修正原则:\n"
            + "- 只修改有问题的 fragment，其他保持原样\n"
            + "- 返回完整的 fragment 列表（包含未修改的）\n"
            + "- 每个修改都要在 changes 中记录\n\n"
            + "返回 JSON:\n"
            + '{\n'
            + '  "revised_fragments": [{"type": "...", "text": "...", "character": "..."}],\n'
            + '  "changes": [{"fragment_index": 0, "original": "...", "revised": "...", "reason": "..."}],\n'
            + '  "verification": {\n'
            + '    "emotion_arc": {"passed": true, "evidence": "..."},\n'
            + '    "rhythm_position": {"passed": true},\n'
            + '    "forbidden_releases": {"passed": true},\n'
            + '    "foreshadowing_resolved": {"passed": true, "resolved": []},\n'
            + '    "foreshadowing_advanced": {"passed": true, "unadvanced": []},\n'
            + '    "key_beats": {"passed": true, "covered": "5/5"},\n'
            + '    "character_beats": {"passed": true}\n'
            + '  },\n'
            + '  "overall_score": 8.5\n'
            + '}'
        )

        try:
            result = self._state.sync_llm.chat_json(
                system_prompt=(
                    "你是专业的审校编辑。请对照本章规划（契约），逐条验收草稿："
                    "1. emotion_arc: 正文是否实际交付了目标情绪路径（warning）"
                    "2. rhythm_position: 叙事节奏是否与定位一致（warning）"
                    "3. forbidden_releases: 是否出现禁止释放的信息（critical，强制删除/改写）"
                    "4. foreshadowing: 承诺回收/推进的伏笔有无兑现（warning）"
                    "5. key_beats: 关键情节点是否都写到了（warning）"
                    "6. character_beats: 角色情绪变化是否在正文中可感知（warning）"
                    "7. 角色 OOC / 时间线 / 死活约束（已有，critical）"
                    "发现问题直接修正。只返回 JSON。"
                ),
                user_prompt=prompt,
                temperature=0.3,
                max_tokens=4096,
            )
            if isinstance(result, dict):
                return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            logger.warning("ReviewEditor LLM 调用失败: %s", e)

        # Fallback: 返回原稿
        return json.dumps({
            "revised_fragments": [f.to_dict() for f in draft],
            "changes": [],
            "overall_score": 0,
            "status": "review_failed",
        }, ensure_ascii=False)
