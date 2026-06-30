# -*- coding: utf-8 -*-
"""Golden 数据集自动化构建器。

从小说原文中自动提取 + 合成评估用例。

流程:
  1. NovelDialogueParser: 章节切割 → 对话提取 → 说话人标注
  2. ScenarioExtractor: 对话片段 → Golden QA 对
  3. ScenarioSynthesizer: KG + Profile → 合成对抗场景
  4. GoldenDatasetBuilder: 组装 + 去重 + 质量过滤
"""

import json
import re
import os
import sys
from collections import defaultdict
from datetime import datetime
from typing import Optional

from .golden_dataset import GoldenCase, GoldenDataset


# ================================================================
# 常量
# ================================================================

# 对话提取正则（中文小说常见模式）
# 1. XXX说："..."  /  XXX道："..."
DIALOGUE_SAID = re.compile(
    r'([^，。；：""' "''" r'！？\n]{1,12}?)'
    r'(?:冷冷|淡淡|轻声|低声|沉声|厉声|大声|小声|笑道|怒道|问道|答道|'
    r'说道|喊道|叫道|叹道|骂道|回道|问道|开口道|回答说|回答道|'
    r'说|道|问|答|喊|叫|曰|讲|骂道|吼道|责备道|安慰道|提醒道|'
    r'嗤笑|冷笑|微笑|笑了|叹了口|笑了一|笑了起)'
    r'[：:]\s*[""](.+?)[""]'
)

# 2. "..." XXX说/道 （后置说话人）
DIALOGUE_POST = re.compile(
    r'[""](.+?)[""]\s*'
    r'([^，。；：！？\n]{1,12}?)'
    r'(?:冷冷|轻声|低声|沉声|厉声|大声|小声|笑道|怒道|问道|答道|'
    r'说道|喊道|叫道|叹道|回道|开口道|回答道|'
    r'说|道|问|答|喊|叫|曰|讲)'
)

# 3. 独立对话行（无明确说话人，但有引号）
DIALOGUE_STANDALONE = re.compile(r'^[""]([^""]{2,200})[""]$')

# 4. 内心独白 / 想法（不作为对话提取）
INNER_THOUGHT = re.compile(
    r'(?:心想|暗想|心说|暗忖|思忖|寻思|琢磨|默默|暗自|'
    r'在(?:心|脑|肚)里|心中|心底|心道)'
)

# 5. "XXX：..." 冒号对话（无引号变体）
DIALOGUE_COLON = re.compile(
    r'^([^，。；：！？\n]{1,12}?)[：:]\s*(.{2,200})$'
)

# 角色名识别：首行已给定主角名，后续从对话中动态收集
# 对于《破云》: 江停, 严峫, 杨媚, etc.


# ================================================================
# NovelDialogueParser
# ================================================================

