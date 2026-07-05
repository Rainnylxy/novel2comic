# -*- coding: utf-8 -*-
"""CharacterStatusResolver —— 用时查询角色当前状态。

不依赖 KG 缓存的静态 status 字段（LLM 抽取可能不准）。
当续写需要用到某角色时，回到小说原文定位该角色最后几次出场场景，
现场用 LLM 分析其当前状态（存活/死亡/失踪/被捕等）。

核心思想: 渐进式获取 (Progressive Resolution)
  - KG 提供索引（角色在哪几章出场）
  - Resolver 回到原文提取场景
  - LLM 基于场景分析状态
  - 返回状态 + 原文证据 + 置信度
"""

import json
import re
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from ..llm import UnifiedLLM
    from ..models import StoryGraph


# ============================================================
# Prompt
# ============================================================

STATUS_RESOLUTION_PROMPT = """你是一位专业的小说分析员。你需要根据角色在小说中**最后几次出场的场景片段**，判断该角色当前的状态。

## 角色名
{character_name}

## 该角色最后几次出场的场景
{scenes}

## 任务
基于以上原文片段，判断该角色的当前状态。返回严格 JSON:

{{
  "status": "active|dead|deceased|killed|missing|arrested|hospitalized|unknown",
  "status_detail": "一句话描述（如: 第112章中被枪击身亡）",
  "evidence": [
    {{
      "chapter": 章节号,
      "quote": "原文关键句（证明该状态的原文）",
      "reasoning": "为什么这句证明了该状态"
    }}
  ],
  "confidence": "high|medium|low",
  "last_seen_chapter": 最后出场的章节号,
  "last_seen_context": "最后出场时在做什么（一句话）"
}}

## 判断规则
1. 如果原文明确描写了角色的死亡（如"倒在血泊中""停止了呼吸""确认死亡"）→ status=dead/deceased/killed
2. 如果角色被逮捕/押送/关押 → status=arrested
3. 如果角色失踪/下落不明/他人提到"找不到他" → status=missing
4. 如果角色最后出场时还活着且没有异常 → status=active
5. 如果原文信息不足以确定 → status=unknown, confidence=low
6. confidence: 原文明确描述=high, 可推断=medium, 信息不足=low

只返回 JSON，不要返回其他内容。"""


# ============================================================
# CharacterStatusResolver
# ============================================================

