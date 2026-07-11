# -*- coding: utf-8 -*-
"""章节解析器——从小说全文自动识别和切分章节。"""

import re
from .core.models import ChapterInfo


# 中文数字映射
_CN_NUM = {
    "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    "十": 10, "百": 100, "千": 1000,
}


def _parse_cn_number(s: str) -> int:
    """解析中文数字为整数。如 "一百二十三" → 123, "三百零五" → 305。"""
    s = s.strip()
    # 尝试直接解析阿拉伯数字
    try:
        return int(s)
    except ValueError:
        pass

    result = 0
    temp = 0
    for ch in s:
        if ch in _CN_NUM:
            val = _CN_NUM[ch]
            if val >= 10:
                if temp == 0:
                    temp = 1
                result += temp * val
                temp = 0
            else:
                temp = val
        else:
            # 非数字字符（如 "两"）
            if ch == "两":
                temp = 2
    result += temp
    return result


# 章节标题匹配模式（按优先级排序）
CHAPTER_PATTERNS = [
    # "第X章 标题" / "第X章"
    re.compile(r"^第\s*([一二三四五六七八九十百千零\d]+)\s*[章节回]\s*(.*)$"),
    # "第X卷 第Y章" → 取最后的章节号
    re.compile(r"^第\s*[一二三四五六七八九十百千零\d]+\s*卷\s+第\s*([一二三四五六七八九十百千零\d]+)\s*[章节回]\s*(.*)$"),
    # "Chapter X: Title" / "Chapter X"
    re.compile(r"^[Cc]hapter\s+(\d+)\s*:?\s*(.*)$"),
    # "X、标题" (数字顿号开头，可能是章节)
    re.compile(r"^(\d+)[、，,.]\s*(.{2,50})$"),
]


def parse_novel_chapters(text: str, title: str = "未命名小说") -> list[ChapterInfo]:
    """从小说全文解析所有章节。

    识别规则（按优先级）：
    1. "第X章 标题" 格式
    2. "第X卷 第Y章" 格式
    3. "Chapter X" 格式
    4. 数字顿号开头（如 "1、初入江湖"）

    Args:
        text: 小说全文
        title: 书名（用于生成默认章节标题）

    Returns:
        按章节顺序排列的 ChapterInfo 列表
    """
    lines = text.split("\n")
    chapter_boundaries = []  # [(line_index, chapter_number, chapter_title)]

    for i, line in enumerate(lines):
        line_stripped = line.strip()
        if not line_stripped:
            continue

        matched = False
        for pattern in CHAPTER_PATTERNS:
            m = pattern.match(line_stripped)
            if m:
                try:
                    num_str = m.group(1)
                    chapter_num = _parse_cn_number(num_str)
                    chapter_title = m.group(2).strip() if m.lastindex and m.lastindex >= 2 else ""
                    chapter_boundaries.append((i, chapter_num, chapter_title))
                    matched = True
                except (ValueError, IndexError):
                    continue
                break

        # 如果没匹配到，但上一行是空行且当前行以数字顿号开头（备用）
        if not matched and i > 0 and not lines[i-1].strip():
            fallback = re.match(r"^(\d{1,3})[、，,.\s]+(.{2,50})$", line_stripped)
            if fallback:
                try:
                    chapter_num = int(fallback.group(1))
                    chapter_title = fallback.group(2).strip()
                    chapter_boundaries.append((i, chapter_num, chapter_title))
                except ValueError:
                    continue

    # 没有识别到任何章节 → 整本当作一章
    if not chapter_boundaries:
        return [ChapterInfo(
            index=1, title=title, content=text.strip(),
            word_count=len(text), status="pending",
        )]

    # 排序 + 去重（按行号）
    chapter_boundaries.sort(key=lambda x: x[0])
    seen_nums = set()
    unique_boundaries = []
    for boundary in chapter_boundaries:
        if boundary[1] not in seen_nums:
            unique_boundaries.append(boundary)
            seen_nums.add(boundary[1])

    # 按章节切分正文
    chapters = []
    for idx, (line_idx, chapter_num, chapter_title) in enumerate(unique_boundaries):
        # 确定本章内容结束行
        if idx + 1 < len(unique_boundaries):
            end_line = unique_boundaries[idx + 1][0]
        else:
            end_line = len(lines)

        # 提取本章内容（从章节标题行到下一章之前）
        chapter_lines = lines[line_idx:end_line]
        chapter_text = "\n".join(chapter_lines).strip()

        if not chapter_title:
            chapter_title = f"第{chapter_num}章"

        chapters.append(ChapterInfo(
            index=chapter_num,
            title=chapter_title,
            content=chapter_text,
            word_count=len(chapter_text),
            status="pending",
        ))

    return chapters


def parse_and_reindex(text: str, title: str = "未命名小说") -> list[ChapterInfo]:
    """解析章节并按 1-N 重新编号（处理原文章节号不连续的情况）。"""
    chapters = parse_novel_chapters(text, title)
    for i, ch in enumerate(chapters):
        ch.index = i + 1
    return chapters