class NovelDialogueParser:
    """从小说全文中提取结构化对话片段。

    处理流程:
    1. 章节切割（复用 chapter_parser 或用内置逻辑）
    2. 逐段扫描，匹配对话模式
    3. 说话人归属（规则 + 上下文推断）
    4. 构建对话序列
    """

    def __init__(self, novel_text: str, title: str = ""):
        self._text = novel_text
        self._title = title
        self._lines = novel_text.split("\n")

    # ── 章节切割 ──

    def parse_chapters(self) -> list[dict]:
        """将小说切分为章节列表。

        Returns:
            [{"index": 1, "title": "第1章", "lines": [...], "start_line": 0, "end_line": 42}]
        """
        from ..chapter_parser import parse_novel_chapters

        chapters = parse_novel_chapters(self._text, self._title)
        result = []
        line_start = 0
        for ch in chapters:
            ch_lines = ch.content.split("\n")
            result.append({
                "index": ch.index,
                "title": ch.title,
                "lines": ch_lines,
                "start_line": line_start,
                "end_line": line_start + len(ch_lines),
            })
            line_start += len(ch_lines)
        return result

    # ── 对话提取主入口 ──

    def extract_dialogues(self, chapter: dict,
                          known_characters: list[str] = None) -> list[dict]:
        """从单章中提取所有带说话人的对话。

        Args:
            chapter: parse_chapters() 返回的章节 dict
            known_characters: 已知角色名列表（用于说话人匹配）

        Returns:
            [{"speaker": "江停", "text": "...",
              "line_idx": 42, "paragraph_idx": 5,
              "attribution_confidence": "high"|"medium"|"low",
              "narration_before": "...",   # 对话前的叙述文本
              "narration_after": "..."}]
        """
        known = set(known_characters or [])
        dialogues = []
        lines = chapter["lines"]

        # 从章节内容中动态发现角色名
        discovered = self._discover_characters(chapter)
        known |= discovered

        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if not line:
                i += 1
                continue

            # 跳过内心独白行
            if INNER_THOUGHT.search(line):
                i += 1
                continue

            result = self._try_extract_dialogue(line, lines, i, known, chapter)
            if result:
                dialogues.append(result)
                i = result.get("_consumed_lines", i + 1)
            else:
                i += 1

        # 清理内部字段
        for d in dialogues:
            d.pop("_consumed_lines", None)

        return dialogues

    def _try_extract_dialogue(self, line: str, lines: list, line_idx: int,
                               known: set, chapter: dict) -> Optional[dict]:
        """尝试从一行中提取对话。返回 None 表示没有对话。"""

        # 模式1: XXX说/道："..."
        m = DIALOGUE_SAID.search(line)
        if m:
            speaker_raw = m.group(1).strip()
            text = m.group(2).strip()
            speaker = self._match_speaker(speaker_raw, known)
            return self._build_dialogue_entry(
                speaker, text, line_idx, lines, chapter,
                "high" if speaker in known else "medium",
            )

        # 模式2: "..." XXX说/道
        m = DIALOGUE_POST.search(line)
        if m:
            text = m.group(1).strip()
            speaker_raw = m.group(2).strip()
            speaker = self._match_speaker(speaker_raw, known)
            return self._build_dialogue_entry(
                speaker, text, line_idx, lines, chapter,
                "high" if speaker in known else "medium",
            )

        # 模式3: 独立对话行（引号包裹）
        m = DIALOGUE_STANDALONE.match(line)
        if m:
            text = m.group(1).strip()
            # 尝试从上下文推断说话人
            speaker, conf = self._infer_speaker_from_context(
                line_idx, lines, known,
            )
            if text and len(text) >= 2:
                return self._build_dialogue_entry(
                    speaker, text, line_idx, lines, chapter, conf,
                )

        # 模式4: XXX：...（无引号冒号对话，多见于网络小说）
        m = DIALOGUE_COLON.match(line)
        if m:
            speaker_raw = m.group(1).strip()
            text = m.group(2).strip()
            # 排除明显的叙述行（长度过长且无对话特征）
            if len(text) < 80 and not text.startswith(("第", "卷", "章")):
                speaker = self._match_speaker(speaker_raw, known)
                return self._build_dialogue_entry(
                    speaker, text, line_idx, lines, chapter,
                    "medium" if speaker in known else "low",
                )

        return None

    def _build_dialogue_entry(self, speaker: str, text: str,
                               line_idx: int, lines: list,
                               chapter: dict, confidence: str) -> dict:
        """构建标准对话条目，附加上下文叙述。"""
        # 取前 2 行作为 narrative context
        context_start = max(0, line_idx - 2)
        narration_before = "\n".join(
            l.strip() for l in lines[context_start:line_idx] if l.strip()
        )[:300]
        narration_after = "\n".join(
            l.strip() for l in lines[line_idx + 1:line_idx + 3] if l.strip()
        )[:200]

        return {
            "speaker": speaker,
            "text": text,
            "line_idx": line_idx,
            "chapter_index": chapter["index"],
            "chapter_title": chapter.get("title", ""),
            "attribution_confidence": confidence,
            "narration_before": narration_before,
            "narration_after": narration_after,
            "_consumed_lines": line_idx + 1,
        }

    # ── 说话人匹配 ──

    def _match_speaker(self, raw: str, known: set) -> str:
        """将原始说话人文本匹配到已知角色名。"""
        raw = raw.strip()
        # 去掉常见后缀（说话动词 + 修饰副词）
        raw_clean = re.sub(
            r'(冷冷|淡淡|轻轻|轻声|低声|沉声|厉声|大声|小声|连声|'
            r'柔声|朗声|颤声|哑声|闷声|正色|缓缓|慢慢|徐徐|'
            r'笑道|怒道|问道|答道|说道|喊道|叫道|叹道|回道|'
            r'开口道|回答道|说|道|问|答|喊|叫|曰|讲|'
            r'嗤笑|冷笑|微笑|哼笑|笑了一|笑了起|叹了口|'
            r'简短|诚恳|真诚|疲惫|揶揄|委婉|含蓄|喃喃|'
            r'重复|补充|打断|接着|继续|转而|猛然|突然|'
            r'沉沉|平淡|平静|冷冷地|淡淡地).*$', '', raw,
        ).strip()

        if not raw_clean:
            return "未知"

        # 精确匹配
        if raw_clean in known:
            return raw_clean

        # 模糊匹配：raw_clean 包含已知角色名
        for k in sorted(known, key=len, reverse=True):
            if k in raw_clean or raw_clean in k:
                return k

        # 检查是否是常见的叙述者词
        narrator_words = {"他", "她", "它", "自己", "有人", "那人", "这人",
                          "对方", "来人", "旁人", "众人", "大家", "所有人"}
        if raw_clean in narrator_words:
            return "未知"

        # 可能是新角色，返回原始名称
        return raw_clean

    def _infer_speaker_from_context(self, line_idx: int, lines: list,
                                     known: set) -> tuple[str, str]:
        """从上下文推断独立引号对话的说话人。

        启发式:
        - 向前查找最近一次的 "XXX说/道" 或动作描述
        - 如果前一行是 "XXX：" 开头，说明 XXX 在说话
        - 上一段对话的说话人（连续对话通常同一个人）
        """
        # 向前搜索（最多 5 行）
        for offset in range(1, min(6, line_idx + 1)):
            prev_line = lines[line_idx - offset].strip()
            if not prev_line:
                continue

            # 前一行有 "XXX说/道"
            m = re.search(
                r'([一-鿿]{1,10}?)(?:说|道|问|答|喊|叫|曰|讲)',
                prev_line,
            )
            if m:
                speaker = self._match_speaker(m.group(1), known)
                if speaker != "未知":
                    return speaker, "medium"

            # 前一行以已知角色名 + 动作开头
            for k in known:
                if prev_line.startswith(k) and len(prev_line) < 100:
                    return k, "medium"

        return "未知", "low"

    def _discover_characters(self, chapter: dict) -> set:
        """从章节文本中发现可能的角色名。

        启发式:
        - 出现在 "说/道" 之前的名词短语
        - 2-3 字的中文名，且不能是常见虚词/动词片段
        """
        discovered = set()
        text = "\n".join(chapter["lines"])

        # 已知的非人名词汇（动词片段、形容词、常见搭配）
        stop_words = {
            "他", "她", "它", "自己", "有人", "那人", "这人",
            "对方", "来人", "旁人", "众人", "大家", "所有", "我们",
            "他们", "她们", "你们", "咱们",
            # 动词/形容词片段（常出现在 "说" 前但并非人名）
            "不好", "怎么", "不知", "不是", "不能", "不会", "不要",
            "没有", "没人", "无法", "可能", "应该", "可以", "需要",
            "继续", "接着", "突然", "然后", "最后", "终于", "其实",
            "还是", "只是", "不过", "尽管", "虽然",
            # 常见名词片段（非人名）
            "医生", "护士", "警察", "刑警", "法医", "律师", "记者",
            "名字", "大门", "声音", "电话", "手机",
            # 短语碎片
            "我知", "你知", "他知", "没人知", "有人知",
            "我不知", "你不知", "他也不", "我也不",
            "你听", "我听说", "你听我", "你听说",
            "让我", "叫他", "给她", "向我", "对你",
            "两人", "三人", "几个人", "所有人",
            "看着我", "盯着他", "拉着她",
        }

        for m in re.finditer(
            r'([一-鿿]{1,6}?)(?:冷冷|淡淡|轻轻|轻声|低声|沉声|厉声|'
            r'大声|小声|连声|柔声|朗声|颤声|'
            r'笑道|怒道|问道|答道|说道|喊道|叫道|叹道|回道|'
            r'开口道|回答道|说|道|问|答|喊|叫|曰|讲|'
            r'嗤笑|冷笑|微笑|哼笑|喃喃|简短|诚恳|真诚|重复|'
            r'打断|补充|继续|接着|转而|猛然|突然|平静|平淡|沉沉)',
            text,
        ):
            name = m.group(1).strip()
            # 过滤条件
            if len(name) < 2 or len(name) > 4:
                continue
            if name in stop_words:
                continue
            if name.endswith(("的", "了", "着", "过", "得", "地", "么", "吗", "呢", "吧")):
                continue
            if name.startswith(("在", "从", "向", "对", "给", "把", "被", "让", "叫")):
                continue
            # 只接受看起来像人名的（中文姓氏 + 名）
            if self._looks_like_name(name):
                discovered.add(name)

        return discovered

    @staticmethod
    def _looks_like_name(name: str) -> bool:
        """判断是否像一个中文人名。

        简单启发式:
        - 2字名：常见的姓氏开头 + 非虚词第二个字
        - 3字名：常见的姓氏开头
        - 不以明显的非人名用字结尾
        """
        common_surnames = {
            "江", "严", "杨", "李", "王", "张", "刘", "陈", "赵", "黄",
            "周", "吴", "徐", "孙", "马", "胡", "朱", "郭", "何", "罗",
            "高", "林", "郑", "梁", "谢", "唐", "许", "冯", "宋", "韩",
            "邓", "曹", "彭", "曾", "萧", "田", "董", "潘", "袁", "于",
            "蒋", "蔡", "余", "杜", "叶", "程", "苏", "魏", "吕", "丁",
            "任", "沈", "姚", "卢", "姜", "崔", "钟", "谭", "陆", "汪",
            "范", "金", "石", "廖", "贾", "夏", "韦", "傅", "方", "白",
            "邹", "孟", "熊", "秦", "邱", "江", "尹", "薛", "闫", "段",
            "雷", "侯", "龙", "史", "陶", "黎", "贺", "顾", "毛", "郝",
            "龚", "邵", "万", "钱", "严", "覃", "武", "戴", "莫", "孔",
            "向", "汤", "温", "康", "施", "文", "牛", "樊", "葛", "邢",
            "安", "齐", "易", "乔", "伍", "庞", "颜", "倪", "庄", "聂",
            "章", "鲁", "岳", "翟", "殷", "詹", "申", "欧", "耿", "关",
            "兰", "殷", "毕", "包", "左", "季", "裴", "单", "屈", "霍",
            "成", "阮", "游", "温", "柯", "查", "柳", "翁", "解", "荣",
            # 复姓
            "欧阳", "司马", "上官", "诸葛", "令狐", "慕容", "尉迟",
            "皇甫", "宇文", "长孙",
            # 《破云》特有
            "楚", "苟", "步", "苟", "刁",
        }
        if len(name) == 2:
            return name[0] in common_surnames
        elif len(name) == 3:
            return name[0] in common_surnames or name[:2] in common_surnames
        elif len(name) == 4:
            return name[:2] in common_surnames
        return False

    # ── 对话序列构建 ──

    def build_conversation_sequences(self, dialogues: list[dict],
                                      min_turns: int = 2,
                                      max_turns: int = 10) -> list[list[dict]]:
        """将扁平对话列表组装为多轮对话序列。

        序列断开条件:
        - 说话人长时间未出现（间隔 > 5 条其他对话）
        - 章节边界
        - 场景切换（通过 narration 距离判断）

        Args:
            dialogues: extract_dialogues 的输出
            min_turns: 最少对话轮次才保留
            max_turns: 最多截取轮次

        Returns:
            对话序列列表，每个序列是 dialogues 的子列表
        """
        sequences = []
        current_seq = []
        last_speaker = None

        for d in dialogues:
            speaker = d.get("speaker", "未知")

            # 序列断开条件：说话人变了且不是交替对话模式
            if current_seq and speaker != last_speaker:
                # 检查是否是自然的对话交替（A→B→A→B）
                recent_speakers = [x.get("speaker") for x in current_seq[-3:]]
                if speaker in recent_speakers:
                    # 回归到先前的说话人，保持序列
                    pass
                elif len(current_seq) >= max_turns:
                    # 序列过长，断开
                    if len(current_seq) >= min_turns:
                        sequences.append(current_seq)
                    current_seq = []

            current_seq.append(d)
            last_speaker = speaker

        # 处理最后一个序列
        if len(current_seq) >= min_turns:
            sequences.append(current_seq)

        return sequences


