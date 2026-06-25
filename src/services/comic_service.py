# -*- coding: utf-8 -*-
"""漫画排版服务 —— 将分格图片拼接为最终漫画页面。"""

import os
from typing import Optional, TYPE_CHECKING

from PIL import Image, ImageDraw, ImageFont

if TYPE_CHECKING:
    from novel2comic.src.models import Scene, StyleProfile, ComicPage


class ComicCompilationService:
    """漫画排版服务。

    将分格图片按风格拼接为最终漫画页面。
    提取自 agent.py compile_comic 工具（第 1255-1366 行）。
    """

    PANEL_GAP = 20
    MARGIN = 40
    BUBBLE_PADDING = 12
    MAX_SCROLL_WIDTH = 800

    def __init__(self):
        self._font_cache = {}

    def _load_font(self, size: int) -> ImageFont.FreeTypeFont:
        """加载中文字体（带缓存）。"""
        key = f"font_{size}"
        if key in self._font_cache:
            return self._font_cache[key]

        font_paths = [
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simsun.ttc",
            "C:/Windows/Fonts/arial.ttf",
        ]
        for fp in font_paths:
            if os.path.exists(fp):
                try:
                    font = ImageFont.truetype(fp, size)
                    self._font_cache[key] = font
                    return font
                except Exception:
                    continue
        font = ImageFont.load_default()
        self._font_cache[key] = font
        return font

    def compile_scene(
        self,
        scene,
        output_dir: str,
    ) -> Optional:
        """将单个场景的分格编译为一张漫画页面。

        Args:
            scene: Scene 对象（含 panels）
            output_dir: 输出目录

        Returns:
            ComicPage 对象，或无图片时返回 None
        """
        from novel2comic.src.models import ComicPage

        panel_imgs = []
        for panel in scene.panels:
            if panel.generated_image_path and os.path.exists(panel.generated_image_path):
                panel_imgs.append((panel, Image.open(panel.generated_image_path)))

        if not panel_imgs:
            return None

        scene_width = self.MAX_SCROLL_WIDTH
        resized = []
        total_h = 0
        for panel, img in panel_imgs:
            ratio = scene_width / img.width
            nh = int(img.height * ratio)
            img = img.resize((scene_width, nh), Image.LANCZOS)
            resized.append((panel, img))
            total_h += nh + self.PANEL_GAP

        font = self._load_font(18)
        font_small = self._load_font(14)
        total_h += 80 * len(resized)

        canvas = Image.new(
            "RGB",
            (scene_width, total_h + self.MARGIN * 2),
            color=(30, 30, 40),
        )
        draw = ImageDraw.Draw(canvas)
        y = self.MARGIN

        for idx, (panel, img) in enumerate(resized):
            canvas.paste(img, (0, y))
            ph = img.height

            # 场景标题（仅第一格）
            if idx == 0:
                title_font = self._load_font(22)
                draw.text(
                    (20, y + 10),
                    f"场景: {scene.title}",
                    fill=(255, 255, 255),
                    font=title_font,
                )

            # 对话框
            if panel.dialogue:
                y = self._draw_dialogue_bubble(
                    canvas, draw, panel.dialogue, scene_width, y, ph, font,
                )
            else:
                y += ph + self.PANEL_GAP

            # 格编号
            draw.text(
                (scene_width - 80, y - 30),
                f"格{panel.panel_number}",
                fill=(150, 150, 170),
                font=font_small,
            )

        comics_dir = os.path.join(output_dir, "comics")
        os.makedirs(comics_dir, exist_ok=True)
        op = os.path.join(comics_dir, f"scene_{scene.id:02d}.png")
        canvas.save(op, "PNG")
        return ComicPage(page_number=scene.id, image_path=op)

    def _draw_dialogue_bubble(
        self, canvas, draw, text: str, scene_width: int,
        y: int, ph: int, font,
    ) -> int:
        """绘制对话框。返回更新后的 y 坐标。"""
        max_tw = scene_width - self.MARGIN * 2 - self.BUBBLE_PADDING * 2 - 40
        lines = []
        cur = ""
        for ch in list(text):
            test = cur + ch
            if draw.textbbox((0, 0), test, font=font)[2] > max_tw:
                lines.append(cur)
                cur = ch
            else:
                cur = test
        if cur:
            lines.append(cur)

        lh = draw.textbbox((0, 0), "啊", font=font)[3] + 4
        th = lh * len(lines)
        bh = th + self.BUBBLE_PADDING * 2
        bx = self.MARGIN + 20
        bw = scene_width - self.MARGIN * 2 - 40

        draw.rounded_rectangle(
            [bx, y + ph + 10, bx + bw, y + ph + 10 + bh],
            radius=16,
            fill=(255, 255, 255, 230),
            outline=(60, 60, 60),
            width=2,
        )
        ty = y + ph + 10 + self.BUBBLE_PADDING
        for line in lines:
            tw = draw.textbbox((0, 0), line, font=font)[2]
            draw.text(
                ((scene_width - tw) // 2, ty),
                line,
                fill=(20, 20, 20),
                font=font,
            )
            ty += lh
        return y + ph + bh + self.PANEL_GAP + 10

    def compile_all(
        self,
        scenes: list,
        output_dir: str,
    ) -> list:
        """将所有场景编译为漫画页面。

        Args:
            scenes: Scene 对象列表
            output_dir: 输出目录

        Returns:
            ComicPage 对象列表
        """
        pages = []
        for scene in scenes:
            page = self.compile_scene(scene, output_dir)
            if page:
                pages.append(page)
        return pages
