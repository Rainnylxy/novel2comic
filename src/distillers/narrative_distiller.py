# -*- coding: utf-8 -*-
"""NarrativeDistiller —— 从原文提取叙事特征（情绪/节奏/钩子/爽点）。"""

import json
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..pipeline.narrative_card import ChapterNarrativeCard, BatchNarrativeSummary


class NarrativeDistiller:
    """叙事蒸馏器。

    与已有的 CharacterDistiller、AuthorStyleDistiller 同级。
    只负责 LLM 调用和输出解析，不持有状态。
    """

    def __init__(self, sync_llm):
        self._llm = sync_llm

    # ================================================================
    # 尾部分析
    # ================================================================

    def analyze_tail(self, novel_text: str, last_n: int = 15) -> list:
        """分析原著最后 N 章的叙事特征。

        Args:
            novel_text: 小说全文
            last_n: 分析最后几章

        Returns:
            list[ChapterNarrativeCard]
        """
        chapters = self._split_novel_by_chapter(novel_text)
        if not chapters:
            return []

        sorted_chs = sorted(chapters.keys())
        target_chs = sorted_chs[-last_n:] if len(sorted_chs) >= last_n else sorted_chs

        # 拼接尾部章节文本供 LLM 一次性分析
        tail_text = ""
        for ch_num in target_chs:
            tail_text += f"\n=== 第{ch_num}章 ===\n{chapters[ch_num][:2000]}\n"

        cards = self._llm_analyze_chapters(tail_text, target_chs)
        return cards

    # ================================================================
    # 批级分析
    # ================================================================

    def analyze_batch(self, chapter_prose_list: list[tuple[int, str]]) -> tuple[list, "BatchNarrativeSummary"]:
        """分析续写产出的 10 章批次，返回 [ChapterNarrativeCard, BatchNarrativeSummary]。

        Args:
            chapter_prose_list: [(chapter_number, prose_text), ...]
                                 prose_text 已是拼接后的完整章节文本

        Returns:
            (cards: list[ChapterNarrativeCard], summary: BatchNarrativeSummary)
        """
        batch_text = ""
        for ch_num, prose in chapter_prose_list:
            batch_text += f"\n=== 第{ch_num}章 ===\n{prose[:2000]}\n"

        ch_nums = [ch for ch, _ in chapter_prose_list]
        cards = self._llm_analyze_chapters(batch_text, ch_nums)
        summary = self._llm_aggregate_batch(cards, ch_nums)
        return cards, summary

    # ================================================================
    # LLM 调用
    # ================================================================

    def _llm_analyze_chapters(self, text: str, chapter_numbers: list) -> list:
        """调用 LLM 分析章节叙事特征。一次性传入多章文本，返回多张叙事卡。"""
        from ..pipeline.narrative_card import ChapterNarrativeCard

        prompt = f"""你是专业小说结构分析员。请分析以下章节的叙事特征。

章节范围: 第{chapter_numbers[0]}-{chapter_numbers[-1]}章 ({len(chapter_numbers)} 章)

原文:
{text[:12000]}

请返回 JSON 数组，每章一个对象:
[
  {{
    "chapter_number": {chapter_numbers[0]},
    "emotion_arc": "前状态 → 触发事件 → 后状态（一句话，读者视角）",
    "rhythm_type": "高压/推进/关系/低压/信息整理",
    "closing_hook_type": "事件钩子/信息钩子/情绪钩子/悬念钩子/弱钩子/阶段目标",
    "highlight_type": "打脸/反转/身份揭露/装逼/感情拉扯/无",
    "key_info_released": "本章揭露的新信息（没有就写'无'）",
    "character_functions": {{"角色名": "功能（对手/盟友/催化剂/被拯救者）"}}
  }},
  ...
]

规则:
- emotion_arc 必须含"前状态 → 触发 → 后状态"三要素，不要只写情绪标签
- rhythm_type: 高压=爽点/反转/高潮密集；推进=主线叙事；关系=人物互动/情感；低压=过渡/信息交代
- closing_hook_type: 每章结尾给读者下读理由的方式
- character_functions 只列承担明确叙事功能的角色（2-4 个），路人不用写

只返回 JSON 数组，不要其他文字。"""

        try:
            result = self._llm.chat(
                system_prompt="你是专业小说结构分析员。只返回 JSON 数组，不返回其他内容。",
                user_prompt=prompt,
                temperature=0.3,
                max_tokens=4096,
            )
            cards = self._parse_cards(result, ChapterNarrativeCard)
            return cards
        except Exception as e:
            print(f"  [NarrativeDistiller] analyze_chapters 失败: {e}")
            return []

    def _llm_aggregate_batch(self, cards: list, chapter_numbers: list) -> "BatchNarrativeSummary":
        """将单章叙事卡聚合为批级摘要。"""
        from ..pipeline.narrative_card import BatchNarrativeSummary

        cards_text = "\n".join(
            f"第{c.chapter_number}章: 情绪={c.emotion_arc}, 节奏={c.rhythm_type}, "
            f"钩子={c.closing_hook_type}, 爽点={c.highlight_type}"
            for c in cards
        )

        prompt = f"""请将以下章节叙事特征聚合为 10 章批次的叙事摘要。

{cards_text}

返回 JSON:
{{
  "emotion_curve": "简要的情绪递进曲线（如'前3章压抑→第4章小爆发→第5-7章紧张→第8-10章爽感释放'）",
  "rhythm_pattern": "高压/推进/关系/低压 各占的百分比（如'高压 30% / 推进 40% / 关系 20% / 低压 10%'）",
  "hook_preference": {{"章尾": "钩子类型分布（如'悬念式 60%, 事件式 30%, 情绪钩子 10%'）"}},
  "highlight_density": "平均每N章一个爽点（数字）",
  "dominant_highlight_types": ["最主要的爽点类型（1-3个）"]
}}

只返回 JSON。"""

        try:
            result = self._llm.chat_json(
                system_prompt="你是专业小说结构分析员。只返回 JSON。",
                user_prompt=prompt,
                temperature=0.3,
                max_tokens=1024,
            )
            if isinstance(result, dict):
                return BatchNarrativeSummary(
                    chapters_range=(chapter_numbers[0], chapter_numbers[-1]),
                    emotion_curve=result.get("emotion_curve", ""),
                    rhythm_pattern=result.get("rhythm_pattern", ""),
                    hook_preference=result.get("hook_preference", {}),
                    highlight_density=float(result.get("highlight_density", 0)),
                    dominant_highlight_types=result.get("dominant_highlight_types", []),
                )
        except Exception as e:
            print(f"  [NarrativeDistiller] aggregate_batch 失败: {e}")

        return BatchNarrativeSummary(
            chapters_range=(chapter_numbers[0], chapter_numbers[-1]),
        )

    # ================================================================
    # 输出解析
    # ================================================================

    @staticmethod
    def _parse_cards(raw: str, card_class) -> list:
        """从 LLM 输出中解析叙事卡 JSON 数组。"""

        # 尝试直接解析
        text = raw.strip()
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return [card_class.from_dict(d) for d in data]
            if isinstance(data, dict):
                return [card_class.from_dict(data)]
        except json.JSONDecodeError:
            pass

        # 从 markdown 代码块提取
        m = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
                if isinstance(data, list):
                    return [card_class.from_dict(d) for d in data]
                if isinstance(data, dict):
                    return [card_class.from_dict(data)]
            except json.JSONDecodeError:
                pass

        # 找 JSON 数组
        m = re.search(r'\[.*\]', text, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group())
                if isinstance(data, list):
                    return [card_class.from_dict(d) for d in data]
            except json.JSONDecodeError:
                pass

        return []

    # ================================================================
    # 工具方法
    # ================================================================

    @staticmethod
    def _split_novel_by_chapter(text: str) -> dict:
        """按章节切分小说。Returns {chapter_number: chapter_text}."""
        pattern = re.compile(r'(第[零一二三四五六七八九十百千\d]+章[^\n]*)')
        parts = pattern.split(text)

        chapters = {}
        current_ch = 0
        current_text = []

        for part in parts:
            m = pattern.match(part)
            if m:
                if current_ch > 0 and current_text:
                    chapters[current_ch] = "".join(current_text)
                current_ch = NarrativeDistiller._parse_chapter_number(m.group(1))
                current_text = [part]
            else:
                current_text.append(part)

        if current_ch > 0 and current_text:
            chapters[current_ch] = "".join(current_text)

        return chapters

    @staticmethod
    def _parse_chapter_number(title: str) -> int:
        # 节号解析（与 pipeline.py 中 ContinuationPipeline._parse_chapter_number 同源）
        # 仅可靠处理阿拉伯数字章节号；中文数字（十二/二十）存在已知偏差
        """从 '第X章' 中解析章节号。"""
        m = re.search(r'第\s*(\d+)\s*章', title)
        if m:
            return int(m.group(1))
        cn = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
              "十": 10, "百": 100, "千": 1000}
        m = re.search(r'第([零一二三四五六七八九十百千]+)章', title)
        if m:
            s = m.group(1)
            result = 0
            unit = 1
            for ch in reversed(s):
                if ch in ("十", "百", "千"):
                    unit = cn[ch]
                else:
                    result += cn.get(ch, 0) * unit
            return result if result > 0 else unit
        return 0