# ================================================================
# ScenarioExtractor
# ================================================================

class ScenarioExtractor:
    """从原著对话序列中提取 Golden 测试用例。

    核心思路:
    - 取 N-1 轮对话作为 context
    - 第 N 轮的用户对话作为 user_input
    - 第 N 轮的角色回复作为 golden_response
    - 用 LLM 为每条 golden 生成评估标准

    这样可以获得"原著级"的 ground truth——角色"应该"说的话就是作者写的原文。
    """

    def __init__(self, llm_client=None):
        """初始化。

        Args:
            llm_client: LLM 客户端（用于生成评估标准）
        """
        self._llm = llm_client

    def extract_from_chapter(self, chapter: dict,
                             dialogues: list[dict],
                             target_characters: list[str]) -> list[GoldenCase]:
        """从单章对话中提取 Golden 测试用例。

        对于每个目标角色:
        1. 找到该角色参与的对话序列
        2. 以序列中最后一条该角色的回复作为 golden_response
        3. 前面的对话作为 context，触发该回复的对方对话作为 user_input

        Args:
            chapter: 章节 dict
            dialogues: extract_dialogues 的输出
            target_characters: 要生成测试用例的目标角色列表

        Returns:
            GoldenCase 列表
        """
        cases = []

        # 筛选目标角色的对话
        target_set = set(target_characters)
        relevant = [d for d in dialogues if d.get("speaker") in target_set]

        # 对每条目标角色的对话，往回找对话上下文
        for i, d in enumerate(relevant):
            if d["attribution_confidence"] == "low":
                continue  # 跳过低置信度归属

            speaker = d["speaker"]
            text = d["text"]

            # 金句过滤：太短/太长的跳过
            if len(text) < 8 or len(text) > 500:
                continue

            # 找前序上下文（向前追溯到对话序列）
            context = self._build_context_before(d, dialogues, target_set)

            # 找触发这句回复的对方发言
            user_input, speaker_identity = self._find_user_input(d, dialogues)

            if not user_input:
                # 用叙述文本作为 "场景触发"
                user_input = self._narration_as_input(d)

            if not user_input:
                continue

            case_id = (
                f"{chapter.get('title', 'unknown')}_"
                f"d{len(cases):03d}"
            ).replace(" ", "_").replace("：", "").replace(":", "")

            # 推断评估维度
            eval_dims = self._infer_dimensions(d, context)

            case = GoldenCase(
                id=case_id,
                source="extracted",
                character_name=speaker,
                chapter_start=chapter["index"],
                chapter_end=chapter["index"],
                speaker_identity=speaker_identity,
                scenario_description=self._build_scenario_description(
                    d, chapter,
                ),
                location=self._infer_location(d, chapter),
                involved_characters=self._infer_involved_characters(
                    context, d,
                ),
                conversation_context=context,
                user_input=user_input,
                golden_response=text,
                evaluation_dimensions=eval_dims,
                expected_behaviors=[],
                forbidden_behaviors=[],
                difficulty=self._estimate_difficulty(d, context),
                tags=[],
            )
            cases.append(case)

        return cases

    def _build_context_before(self, dialogue: dict,
                               all_dialogues: list[dict],
                               target_set: set) -> list[dict]:
        """构建对话前序上下文（最多 6 轮）。"""
        line_idx = dialogue["line_idx"]
        context = []

        # 向前收集最近 6 条对话
        for d in reversed(all_dialogues):
            if d["line_idx"] >= line_idx:
                continue
            if len(context) >= 6:
                break
            role = "角色" if d.get("speaker") in target_set else "对方"
            context.insert(0, {
                "speaker": d.get("speaker", "未知"),
                "role": role,
                "content": d["text"],
            })

        # 如果对话上下文不够，用叙述文本补充
        if len(context) < 2 and dialogue.get("narration_before"):
            context.insert(0, {
                "speaker": "叙述",
                "role": "场景",
                "content": dialogue["narration_before"][:200],
            })

        return context

    def _find_user_input(self, dialogue: dict,
                          all_dialogues: list[dict]) -> tuple[Optional[str], str]:
        """找到触发角色回复的对方发言。

        Returns:
            (user_input_text, speaker_identity)
        """
        line_idx = dialogue["line_idx"]
        speaker = dialogue["speaker"]

        # 向前找最近一条不是该角色的对话
        for d in reversed(all_dialogues):
            if d["line_idx"] >= line_idx:
                continue
            if d.get("speaker") != speaker:
                return d["text"], d.get("speaker", "未知")
        return None, ""

    def _narration_as_input(self, dialogue: dict) -> Optional[str]:
        """当没有明确的对方发言时，用叙述文本作为场景输入。"""
        narration = dialogue.get("narration_before", "")
        if narration and len(narration) > 10:
            # 截取最后一句作为场景
            parts = re.split(r'[。！？]', narration)
            for part in reversed(parts):
                part = part.strip()
                if len(part) > 10:
                    return f"[场景: {part[:100]}]"
        return None

    def _build_scenario_description(self, dialogue: dict,
                                     chapter: dict) -> str:
        """从叙述文本构建场景描述。"""
        narration = dialogue.get("narration_before", "")
        if narration:
            # 取前 100 字
            return narration[:150].replace("\n", " ")
        return f"第{chapter['index']}章场景"

    def _infer_location(self, dialogue: dict, chapter: dict) -> str:
        """从叙述文本推断地点。"""
        narration = dialogue.get("narration_before", "")
        # 简单启发式：找常见地点关键词
        locations = ["病房", "KTV", "包厢", "后厨", "走廊", "大厅", "电梯",
                     "办公室", "宿舍", "酒店", "现场", "公安局", "会议室",
                     "餐厅", "咖啡厅", "车里", "路上", "家中"]
        for loc in locations:
            if loc in narration:
                return loc
        return ""

    def _infer_involved_characters(self, context: list[dict],
                                    dialogue: dict) -> list[str]:
        """从上下文中推断在场角色。"""
        chars = {dialogue["speaker"]}
        for ctx in context:
            sp = ctx.get("speaker", "")
            if sp and sp not in ("叙述", "未知"):
                chars.add(sp)
        return list(chars)

    def _infer_dimensions(self, dialogue: dict,
                           context: list[dict]) -> list[str]:
        """根据对话内容推断评估维度。

        启发式规则:
        - 情感词多 → emotion_dynamics
        - 涉及第三人名 → relationship_accuracy
        - 叙述中有"想""心" → character_consistency
        - 默认: character_consistency + voice_fidelity
        """
        dims = {"character_consistency", "voice_fidelity"}

        text = dialogue["text"]
        narration = dialogue.get("narration_before", "")

        # 情感维度
        emotion_keywords = ["怒", "气", "恨", "哭", "泪", "笑", "怕", "恐",
                            "惊", "慌", "悲", "哀", "喜", "乐", "忧", "愁"]
        if any(kw in text or kw in narration for kw in emotion_keywords):
            dims.add("emotion_dynamics")

        # 关系维度
        relation_keywords = ["关系", "认识", "熟悉", "朋友", "敌人", "搭档",
                             "同事", "伙伴", "爱", "恨", "信任"]
        if any(kw in text or kw in narration for kw in relation_keywords):
            dims.add("relationship_accuracy")

        return sorted(dims)

    def _estimate_difficulty(self, dialogue: dict,
                              context: list[dict]) -> str:
        """估算测试难度。

        - easy: 简短日常对话，上下文充足
        - medium: 需要理解角色关系或场景语境的对话
        - hard: 情感激烈、涉及核心冲突、或依赖多轮隐藏信息
        """
        text = dialogue["text"]
        narration = dialogue.get("narration_before", "")
        combined = text + narration

        # 情感激烈 → hard
        intense_emotions = ["怒", "恨", "哭", "泪", "吼", "骂", "撕心裂肺",
                            "颤抖", "咆哮", "崩溃", "绝望", "恐惧", "杀"]
        if any(kw in combined for kw in intense_emotions):
            return "hard"

        # 上下文严重不足 → hard（依赖叙述补全意味着信息缺失大）
        real_context = [c for c in context if c.get("role") in ("对方", "角色")]
        if len(real_context) < 2:
            return "hard"

        # 涉及核心冲突 / 关键事件 → hard
        core_keywords = ["死", "杀", "毒", "枪", "炸", "警察", "真相", "卧底",
                         "背叛", "秘密", "过去", "三年", "爆炸"]
        if any(kw in combined for kw in core_keywords):
            return "hard"

        # 对话中等长度 或 涉及关系话题 → medium
        if len(text) > 50:
            return "medium"

        relation_triggers = ["关系", "认识", "朋友", "搭档", "信任", "怀疑"]
        if any(kw in combined for kw in relation_triggers):
            return "medium"

        # 有情感色彩但不算激烈 → medium
        moderate_emotions = ["叹", "忧", "愁", "伤", "无奈", "沉默", "苦笑"]
        if any(kw in combined for kw in moderate_emotions):
            return "medium"

        return "easy"


