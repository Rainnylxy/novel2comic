# -*- coding: utf-8 -*-
"""StyleProfile 定义 + 自动风格判断。"""

from novel2comic.src.models import StyleProfile


STYLE_MANGA = StyleProfile(
    name="manga",
    color_mode="bw_screentone",
    reading_direction="rtl_page",
    aspect_ratio="4:3",
    sd_base_prompt="manga style, black and white, screentone, speed lines, line art, high contrast",
    speech_bubble_style="irregular_rounded",
    sfx_style="hand_drawn_bold",
    layout_mode="grid",
)

STYLE_WEBTOON = StyleProfile(
    name="webtoon",
    color_mode="full_color",
    reading_direction="vertical_scroll",
    aspect_ratio="9:16",
    sd_base_prompt="webtoon style, full color, soft palette, manhwa, clean lines, gentle shading",
    speech_bubble_style="clean_rounded_rect",
    sfx_style="digital_gradient",
    layout_mode="scroll",
)

STYLE_GUFENG = StyleProfile(
    name="gufeng",
    color_mode="ink_wash",
    reading_direction="flexible",
    aspect_ratio="9:16",
    sd_base_prompt="chinese ink painting style, gufeng, watercolor wash, ancient chinese comic, elegant muted colors, flowing brushwork",
    speech_bubble_style="scroll_label",
    sfx_style="calligraphy_brush",
    layout_mode="scroll",
)

BUILTIN_STYLES = {
    "manga": STYLE_MANGA,
    "webtoon": STYLE_WEBTOON,
    "gufeng": STYLE_GUFENG,
}

GENRE_STYLE_MAP = {
    "武侠": "gufeng",
    "仙侠": "gufeng",
    "玄幻": "gufeng",
    "历史": "gufeng",
    "古装": "gufeng",
    "古代": "gufeng",
    "轻小说": "manga",
    "校园": "manga",
    "恋爱": "manga",
    "日常": "manga",
    "异世界": "manga",
    "都市": "webtoon",
    "职场": "webtoon",
    "现实": "webtoon",
    "娱乐圈": "webtoon",
    "悬疑": None,
    "科幻": None,
}


def detect_style(genre_tags: list[str], pace: str = "") -> StyleProfile:
    """根据题材标签自动判断漫画风格。"""
    scores = {"gufeng": 0, "manga": 0, "webtoon": 0}
    for tag in genre_tags:
        mapped = GENRE_STYLE_MAP.get(tag)
        if mapped:
            scores[mapped] += 1
    for tag in genre_tags:
        if GENRE_STYLE_MAP.get(tag) is None:
            if pace in ("快节奏", "紧张", "动作"):
                scores["manga"] += 1
            else:
                scores["webtoon"] += 1
    # 同分时优先: gufeng > manga > webtoon
    best = max(scores, key=scores.get)
    if scores[best] == 0:
        return STYLE_WEBTOON
    return BUILTIN_STYLES[best]


def get_style(name: str) -> StyleProfile:
    """按名称获取风格。"""
    if name in BUILTIN_STYLES:
        return BUILTIN_STYLES[name]
    raise ValueError(f"Unknown style: {name}. Available: {list(BUILTIN_STYLES.keys())}")
