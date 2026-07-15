# -*- coding: utf-8 -*-
"""SharedToolKit —— 跨 Agent 公用的工具工厂。

消除 PlotArchitect / ChapterWriter / ReviewEditor 之间的工具重复。

用法:
    from agentflow.runtime.toolkit import tool

    shared = SharedToolKit(ctx, services, character_profiles, character_statuses)

    @tool
    def lookup_character(name: str) -> str:
        return shared.lookup_character(name)
"""

from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    pass


class SharedToolKit:
    """跨 Agent 公用的 KG 查询工具集。

    所有方法都是纯函数（无副作用），通过注入的 graph/kg/profiles 工作。
    Agent 在自己的 _build_tools() 中用 @tool 包装这些方法。

    用法:
        shared = SharedToolKit(graph=graph, kg=kg, character_profiles=profiles)

        def _build_tools(self):
            return [
                shared.make_lookup_character(),
                ...agent-specific tools...
            ]
    """

    def __init__(
        self,
        graph=None,
        kg=None,
        character_profiles: dict = None,
        character_statuses: dict = None,
        story_memory: object = None,
        plot_threads: list = None,
        enable_cache: bool = False,
    ):
        self._graph = graph
        self._kg = kg
        self._character_profiles = character_profiles or {}
        self._character_statuses = character_statuses or {}
        self._story_memory = story_memory
        self._plot_threads = plot_threads or []
        self._cache: dict = {} if enable_cache else None

    def clear_cache(self):
        """清空查找缓存（跨章节时调用）。"""
        if self._cache is not None:
            self._cache.clear()

    # ================================================================
    # lookup_character — PlotArchitect & Writer 公用
    # ================================================================

    def lookup_character(self, name: str) -> str:
        """查询角色的完整档案：状态、Voice、行为边界、关系、最后事件。

        当需要写/规划某个角色但不清楚其设定时，按需调用。
        不要预先查询所有角色——只查当前涉及的。

        如果 enable_cache=True，同章内重复查询直接返回缓存。

        Returns:
            格式化的角色档案文本
        """
        # 缓存检查
        if self._cache is not None and name in self._cache:
            return self._cache[name]

        if not self._graph:
            return f"KG 不可用：StoryGraph 为空"

        graph = self._graph
        if not graph:
            return f"KG 不可用：StoryGraph 为空"

        if not self._kg:
            return f"KG 服务不可用，无法查询 {name}"

        person = self._kg.get_person(graph, name)
        if not person:
            return f"KG 中未找到角色「{name}」"

        char_statuses = self._character_statuses
        char_profiles = self._character_profiles

        lines = [
            f"角色: {name}",
            f"状态: {char_statuses.get(name, person.status)}",
            f"叙事角色: {person.role_type}",
            f"重要性: {person.importance}",
            f"所属: {person.faction}" if person.faction else "",
        ]
        # description 含有关键的职业/身份信息，独立并优先展示
        if person.description:
            lines.append(f"身份: {person.description}")
        else:
            lines.append("身份: 无")

        # 最后参与的事件
        if hasattr(graph, 'character_events'):
            events = graph.character_events(name)
            if events:
                last = events[-1]
                lines.append(
                    f"\n最后事件（第{last.get('chapter_start','?')}章）: "
                    f"{last.get('name','?')} — {last.get('summary','')[:120]}"
                )

        # 关系
        if hasattr(graph, 'relationship_edges'):
            rels = []
            for edge in graph.relationship_edges:
                if edge.from_char == name:
                    rels.append(
                        f"对{edge.to_char}: {edge.relation_type}"
                        + (f"(亲密度:{edge.intimacy:+d})" if edge.intimacy else "")
                    )
                elif edge.to_char == name:
                    rels.append(
                        f"被{edge.from_char}: {edge.relation_type}"
                        + (f"(亲密度:{edge.intimacy:+d})" if edge.intimacy else "")
                    )
            if rels:
                lines.append(f"关系: {'; '.join(rels[:5])}")

        # 状态硬约束
        status = char_statuses.get(name, person.status)
        if status in ("dead", "deceased", "killed"):
            lines.append("\n⚠️ 此角色已死亡！只能以回忆/闪回/他人提及出现。")
        elif status == "missing":
            lines.append("\n⚠️ 此角色下落不明！不能直接出场。")

        # 角色蒸馏档案
        profile = char_profiles.get(name)
        if profile:
            self._format_voice(lines, profile)
            self._format_boundary(lines, profile)
            self._format_sensitivity(lines, profile)
            self._format_anchors(lines, profile)
            self._format_state(lines, profile)

        # Dossier 数据（verify_character 写入 CharacterNode）
        if person.ending:
            lines.append(f"\n结局: {person.ending}")
        if person.foreshadowing:
            lines.append(f"伏笔: {person.foreshadowing}")
        if person.evidence:
            lines.append(f"原文依据: {person.evidence}")

        result = "\n".join(lines)
        # 写入缓存
        if self._cache is not None:
            self._cache[name] = result
        return result

    # ── Profile 格式化辅助 ──

    @staticmethod
    def _format_voice(lines: list, profile):
        """格式化 Voice 信息。"""
        if not (hasattr(profile, 'voice') and profile.voice):
            return
        v = profile.voice
        if v.summary:
            lines.append(f"\nVoice: {v.summary}")
        tone_parts = []
        if v.tone_cold_warm:
            tone_parts.append(f"冷暖={v.tone_cold_warm:.1f}")
        if v.tone_hard_soft:
            tone_parts.append(f"硬软={v.tone_hard_soft:.1f}")
        if v.tone_distant_close:
            tone_parts.append(f"疏近={v.tone_distant_close:.1f}")
        if tone_parts:
            lines.append(f"语气光谱: {', '.join(tone_parts)} (0=冷/硬/疏, 1=暖/软/近)")
        if v.rhythm:
            lines.append(f"说话节奏: {v.rhythm}")
        if v.response_pattern:
            lines.append(f"回应模式: {v.response_pattern}")
        if v.taboo_words:
            lines.append(f"禁用词: {', '.join(v.taboo_words)}")
        if v.taboo_patterns:
            lines.append(f"禁用句式: {', '.join(v.taboo_patterns)}")
        if v.voice_shift:
            shifts = [f"对{k}: {vv}" for k, vv in v.voice_shift.items()]
            if shifts:
                lines.append(f"表达差异: {'; '.join(shifts[:5])}")

    @staticmethod
    def _format_boundary(lines: list, profile):
        """格式化 Boundary 信息。"""
        if not (hasattr(profile, 'boundary') and profile.boundary):
            return
        b = profile.boundary
        if b.hard_rules:
            lines.append(f"硬底线: {', '.join(b.hard_rules[:5])}")
        if b.tendencies:
            lines.append(f"行为倾向: {', '.join(b.tendencies[:5])}")
        if b.relationship_behaviors:
            rb = [f"对{k}: {v}" for k, v in b.relationship_behaviors.items()]
            if rb:
                lines.append(f"关系行为: {'; '.join(rb[:5])}")

    @staticmethod
    def _format_sensitivity(lines: list, profile):
        """格式化 Sensitivity 信息。"""
        if not (hasattr(profile, 'sensitivity') and profile.sensitivity):
            return
        entries = profile.sensitivity.entries
        if entries:
            lines.append(f"敏感触发 ({len(entries)}项):")
            for e in entries[:3]:
                triggers = ', '.join(e.triggers[:3]) if hasattr(e, 'triggers') else ''
                effects = str(e.effects)[:80] if hasattr(e, 'effects') else ''
                if triggers:
                    lines.append(f"  · {triggers} → {effects}")

    @staticmethod
    def _format_anchors(lines: list, profile):
        """格式化 Policy Anchors 信息。"""
        if not (hasattr(profile, 'policy_anchors') and profile.policy_anchors):
            return
        anchors = profile.policy_anchors
        if anchors:
            lines.append(f"行为锚点 ({len(anchors)}个):")
            for a in anchors[:3]:
                s = getattr(a, 'situation', '') or ''
                act = getattr(a, 'action', '') or ''
                if s and act:
                    lines.append(f"  · {s} → {act}")

    @staticmethod
    def _format_state(lines: list, profile):
        """格式化 State 信息。"""
        if not (hasattr(profile, 'state') and profile.state):
            return
        baseline = profile.state.baseline
        if baseline:
            base_str = ', '.join(f"{k}={v}" for k, v in list(baseline.items())[:5])
            lines.append(f"心理基线: {base_str}")

    # ================================================================
    # gather_active_conflicts — PlotArchitect 专用
    # ================================================================

    def gather_active_conflicts(self) -> str:
        """从 KG 查询当前活跃的角色冲突。

        遍历 enemy_pairs，返回冲突双方、紧张度和共享历史。

        Returns:
            格式化的冲突列表文本
        """
        graph = self._graph
        if not graph:
            return "KG 不可用：StoryGraph 为空"
        if not self._kg:
            return "KG 服务不可用"

        conflicts = []
        for pair in self._kg.enemy_pairs(graph):
            rel = graph.get_relationship_edge(pair[0], pair[1])
            if rel:
                conflicts.append({
                    "characters": list(pair),
                    "tension": rel.current_tension or "?",
                    "shared_history": rel.shared_history or "",
                })

        if not conflicts:
            return "KG 中暂无活跃冲突记录"

        lines = [f"共 {len(conflicts)} 对活跃冲突:"]
        for c in conflicts[:8]:
            chars = ' vs '.join(c['characters'])
            lines.append(f"- {chars}: 紧张度={c['tension']} | {c['shared_history'][:120]}")
        return "\n".join(lines)

    # ================================================================
    # recall_foreshadowing — Writer 专用
    # ================================================================

    def recall_foreshadowing(self) -> str:
        """查询当前续写故事中已引入但尚未回收的伏笔和悬念线。

        来自 StoryMemory 的 pending_threads 或直接传入的 plot_threads。

        Returns:
            活跃伏笔列表
        """
        # 优先从 StoryMemory 读取
        if self._story_memory:
            threads = self._story_memory.get_pending_threads()
            if threads:
                lines = [f"共 {len(threads)} 条活跃伏笔:"]
                for t in threads:
                    ch = t.get("introduced_ch", "?")
                    desc = t.get("description", "")
                    thread_name = t.get("thread", str(t))
                    lines.append(f"- [{ch}] {thread_name}" + (f": {desc}" if desc else ""))
                return "\n".join(lines)
            return "暂无活跃伏笔。可以引入新的悬念线。"

        # Fallback: 直接传入的列表
        threads = self._plot_threads
        if not threads:
            return "暂无活跃伏笔。可以引入新的悬念线。"

        lines = [f"共 {len(threads)} 条活跃伏笔:"]
        for t in threads:
            if isinstance(t, dict):
                lines.append(f"- [{t.get('introduced_ch', '?')}] {t.get('thread', str(t))}")
            else:
                lines.append(f"- {t}")
        return "\n".join(lines)

    # ================================================================
    # 工具工厂方法（供 Agent._build_tools() 使用）
    # ================================================================

    def make_lookup_character(self):
        """创建 @tool 包装的 lookup_character。"""
        from agentflow.runtime.toolkit import tool
        shared = self

        def lookup_character(name: str) -> str:
            return shared.lookup_character(name)
        lookup_character.__doc__ = self.lookup_character.__doc__

        return tool(lookup_character)

    def make_gather_active_conflicts(self):
        """创建 @tool 包装的 gather_active_conflicts。"""
        from agentflow.runtime.toolkit import tool
        shared = self

        def gather_active_conflicts() -> str:
            return shared.gather_active_conflicts()
        gather_active_conflicts.__doc__ = self.gather_active_conflicts.__doc__

        return tool(gather_active_conflicts)

    def make_recall_foreshadowing(self):
        """创建 @tool 包装的 recall_foreshadowing。"""
        from agentflow.runtime.toolkit import tool
        shared = self

        def recall_foreshadowing() -> str:
            return shared.recall_foreshadowing()
        recall_foreshadowing.__doc__ = self.recall_foreshadowing.__doc__

        return tool(recall_foreshadowing)