class CharacterStatusResolver:
    """用时查询角色状态。

    不信任 KG 的静态 status 字段，在续写用到某角色时
    回到小说原文查找该角色的最后出场场景，现场分析状态。

    用法:
        resolver = CharacterStatusResolver(llm)
        result = resolver.resolve("金杰", novel_text, story_graph)
        # → {"status": "dead", "evidence": [...], "confidence": "high"}
    """

    def __init__(self, llm: "UnifiedLLM"):
        self._llm = llm

    def resolve(
        self,
        character_name: str,
        novel_text: str,
        graph: "StoryGraph",
        lookback_chapters: int = 3,
    ) -> dict:
        """查询角色的当前状态。

        Args:
            character_name: 角色名（如 "金杰"）
            novel_text: 小说全文
            graph: 知识图谱（用于定位章节）
            lookback_chapters: 回看最后几个有出场的章节（默认 3）

        Returns:
            {
                "status": "dead|active|missing|arrested|...",
                "status_detail": "...",
                "evidence": [{chapter, quote, reasoning}],
                "confidence": "high|medium|low",
                "last_seen_chapter": int,
                "last_seen_context": "..."
            }
        """
        # 1. 从 KG 获取角色出场的章节列表
        appearance_chapters = self._get_appearance_chapters(
            character_name, graph,
        )

        if not appearance_chapters:
            return self._unknown_result(
                f"KG 中没有 {character_name} 的出场记录",
            )

        # 2. 取最后 N 个出场章节
        target_chapters = sorted(appearance_chapters)[-lookback_chapters:]

        # 3. 从原文中提取这些章节中该角色出现的场景
        scenes = self._extract_scenes(
            character_name, novel_text, target_chapters,
        )

        if not scenes:
            return self._unknown_result(
                f"在原文中未能提取到 {character_name} 的场景片段",
                last_chapter=target_chapters[-1] if target_chapters else 0,
            )

        # 4. LLM 分析状态
        try:
            result = self._llm.chat_json(
                system_prompt="你是一位专业的小说分析员。只返回 JSON，不返回其他内容。",
                user_prompt=STATUS_RESOLUTION_PROMPT.format(
                    character_name=character_name,
                    scenes=scenes,
                ),
                temperature=0.2,
                max_tokens=2048,
            )
            if isinstance(result, dict):
                result.setdefault("status", "unknown")
                result.setdefault("confidence", "low")
                return result
        except Exception:
            pass

        return self._unknown_result(
            f"LLM 分析失败",
            last_chapter=target_chapters[-1] if target_chapters else 0,
        )

    # ================================================================
    # 内部方法
    # ================================================================

    def _get_appearance_chapters(
        self, name: str, graph: "StoryGraph",
    ) -> list[int]:
        """从 KG 获取角色出场的章节号列表。"""
        chapters = set()

        # 从 appears_in_edges 获取
        if hasattr(graph, 'appears_in_edges'):
            for edge in graph.appears_in_edges:
                if edge.person == name:
                    chapters.add(edge.chapter)

        # 从 character_events 获取
        if hasattr(graph, 'character_events'):
            try:
                events = graph.character_events(name)
                for ev in events:
                    ch = ev.get("chapter_start", 0)
                    if ch > 0:
                        chapters.add(ch)
            except Exception:
                pass

        # 从 person 节点的 first_appearance 推断
        person = graph.get_person_node(name) if hasattr(graph, 'get_person_node') else None
        if person and person.first_appearance_chapter:
            chapters.add(person.first_appearance_chapter)

        return sorted(chapters)

    def _extract_scenes(
        self,
        character_name: str,
        novel_text: str,
        target_chapters: list[int],
    ) -> str:
        """从原文中提取指定章节中该角色出现的场景。

        策略:
          1. 按章节切分原文
          2. 在目标章节中搜索角色名
          3. 提取角色名周围的文本段落（前后各 ~200 字）
          4. 返回格式化的场景片段
        """
        # 按 "第X章" 切分
        chapter_pattern = re.compile(
            r'(第[零一二三四五六七八九十百千0-9]+章[^\n]*)',
        )
        parts = chapter_pattern.split(novel_text)

        # 构建章节号 → 文本的映射
        chapter_map = {}
        current_chapter = 0
        current_text = []

        for part in parts:
            match = chapter_pattern.match(part)
            if match:
                # 保存上一章
                if current_chapter > 0 and current_text:
                    chapter_map[current_chapter] = "".join(current_text)
                # 解析新章节号
                current_chapter = self._parse_chapter_number(match.group(1))
                current_text = [part]
            else:
                current_text.append(part)

        # 保存最后一章
        if current_chapter > 0 and current_text:
            chapter_map[current_chapter] = "".join(current_text)

        # 从目标章节中提取场景
        scenes = []
        for ch in target_chapters:
            ch_text = chapter_map.get(ch, "")
            if not ch_text:
                continue

            # 搜索角色名出现的位置
            positions = []
            idx = 0
            while True:
                idx = ch_text.find(character_name, idx)
                if idx == -1:
                    break
                positions.append(idx)
                idx += len(character_name)

            if not positions:
                continue

            # 提取每个出现位置周围的文本
            for pos in positions[-3:]:  # 每章最多取 3 处
                start = max(0, pos - 200)
                end = min(len(ch_text), pos + 300)
                context = ch_text[start:end].strip()
                # 截断在完整句子的边界
                context = self._trim_to_sentence_boundary(context)
                scenes.append(
                    f"[第{ch}章] ...{context}..."
                )

        if not scenes:
            return ""

        # 格式化输出（限制总长度）
        result = "\n\n---\n\n".join(scenes)
        if len(result) > 4000:
            result = result[-4000:]  # 保留最后的场景（最有参考价值）
        return result

    @staticmethod
    def _parse_chapter_number(chapter_title: str) -> int:
        """从章节标题中解析序号。

        Examples:
            "第一章 楔子" → 1
            "第112章" → 112
        """
        # 尝试阿拉伯数字
        match = re.search(r'第\s*(\d+)\s*章', chapter_title)
        if match:
            return int(match.group(1))

        # 尝试中文数字（简化版，只处理 1-999）
        cn_nums = {
            "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
            "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
            "十": 10, "百": 100, "千": 1000,
        }
        match = re.search(r'第([零一二三四五六七八九十百千]+)章', chapter_title)
        if match:
            cn = match.group(1)
            result = 0
            unit = 1
            for char in reversed(cn):
                if char in ("十", "百", "千"):
                    unit = cn_nums[char]
                else:
                    result += cn_nums.get(char, 0) * unit
            return result if result > 0 else unit

        return 0

    @staticmethod
    def _trim_to_sentence_boundary(text: str) -> str:
        """截断到完整句子边界。"""
        # 向前找最近的句号/问号/感叹号作为起始
        first_period = text.find("。")
        first_quote = text.find(""")
        first_close_quote = text.find(""")
        first_newline = text.find("\n")

        starts = [p for p in [first_period, first_quote, first_close_quote, first_newline]
                  if p >= 0 and p < 50]
        if starts:
            return text[min(starts) + 1:]

        return text

    def _unknown_result(self, reason: str, last_chapter: int = 0) -> dict:
        """返回 unknown 状态的结果。"""
        return {
            "status": "unknown",
            "status_detail": reason,
            "evidence": [],
            "confidence": "low",
            "last_seen_chapter": last_chapter,
            "last_seen_context": "未能确定",
        }
