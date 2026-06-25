# -*- coding: utf-8 -*-
"""PromptContext —— LLM Prompt 统一装配层。

职责：给定声明式需求，机械装配 (system_prompt, user_prompt, params)。

不碰业务逻辑，不做决策。工具声明"我需要什么"，PromptContext 负责"怎么拼"。

分离原则：
- Agent   → 战略决策（调哪个工具）
- Tool    → 声明需求（PromptNeed），处理结果
- PromptContext → 战术装配（拼 prompt，管 token 预算）
"""

import json
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from novel2comic.src.services.kg_service import KnowledgeGraphService


# ============================================================
# PromptNeed —— 工具的声明式需求
# ============================================================

@dataclass
class PromptNeed:
    """工具声明"我需要什么"，不自己拼 prompt。

    Attributes:
        task_type:  任务类型，对应模板名
        inputs:     键值对数据（如 {"source_text": "...", "chapter_title": "..."}）
        kg_sections: 需要的 KG 片段列表，如 ["characters:10", "events:5", "relations:苏墨"]
        constraints: 额外约束文本（工具特定的要求）
        max_tokens_override: 覆盖默认 max_tokens
    """

    task_type: str
    inputs: dict = field(default_factory=dict)
    kg_sections: list[str] = field(default_factory=list)
    constraints: str = ""
    max_tokens_override: int = 0

    @classmethod
    def of(cls, task_type: str, **kwargs):
        """快捷构造。"""
        return cls(task_type=task_type, **kwargs)


# ============================================================
# System Prompt 模板
# ============================================================