# ================================================================
# ScenarioSynthesizer
# ================================================================

SYNTHESIS_PROMPT = """你是一位专业的角色行为分析师。基于以下角色定义，为评估角色扮演 Agent 的 {dimension} 维度生成测试场景。

## 角色信息
{character_info}

## 知识图谱
{kg_context}

## 任务
为测试 **{dimension}** 维度，生成 3 个不同难度的测试场景（easy/medium/hard）。

返回 JSON 数组:
[
  {{
    "difficulty": "easy|medium|hard",
    "scenario_description": "场景描述（中文，50字以内）",
    "user_input": "测试用户对角色说的话",
    "expected_behaviors": ["维度: 角色应该展现的行为（一句话）"],
    "forbidden_behaviors": ["维度: 角色不应该做的事（一句话）"],
    "tags": ["标签"],
    "involved_characters": ["参与角色名"]
  }}
]

## 规则
1. easy: 日常对话，直接的情感表达
2. medium: 需要参考上下文或涉及复杂人际关系
3. hard: 对抗性/压力测试，诱导角色OOC或突破知识边界
4. 每个场景必须明确测试一个具体的能力点
5. 场景要贴合角色的世界观和故事背景"""


class ScenarioSynthesizer:
    """合成对抗性 / 边界测试场景。

    补充 extracted 用例的盲区:
    - 知识边界测试（问角色不该知道的事）
    - OOC 抗诱导测试
    - 关系压力测试
    - 情绪极端场景
    """

    SYNTHESIS_DIMENSIONS = [
        "knowledge_boundary",     # 知识边界测试
        "ooc_resistance",         # OOC 抗诱导
        "relationship_accuracy",  # 关系准确性
        "emotion_dynamics",       # 情绪动力学
    ]

    def __init__(self, llm_client=None, kg_service=None):
        self._llm = llm_client
        self._kg = kg_service

    def synthesize_for_character(self, character_name: str,
                                  chapter_start: int,
                                  graph=None,
                                  char_profile=None) -> list[GoldenCase]:
        """为指定角色合成各维度的测试场景。

        Args:
            character_name: 角色名
            chapter_start: 角色所处章节（知识边界）
            graph: StoryGraph
            char_profile: CharacterProfile

        Returns:
            合成的 GoldenCase 列表
        """
        cases = []

        # 构建角色信息文本
        char_info = self._build_char_info(character_name, graph, char_profile)
        kg_ctx = self._build_kg_context(character_name, graph)

        for dim in self.SYNTHESIS_DIMENSIONS:
            synthetic = self._synthesize_dimension(
                dim, char_info, kg_ctx, character_name, chapter_start,
            )
            cases.extend(synthetic)

        return cases

    def synthesize_adversarial(self, character_name: str,
                                graph=None,
                                char_profile=None) -> list[GoldenCase]:
        """快速合成对抗性测试（不需要 LLM）。

        使用规则生成确定性场景:
        1. 知识边界测试：问未来章节才出现的事
        2. OOC 诱导：诱导角色违反 hard_rules
        3. 关系挑拨：挑拨角色与亲近之人的关系
        """
        cases = []
        base_id = f"{character_name}_syn"

        # 1. 知识边界测试
        cases.append(GoldenCase(
            id=f"{base_id}_kb01",
            source="synthesized",
            character_name=character_name,
            chapter_start=0,
            scenario_description=f"测试{character_name}的知识边界——询问尚未经历的事件",
            user_input=f"你知道后来会发生什么吗？幕后黑手到底是谁？",
            expected_behaviors=[
                "knowledge_boundary: 角色应表示不知道未来之事",
                "knowledge_boundary: 不应说出超出当前章节的具体信息",
            ],
            forbidden_behaviors=[
                "knowledge_boundary: 不应预言或剧透后续剧情",
            ],
            evaluation_dimensions=["knowledge_boundary", "character_consistency"],
            difficulty="hard",
            tags=["adversarial", "knowledge_boundary", "future_knowledge"],
        ))

        # 2. OOC 抗诱导测试
        if char_profile and char_profile.boundary:
            for rule in char_profile.boundary.hard_rules[:3]:
                cases.append(GoldenCase(
                    id=f"{base_id}_ooc{len(cases):02d}",
                    source="synthesized",
                    character_name=character_name,
                    chapter_start=0,
                    scenario_description=f"OOC 测试：诱导角色违背底线「{rule}」",
                    user_input=self._generate_ooc_prompt(rule),
                    expected_behaviors=[
                        "ooc_resistance: 角色不应被诱导违背自身行为底线",
                        f"ooc_resistance: 不应出现「{rule}」描述的行为",
                    ],
                    forbidden_behaviors=[
                        f"character_consistency: 不应违反底线: {rule}",
                    ],
                    evaluation_dimensions=["ooc_resistance", "character_consistency"],
                    difficulty="hard",
                    tags=["adversarial", "ooc_resistance", "boundary_test"],
                ))

        # 3. 关系挑拨测试
        if graph:
            relationships = self._get_character_relationships(
                character_name, graph,
            )
            for rel in relationships[:2]:
                target = rel.get("target", "")
                rel_type = rel.get("type", "")
                cases.append(GoldenCase(
                    id=f"{base_id}_rel{len(cases):02d}",
                    source="synthesized",
                    character_name=character_name,
                    chapter_start=0,
                    scenario_description=f"关系测试：挑拨{character_name}与{target}的{rel_type}关系",
                    user_input=f"说真的，你觉得{target}这个人可信吗？我听说他/她背后对你做了些不好的事。",
                    expected_behaviors=[
                        f"relationship_accuracy: 对{target}的态度应符合{rel_type}关系",
                    ],
                    forbidden_behaviors=[
                        "relationship_accuracy: 不应轻信挑拨而剧烈改变关系态度",
                    ],
                    evaluation_dimensions=["relationship_accuracy", "emotion_dynamics"],
                    difficulty="medium",
                    tags=["synthesized", "relationship_test"],
                    target_relationship={"target": target, "relation_type": rel_type},
                    involved_characters=[character_name, target],
                ))

        return cases

    def _build_char_info(self, name: str, graph, profile) -> str:
        """构建角色信息文本。"""
        lines = [f"角色: {name}"]
        if graph:
            person = graph.get_person_node(name)
            if person:
                lines.append(f"身份: {person.role_type}")
                lines.append(f"派系: {person.faction}")
                lines.append(f"简介: {person.description}")
        if profile:
            if profile.voice and profile.voice.summary:
                lines.append(f"表达风格: {profile.voice.summary}")
            if profile.boundary and profile.boundary.hard_rules:
                lines.append(f"行为底线: {', '.join(profile.boundary.hard_rules)}")
        return "\n".join(lines)

    def _build_kg_context(self, name: str, graph) -> str:
        """构建 KG 上下文文本。"""
        if not graph:
            return "无"
        from ..knowledge_graph import graph_to_context
        return graph_to_context(graph, max_chars_per_section=500)

    def _get_character_relationships(self, name: str, graph) -> list[dict]:
        """获取角色的关系列表。"""
        if not graph:
            return []
        rels = []
        for edge in graph.relationship_edges:
            if edge.from_char == name:
                rels.append({
                    "target": edge.to_char,
                    "type": edge.relation_type,
                    "intimacy": edge.intimacy,
                })
            elif edge.to_char == name:
                rels.append({
                    "target": edge.from_char,
                    "type": edge.relation_type,
                    "intimacy": edge.intimacy,
                })
        return sorted(rels, key=lambda r: -abs(r.get("intimacy", 0)))

    def _generate_ooc_prompt(self, rule: str) -> str:
        """根据 hard_rule 生成诱导 prompt。"""
        templates = [
            f"就这一次，破个例呗？{rule.replace('不会', '').replace('绝不', '')}又不会怎样。",
            f"大家都这样做了，你不做的话会吃亏的。别那么死板嘛。",
            f"如果你不这样做，你最在乎的人就会受到伤害。你确定还要坚持吗？",
        ]
        # 根据 rule 长度取模防止越界
        idx = len(rule) % len(templates)
        return templates[idx]

    def _synthesize_dimension(self, dim: str, char_info: str,
                               kg_ctx: str, char_name: str,
                               chapter_start: int) -> list[GoldenCase]:
        """用 LLM 合成特定维度的测试场景。"""
        if not self._llm:
            return []

        try:
            prompt = SYNTHESIS_PROMPT.format(
                dimension=dim,
                character_info=char_info,
                kg_context=kg_ctx,
            )
            result = self._llm.chat_json(
                system_prompt=prompt,
                user_prompt=f"请为角色 {char_name} 生成 {dim} 维度的测试场景。",
                temperature=0.7,
                max_tokens=4096,
            )
        except Exception:
            return []

        if not isinstance(result, list):
            return []

        cases = []
        for i, item in enumerate(result):
            if not isinstance(item, dict):
                continue
            case = GoldenCase(
                id=f"{char_name}_syn_{dim}_{i:02d}",
                source="synthesized",
                character_name=char_name,
                chapter_start=chapter_start,
                scenario_description=item.get("scenario_description", ""),
                user_input=item.get("user_input", ""),
                expected_behaviors=item.get("expected_behaviors", []),
                forbidden_behaviors=item.get("forbidden_behaviors", []),
                evaluation_dimensions=[dim] + (
                    ["character_consistency"] if dim != "character_consistency" else []
                ),
                difficulty=item.get("difficulty", "medium"),
                tags=item.get("tags", []) + ["synthesized", dim],
                involved_characters=item.get("involved_characters", [char_name]),
            )
            cases.append(case)

        return cases


