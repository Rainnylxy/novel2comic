# -*- coding: utf-8 -*-
"""Fragmentizer —— 自然段落 → StoryFragment[] 后处理层。

规则驱动，不依赖 LLM。让 Writer 专注于文学创作，
结构化输出由本层负责。

分类规则:
  - 引号对话 → dialogue（提取 speaker）
  - 短动作句 → action
  - 心想/暗想 → inner_thought
  - 分隔标记 → divider
  - 其余 → narration
"""

import re
from typing import Optional

from .fragment import StoryFragment


class Fragmentizer:
    """Prose → StoryFragment[] 转换器。

    用法:
        fz = Fragmentizer()
        fragments = fz.process("清晨。严峫推开门。\"说。\"")
    """

    # ── 对话引号模式 ──
    QUOTE_PATTERNS = [
        (re.compile(r'["“](.+?)["”]'), '"'),      # "..." 或 "..."
        (re.compile(r'「(.+?)」'), '「'),        # 「...」
        (re.compile(r'『(.+?)』'), '『'),        # 『...』
    ]

    # 中文破折号引导的对话（——说。——问。）
    DASH_SPEECH = re.compile(r'——\s*(.+?)(?:[。！？]|$)')

    # 内心独白标记
    THOUGHT_MARKERS = re.compile(
        r'(?:^|(?<=[。！？]))'           # 句首或标点后
        r'([一-鿿]{2,4})'                 # 角色名
        r'(?:心想|暗想|心道|心说|默默|寻思|思忖|忖度|嘀咕|念叨)'
        r'(.*?)(?:[。！？]|$)'            # 独白内容
    )

    # 场景分隔（段落开头的时间/地点标记）
    DIVIDER_PATTERN = re.compile(
        r'^([一二两三四五六七八九十\d]+[个]?(?:小时|天|日|周|月|年)后|[一-鿿]{2,6}(?:市|区|县|镇|村|局|所|厅|部|院|馆|店|吧|楼))'
    )

    # 动作句：以角色名开头 + 动词
    ACTION_VERBS = re.compile(
        r'^(?:他|她|它|[^\s]{1,4})'
        r'(?:站起|推开|走[进出]|跑[向去]|看[向到]|拿[起出]|放[下开]|'
        r'坐[下上]|转[身过头]|点[头了]|笑[了着]|叹[了气口]|摇[头了]|'
        r'挥[手了]|伸[出手]|收[回起]|握[紧住]|松[开手]|踢[开]|敲[了门]|'
        r'按[下了]|拨[通了]|写[了下]|扔[下出]|接[过住]|举[起]|拉[开]|'
        r'推[了开]|踩[了下]|跳[了下]|爬[起]|躺[了下]|摸[索到]|'
        r'闭[上眼]|睁[开眼]|皱[了眉]|抿[了嘴]|咳[了嗽]|'
        r'扶[着了]|靠[在]|倚[着]|蹲[下了]|抬[起头眼]|'
        r'吸[了气口]|吐[了出]|掏[出]|翻[开了]|合[上了])'
    )

    def process(self, prose: str) -> list[StoryFragment]:
        """将自然段落文本转换为 StoryFragment 列表。"""
        if not prose or not prose.strip():
            return []

        # 1. 按空行拆分段落
        paragraphs = re.split(r'\n\s*\n', prose.strip())
        fragments = []

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue

            # 场景分隔：如果段落以时间/地点标记开头，拆出 divider
            dm = self.DIVIDER_PATTERN.match(para)
            if dm:
                label = dm.group(1)
                rest = para[dm.end():].strip().lstrip("，。！？ \t")
                fragments.append(StoryFragment(type="divider", text="", divider_label=label))
                if rest:
                    fragments.append(StoryFragment(type="narration", text=rest))
                continue

            frag = self._classify(para)
            if frag:
                fragments.append(frag)

        return fragments

    def _classify(self, text: str) -> Optional[StoryFragment]:
        """将一个段落分类为一个 StoryFragment。"""

        # 内心独白
        tm = self.THOUGHT_MARKERS.search(text)
        if tm:
            character = tm.group(1)
            thought_content = tm.group(2).strip()
            if thought_content:
                return StoryFragment(
                    type="inner_thought",
                    text=thought_content.rstrip("。！？"),
                    character=character,
                )

        # 无匹配 → narration 兜底

        # 对话
        for pattern, _ in self.QUOTE_PATTERNS:
            quotes = pattern.findall(text)
            if quotes:
                # 取第一段引号内容作为 dialogue，其余归 narration
                quote_text = quotes[0]
                speaker = self._extract_speaker(text, quotes[0])
                before = text[:text.index(quotes[0])].strip().rstrip("，。！？")
                after = text[text.index(quotes[0]) + len(quotes[0]) + 2:].strip()

                # 有引号对话 → 提取为 dialogue
                if before and self._is_action(before):
                    # "严峫推开门，\"说。\"" → 前面的动作归 narration，这句归 dialogue
                    pass
                return StoryFragment(
                    type="dialogue", text=quote_text, character=speaker,
                )

        # 破折号对话
        dm = self.DASH_SPEECH.search(text)
        if dm:
            return StoryFragment(
                type="dialogue", text=dm.group(1).strip(),
                character=self._extract_character(text[:dm.start()]),
            )

        # 动作句
        if self._is_action(text):
            return StoryFragment(
                type="action",
                text=text.rstrip("。！？"),
                character=self._extract_character(text),
            )

        # 兜底 → narration
        return StoryFragment(type="narration", text=text)

    # ── 辅助 ──

    @staticmethod
    def _is_action(text: str) -> bool:
        """判断是否是动作描述句。"""
        # 短句 + 包含动作动词 → action
        if len(text) > 80:
            return False
        if Fragmentizer.ACTION_VERBS.match(text):
            return True
        # 以角色名 + 逗号开头（如"严峫，推开门"）
        if re.match(r'^[^\s]{1,4}[，,]', text) and len(text) < 60:
            return True
        return False

    @staticmethod
    def _extract_character(text: str) -> Optional[str]:
        """从文本中提取角色名。取开头的 2-4 字中文名。"""
        m = re.match(r'^([一-鿿]{2,4})', text)
        return m.group(1) if m else None

    # 常见说话动词（跟在角色名后面）
    _SPEECH_VERBS = {"道", "说", "问", "喊", "叫", "答", "回", "讲", "曰", "言"}

    @staticmethod
    def _extract_speaker(full_text: str, quote: str) -> Optional[str]:
        """从引号前的文本中提取说话人。"""
        idx = full_text.index(quote)
        before = full_text[:idx].strip()
        if not before:
            return None
        # 去掉末尾的说话动词和标点
        before = re.sub(r'[：:，,。！？\s]+$', '', before)
        chars = re.findall(r'[一-鿿]{2,4}', before)
        if chars:
            name = chars[-1]
            # 去掉末尾的说话动词
            while len(name) >= 2 and name[-1] in Fragmentizer._SPEECH_VERBS:
                name = name[:-1]
            return name if len(name) >= 2 else None
        return None