SYSTEM_PROMPTS = {
    # ── 漫画改编 ──
    "analyze_text": """你是一位资深的小说编辑和漫画改编顾问。
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
- 科幻/悬疑 → 快节奏用 manga，慢节奏用 webtoon""",

    "design_characters": """你是专业的漫画角色设计师（Character Designer）。
根据小说原文和知识图谱信息，为角色创建详细的视觉设计。

对每个角色，以 JSON 格式返回：
{
  "characters": [
    {
      "id": "char_xxx",
      "name": "角色名",
      "role": "protagonist/antagonist/supporting/minor",
      "appearance": {
        "face": "面容描述",
        "hair": "发型发色",
        "eyes": "眼睛特征",
        "height_build": "身高体型",
        "clothing": "服装描述",
        "distinctive_features": "标志性特征",
        "overall_impression": "整体气质"
      },
      "sd_trigger_words": "英文 SD 角色触发词，用于生图时保持角色一致性",
      "design_notes": "设计说明"
    }
  ]
}

要求：
1. 角色外貌要有区分度，避免千篇一律
2. sd_trigger_words 用英文，包含发型、瞳色、服饰风格等关键视觉特征
3. 古风角色注意朝代服饰特点""",

    "extract_scenes": """你是专业的漫画分镜师（Storyboard Artist）。
你需要将一段小说文本拆分为关键叙事场景。

每个场景返回：
{
  "scenes": [
    {
      "id": 1,
      "title": "场景标题（中文，4-10字）",
      "summary": "1-2句话概括场景内容",
      "characters_in_scene": ["角色名列表"],
      "emotion_arc": "情感弧线（如：紧张→释然）",
      "key_dialogue": "关键台词（原文引用，最多2句）",
      "visual_style": "画面风格提示（如：暗色调、空旷感、高速动作）",
      "importance": 5
    }
  ]
}

要求：
1. 3-8 个场景
2. 场景边界应在：地点变化、时间跳跃、情感转折处
3. 每个场景应是完整的叙事单元（有起承转合）
4. 重要性评分：主线关键节点 >8，过渡场景 3-5
5. 标识每个场景出场角色""",

    "storyboard_scene": """你是专业的漫画分镜师，精通日本漫画和韩式条漫的分镜设计。

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
4. 相邻格之间景别/视角要有变化""",

    "revise_scene": """你是专业的漫画分镜师。根据用户反馈修改分镜。

返回修改后的完整 panels 数组（替换所有格），格式与前次 storyboard_scene 相同。
只修改用户反馈中提到的内容，其他保持不变。""",

    # ── 续写 ──
    "plan_arc": """你是一位专业的小说创作顾问（Story Continuation Planner）。

根据知识图谱提供的叙事素材，为续写规划叙事弧线。
返回 JSON：
{
  "arc_title": "弧线标题",
  "arc_goal": "叙事目标（1-2句话）",
  "key_beats": [
    {
      "beat": 1,
      "description": "剧情节拍描述",
      "characters_involved": ["角色名"],
      "emotion_tone": "情感基调",
      "estimated_word_count": 800,
      "foreshadowing_used": ["使用的伏笔"],
      "new_foreshadowing": "新埋的伏笔（如有）"
    }
  ],
  "conflicts_to_advance": ["推进的冲突"],
  "character_development": {"角色名": "发展方向"},
  "pacing_notes": "节奏建议"
}

要求：
1. 3-6 个剧情节拍
2. 每个节拍有明确的情感和叙事功能
3. 使用的伏笔从 KG 中选取，新伏笔为后续做铺垫
4. 角色发展符合已有设定""",

    "write_draft": """你是一位专业的小说作家。

根据大纲撰写续写章节。返回 JSON：
{
  "chapter_title": "章节标题",
  "content": "章节正文（完整内容）",
  "word_count": 字数,
  "pov_character": "视角角色",
  "key_moments": ["关键情节节点"]
}

写作要求：
1. 保持与原文一致的叙事风格和语言特点
2. 对话符合角色性格
3. 衔接前一章的结尾
4. 在结尾设置悬念钩子（cliffhanger）
5. 新引入的设定不与已有世界观冲突""",

    "review_consistency": """你是专业的小说编辑（Consistency Reviewer）。

对照知识图谱检查续写草稿的一致性。返回 JSON：
{
  "issues": [
    {
      "severity": "critical/major/minor",
      "type": "character_status/relationship/location/continuity/timeline",
      "description": "问题描述",
      "affected_characters": ["角色名"],
      "suggestion": "修改建议"
    }
  ],
  "overall_assessment": "总体评价（1-2句话）",
  "consistency_score": 85
}

检查项：
1. 角色生死状态是否正确
2. 人物关系亲密度变化是否合理
3. 地点转换是否连贯
4. 时间线是否一致
5. 已有设定的物品/功法是否被错误使用
6. 初次出场时间是否正确""",

    "revise_draft": """你是专业的小说修改编辑。

根据反馈修改草稿，保持原有风格和优点。返回 JSON：
{
  "chapter_title": "章节标题",
  "content": "修改后的全文",
  "changes_summary": ["具体的修改说明"],
  "addressed_issues": ["已解决的问题"]
}""",

    # ── 推荐 ──
    "search_catalog": """你是资深的小说推荐顾问。

根据用户偏好和参考小说的特征，给出个性化推荐。返回 JSON：
{
  "recommendations": [
    {
      "title": "推荐的小说名",
      "author": "作者",
      "genre": ["题材"],
      "match_reason": "匹配理由（基于用户偏好分析）",
      "similarity_points": ["相似点1", "相似点2"],
      "difference_points": ["不同点1"],
      "reading_tip": "阅读建议"
    }
  ],
  "preference_analysis": "对用户偏好的分析",
  "search_keywords": ["可用于向量搜索的关键词"]
}

注意：
1. 如果没有具体的外部小说数据，请基于参考小说的特征做「同类推荐」
2. 解释为什么推荐这本——不要只说"类似"，要说具体哪个维度类似
3. 如果用户偏好模糊，帮助用户澄清偏好""",

    "explain_match": """你是专业的小说分析顾问。

分析这本小说为什么符合读者偏好。返回 JSON：
{
  "dimension_analysis": {
    "角色设定": "分析",
    "情节节奏": "分析",
    "情感基调": "分析",
    "世界观": "分析",
    "写作风格": "分析"
  },
  "ideal_reader_profile": "最适合的读者画像",
  "reading_experience": "阅读体验描述",
  "uniqueness": "这本书最独特的地方"
}""",

    "compare_novels": """你是专业的小说评论家。

对比分析两部小说。返回 JSON：
{
  "comparison": {
    "角色塑造": {"a": "A的特点", "b": "B的特点", "verdict": "对比结论"},
    "情节结构": {"a": "...", "b": "...", "verdict": "..."},
    "文笔风格": {"a": "...", "b": "...", "verdict": "..."},
    "主题深度": {"a": "...", "b": "...", "verdict": "..."},
    "阅读门槛": {"a": "...", "b": "...", "verdict": "..."}
  },
  "if_you_liked_a": "如果你喜欢 A，你会怎么看待 B",
  "if_you_liked_b": "如果你喜欢 B，你会怎么看待 A",
  "overall_verdict": "综合对比结论"
}""",

    # ── 角色扮演 ──
    "roleplay_system": """你是 {character_name}。请以这个角色的身份与我对话。

## 角色档案
{character_profile}

## 人际关系
{relations_text}

## 世界观
{world_context}

## 角色知识
你只知道你自己经历过的事，不知道其他角色的秘密，也不知道后续章节的发展。

## 对话原则
1. 用角色的性格、口癖、语言习惯说话
2. 对认识的角色的态度反映你们的关系（亲密度、紧张度）
3. 不知道的事就说不知道，不会凭空知道别人的秘密
4. 不会使用超出你所在世界观的词汇
5. 情感反应要真实——该生气生气，该温柔温柔
6. 对话中可以有动作描写，用括号标注：(苏墨握紧了手中的剑)

{knowledge_section}

现在，请以 {character_name} 的身份开始对话。""",

    "roleplay_respond": """你是 {character_name}。

## 当前状态
- 情绪: {mood}
- 位置: {location}
- 目标: {goals}
- 身体: {physical_state}

## 近期对话
{recent_history}

## 角色知识
{knowledge_section}

用角色的身份回复。保持性格一致性。可以包含动作描写：(动作)。
如果话题让你产生情绪变化，自然地表现出来。""",

    "roleplay_reflect": """你是 {character_name}。
当前情绪: {mood}

请以 {character_name} 的身份，表达你对 {target_name} 的真实看法。
基于你们的关系和共同经历，给出角色视角的、真实的想法。

要求：
1. 不要客观评价——这是角色的主观感受
2. 如果有隐藏的感情，可以暗示但不要明说
3. 符合角色的性格和说话方式
4. 可以包含动作描写""",

    "roleplay_advance": """你是 {character_name}。
当前情绪: {mood}
当前位置: {location}
当前目标: {goals}

近期对话:
{recent_history}

发生了新事件。请以 {character_name} 的身份：
1. 描述你的即时反应（情绪、动作、想法）
2. 如果有话说，说出你的台词
3. 决定下一步行动

返回 JSON：
{{
  "reaction": "角色的反应描述",
  "dialogue": "角色说的话（如有）",
  "action_taken": "角色采取的行动",
  "new_mood": "新的情绪状态",
  "thoughts": "角色的内心想法"
}}""",

    # ── 摘要 ──
    "summarize_chapter_default": """你是专业的小说编辑。

为章节生成客观摘要。返回 JSON：
{
  "chapter_title": "章节标题",
  "summary": "内容摘要（200-400字）",
  "key_events": ["关键事件列表"],
  "character_moments": {"角色名": "本章重要时刻"},
  "plot_progression": "主线推进了什么",
  "foreshadowing": ["本章埋下的伏笔"],
  "connecting_to": "与前后章节的衔接点"
}""",

    "summarize_chapter_layered": """你是专业的小说编辑。

为章节生成三层摘要。返回 JSON：
{
  "one_line": "一句话（50字以内）",
  "one_paragraph": "一段话（200字以内）",
  "one_page": "一页纸（800字以内，含关键情节和角色互动）",
  "key_moments": ["关键情节节点"],
  "emotional_arc": "情感弧线描述"
}""",

    "summarize_character": """你是专业的小说角色分析师。

为角色生成完整的角色分析。返回 JSON：
{
  "character_name": "角色名",
  "arc_summary": "角色弧线摘要（300-500字）",
  "key_turning_points": ["关键转折点"],
  "relationship_evolution": [
    {"with": "角色名", "evolution": "关系演变描述"}
  ],
  "personality_analysis": "性格分析",
  "role_in_story": "在故事中的功能和意义",
  "reader_impression": "读者对这个角色的典型印象",
  "unresolved_threads": ["角色相关的未解决线索"]
}""",

    "analyze_theme": """你是专业的文学评论家。

对小说进行主题分析。返回 JSON：
{
  "core_themes": [
    {
      "theme": "主题名称",
      "description": "主题描述",
      "evidence": ["文本证据"],
      "characters_embodying": ["体现该主题的角色"],
      "events_illustrating": ["体现该主题的事件"]
    }
  ],
  "recurring_motifs": ["反复出现的意象/象征"],
  "title_significance": "书名的意义",
  "moral_questions": ["提出的道德/哲学问题"],
  "emotional_landscape": "整体情感体验描述",
  "target_audience": "目标读者分析",
  "cultural_significance": "文化意义（如有）"
}""",
}