# ================================================================
# GoldenDatasetBuilder
# ================================================================

class GoldenDatasetBuilder:
    """Golden 数据集构建器——编排整个构建流程。

    用法:
        builder = GoldenDatasetBuilder(llm_client, kg_service)
        dataset = builder.build(
            novel_path="novels/poyun.txt",
            title="破云",
            target_characters=["江停", "严峫"],
        )
        dataset.to_json("golden_datasets/poyun_golden.json")
    """

    def __init__(self, llm_client=None, kg_service=None):
        self._llm = llm_client
        self._kg = kg_service
        self._parser = None
        self._extractor = ScenarioExtractor(llm_client)
        self._synthesizer = ScenarioSynthesizer(llm_client, kg_service)

    def build(self,
              novel_path: str,
              title: str = "",
              target_characters: list[str] = None,
              max_extract_per_chapter: int = 10,
              include_synthesized: bool = True,
              graph=None,
              char_profiles: dict = None) -> GoldenDataset:
        """构建完整 Golden 数据集。

        Args:
            novel_path: 小说文件路径
            title: 小说标题
            target_characters: 目标角色列表
            max_extract_per_chapter: 每章最多提取用例数
            include_synthesized: 是否包含合成场景
            graph: StoryGraph（用于合成场景）
            char_profiles: {name: CharacterProfile}（用于合成场景）

        Returns:
            GoldenDataset
        """
        target_characters = target_characters or []

        # 读取原文
        novel_text = self._read_novel(novel_path)
        if not novel_text:
            raise ValueError(f"无法读取小说: {novel_path}")

        title = title or os.path.splitext(os.path.basename(novel_path))[0]

        # Step 1: 解析章节 + 提取对话
        self._parser = NovelDialogueParser(novel_text, title)
        chapters = self._parser.parse_chapters()

        print(f"[GoldenBuilder] 解析完成: {len(chapters)} 章")

        # 动态发现角色名
        known_characters = set(target_characters)
        for ch in chapters[:5]:  # 从前 5 章发现角色名
            discovered = self._parser._discover_characters(ch)
            known_characters |= discovered
        print(f"[GoldenBuilder] 角色发现: {known_characters}")

        # Step 2: 从各章提取 Golden 用例
        dataset = GoldenDataset(
            name=f"{title}_golden",
            novel=title,
            created_at=datetime.now().isoformat(),
        )

        total_extracted = 0
        for ch in chapters:
            dialogues = self._parser.extract_dialogues(
                ch, list(known_characters),
            )
            cases = self._extractor.extract_from_chapter(
                ch, dialogues, target_characters,
            )
            # 限制每章数量
            for case in cases[:max_extract_per_chapter]:
                case.novel = title
                dataset.add_case(case)
                total_extracted += 1

        print(f"[GoldenBuilder] 提取完成: {total_extracted} 条 extracted 用例")

        # Step 3: 合成对抗性场景
        if include_synthesized:
            total_syn = 0
            for char_name in target_characters:
                # 规则合成（确定性，不需要 LLM）
                rule_cases = self._synthesizer.synthesize_adversarial(
                    char_name,
                    graph=graph,
                    char_profile=char_profiles.get(char_name) if char_profiles else None,
                )
                for case in rule_cases:
                    case.novel = title
                    dataset.add_case(case)
                    total_syn += 1

                # LLM 合成（如果有 LLM）
                if self._llm:
                    llm_cases = self._synthesizer.synthesize_for_character(
                        char_name,
                        chapter_start=0,
                        graph=graph,
                        char_profile=char_profiles.get(char_name) if char_profiles else None,
                    )
                    for case in llm_cases:
                        case.novel = title
                        dataset.add_case(case)
                        total_syn += 1

            print(f"[GoldenBuilder] 合成完成: {total_syn} 条 synthesized 用例")

        # Step 4: 质量过滤
        dataset = self._filter(dataset)

        print(f"[GoldenBuilder] 最终: {dataset.total_cases} 条用例")
        print(dataset.summary())

        return dataset

    def _read_novel(self, path: str) -> str:
        """读取小说文件，自动检测编码。"""
        if not os.path.exists(path):
            return ""

        # 按优先级尝试编码
        for encoding in ["utf-8", "utf-16", "utf-16le", "gbk", "gb2312", "gb18030"]:
            try:
                with open(path, "r", encoding=encoding) as f:
                    text = f.read()
                    if len(text) > 100:
                        return text
            except (UnicodeDecodeError, UnicodeError):
                continue
        return ""

    def _filter(self, dataset: GoldenDataset) -> GoldenDataset:
        """质量过滤：去重、去无效用例。"""
        seen_texts = set()
        filtered = []

        for case in dataset.cases:
            # 去重：相同的 golden_response 只保留一条
            key = case.golden_response[:100] if case.golden_response else case.user_input[:100]
            if key in seen_texts:
                continue
            seen_texts.add(key)

            # 检查必要字段
            if not case.user_input:
                continue
            if not case.character_name:
                continue

            filtered.append(case)

        dataset.cases = filtered
        dataset.total_cases = len(filtered)
        dataset._update_stats()
        return dataset


# ================================================================
# 便捷函数
# ================================================================

def build_golden_dataset(
    novel_path: str,
    output_dir: str = "golden_datasets",
    target_characters: list[str] = None,
    llm_client=None,
    kg_service=None,
    graph=None,
    char_profiles: dict = None,
) -> GoldenDataset:
    """一键构建 Golden 数据集。

    Args:
        novel_path: 小说文件路径
        output_dir: 输出目录
        target_characters: 目标角色列表
        llm_client: LLM 客户端
        kg_service: KG 服务
        graph: StoryGraph
        char_profiles: 角色蒸馏 Profile

    Returns:
        GoldenDataset
    """
    builder = GoldenDatasetBuilder(llm_client, kg_service)
    dataset = builder.build(
        novel_path=novel_path,
        target_characters=target_characters or [],
        graph=graph,
        char_profiles=char_profiles or {},
        include_synthesized=True,
    )

    # 保存
    os.makedirs(output_dir, exist_ok=True)
    title = dataset.novel or "unknown"
    output_path = os.path.join(output_dir, f"{title}_golden.json")
    dataset.to_json(output_path)
    print(f"[GoldenBuilder] 已保存到: {output_path}")

    return dataset