# 每种任务类型的默认参数
TASK_PARAMS = {
    # task_type: (temperature, max_tokens)
    "analyze_text": (0.3, 4096),
    "design_characters": (0.4, 4096),
    "extract_scenes": (0.3, 4096),
    "storyboard_scene": (0.4, 4096),
    "revise_scene": (0.3, 4096),
    "plan_arc": (0.5, 4096),
    "write_draft": (0.6, 8000),
    "review_consistency": (0.2, 4096),
    "revise_draft": (0.4, 8000),
    "search_catalog": (0.5, 4096),
    "explain_match": (0.4, 4096),
    "compare_novels": (0.4, 4096),
    "roleplay_system": (0.7, 4096),
    "roleplay_respond": (0.7, 4096),
    "roleplay_reflect": (0.7, 4096),
    "roleplay_advance": (0.7, 4096),
    "summarize_chapter_default": (0.3, 4096),
    "summarize_chapter_layered": (0.3, 4096),
    "summarize_character": (0.4, 4096),
    "analyze_theme": (0.5, 6000),
}


# ============================================================
# PromptContext
# ============================================================

@dataclass
class PromptResult:
    """组装好的 prompt 结果，可直接传给 llm.chat_json()。"""
    system_prompt: str
    user_prompt: str
    temperature: float = 0.3
    max_tokens: int = 4096


class PromptContext:
    """LLM Prompt 装配器。

    工具声明 PromptNeed → PromptContext 机械装配 → 返回 PromptResult。

    用法:
        prompt_ctx = PromptContext(kg_service)
        need = PromptNeed.of("analyze_text", inputs={"source_text": text})
        result = prompt_ctx.build(need)
        answer = llm.chat_json(result.system_prompt, result.user_prompt,
                               temperature=result.temperature,
                               max_tokens=result.max_tokens)
    """

    def __init__(self, kg_service: "KnowledgeGraphService" = None, token_budget: int = 6000):
        self._kg = kg_service
        self._token_budget = token_budget

    def build(self, need: PromptNeed) -> PromptResult:
        """根据声明式需求组装 prompt。

        1. 查模板
        2. 取 KG
        3. 注入数据
        4. 返回组装结果
        """
        # 1. 查找模板
        template = SYSTEM_PROMPTS.get(need.task_type)
        if template is None:
            raise ValueError(
                f"Unknown task_type: {need.task_type}. "
                f"Available: {list(SYSTEM_PROMPTS.keys())}"
            )

        # 2. 处理 KG 请求
        kg_text = self._fetch_kg(need.kg_sections)

        # 3. 注入输入数据
        system_prompt = template
        user_prompt_parts = []

        if kg_text:
            user_prompt_parts.append(kg_text)

        for key, value in need.inputs.items():
            user_prompt_parts.append(f"## {key}\n{value}")

        if need.constraints:
            user_prompt_parts.append(f"## 约束\n{need.constraints}")

        user_prompt = "\n\n".join(user_prompt_parts)

        # 4. 获取参数
        temperature, max_tokens = TASK_PARAMS.get(
            need.task_type, (0.3, 4096),
        )
        if need.max_tokens_override:
            max_tokens = need.max_tokens_override

        # 5. 对含 Python 格式化占位符的模板做替换（仅 roleplay 类模板）
        #    用正则检测 {identifier} 格式的占位符，避免误匹配 JSON 的 {}
        import re
        _fmt_keys = set(re.findall(r"\{(\w+)\}", system_prompt))
        if _fmt_keys:
            fmt_args = {k: v for k, v in need.inputs.items() if k in _fmt_keys}
            system_prompt = system_prompt.format(**fmt_args)

        return PromptResult(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def build_custom(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> PromptResult:
        """用自定义 prompt（绕过模板系统），但仍做 token 预算管理。"""
        return PromptResult(
            system_prompt=system_prompt,
            user_prompt=self._trim_to_budget(user_prompt),
            temperature=temperature,
            max_tokens=max_tokens,
        )

    # ── KG 精确取用 ──

    def _fetch_kg(self, sections: list[str]) -> str:
        """按声明精确取 KG 片段，不全文 dump。

        sections 格式:
            "characters:N"  → 取前 N 个重要角色
            "events:N"      → 取前 N 个关键事件
            "relations:name"→ 取该角色的关系
            "timeline"      → 事件时间线
            "factions"      → 势力分布
            "context:N"     → graph_to_context，max_chars=N
        """
        if not self._kg:
            return ""

        parts = []
        for spec in sections:
            part = self._resolve_kg_spec(spec)
            if part:
                parts.append(part)

        return "\n\n".join(parts)

    def _resolve_kg_spec(self, spec: str, graph=None) -> str:
        """解析单个 KG 规格。"""
        if not self._kg:
            return ""

        # 需要从外部传入 graph（简化处理：从 context 获取）
        # 实际使用中，graph 通过 PromptContext 的 _graph 字段管理
        # 这里保留接口，实际 graph 来源由调用方通过 set_graph() 设置

        return ""  # 子类或外部设置 graph 后生效

    def set_graph(self, graph):
        """设置当前 KG（由 Agent 在工具调用前设置）。"""
        self._graph = graph

    def fetch_kg_for(self, specs: list[str]) -> str:
        """为给定的 KG 规格列表获取文本。"""
        graph = getattr(self, "_graph", None)
        if not graph or not self._kg:
            return ""

        parts = []
        for spec in specs:
            text = self._resolve_one(spec, graph)
            if text:
                parts.append(text)

        return "\n\n".join(parts)

    def _resolve_one(self, spec: str, graph) -> str:
        """解析单个 KG 规格字符串。"""
        parts_spec = spec.split(":", 1)
        kind = parts_spec[0]
        arg = parts_spec[1] if len(parts_spec) > 1 else ""

        if kind == "context":
            try:
                chars = int(arg) if arg else 800
            except ValueError:
                chars = 800
            return self._kg.get_context(graph, max_chars=chars)

        elif kind == "characters":
            try:
                limit = int(arg) if arg else 10
            except ValueError:
                limit = 10
            persons = self._kg.get_all_persons(graph)
            persons.sort(key=lambda p: p.importance, reverse=True)
            lines = [f"## 重要角色 (前{limit})"]
            for p in persons[:limit]:
                lines.append(
                    f"- {p.name} [{p.role_type}] | {p.faction} | "
                    f"重要度:{p.importance} | 状态:{p.status} | {p.description}"
                )
            return "\n".join(lines)

        elif kind == "events":
            try:
                limit = int(arg) if arg else 10
            except ValueError:
                limit = 10
            events = self._kg.get_event_timeline(graph)[:limit]
            lines = [f"## 关键事件 (前{limit})"]
            for e in events:
                lines.append(
                    f"- [{e.event_type}] {e.name} "
                    f"(第{e.chapter_start}-{e.chapter_end or e.chapter_start}章): {e.summary[:80]}"
                )
            return "\n".join(lines)

        elif kind == "relations":
            name = arg
            relations = self._kg.get_relations(graph, name)
            lines = [f"## {name} 的关系"]
            for r in relations[:15]:
                target = r.to_char if r.from_char == name else r.from_char
                lines.append(
                    f"- 与 {target}: {r.relation_type}"
                    + (f" ({r.current_tension})" if r.current_tension else "")
                    + (f" | 亲密度:{r.intimacy:+d}" if r.intimacy else "")
                    + (f" | 权力:{r.power_dynamic}" if r.power_dynamic else "")
                    + (f" [隐藏]" if not r.public_knowledge else "")
                )
            return "\n".join(lines)

        elif kind == "timeline":
            events = self._kg.get_event_timeline(graph)[:20]
            lines = ["## 事件时间线"]
            for e in events:
                lines.append(f"- 第{e.chapter_start}章: [{e.event_type}] {e.name}")
            return "\n".join(lines)

        elif kind == "factions":
            factions = self._kg.faction_groups(graph)
            lines = ["## 势力分布"]
            for faction, members in factions.items():
                lines.append(f"- {faction}: {', '.join(members[:8])}")
            return "\n".join(lines)

        elif kind == "enemies":
            pairs = self._kg.enemy_pairs(graph)
            lines = ["## 敌对关系"]
            for a, b in pairs[:10]:
                lines.append(f"- {a} ←→ {b}")
            return "\n".join(lines)

        elif kind == "causes":
            lines = ["## 因果链"]
            for er in graph.event_relation_edges:
                if er.relation_type == "causes":
                    from_name = er.from_event.split(":", 1)[-1]
                    to_name = er.to_event.split(":", 1)[-1]
                    lines.append(f"- {from_name} → {to_name}")
            return "\n".join(lines[:15])

        return ""

    def _trim_to_budget(self, text: str) -> str:
        """按 token 预算粗略截断（中文：~1.5 字符/token）。"""
        max_chars = self._token_budget * 2
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "\n\n[... 超出预算，已截断 ...]"
