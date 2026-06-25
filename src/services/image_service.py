# -*- coding: utf-8 -*-
"""图片生成服务 —— 封装 img_adapter.py 并提供批量编排。"""

import os
from typing import Optional, TYPE_CHECKING

from novel2comic.src.img_adapter import ImageGenAdapter

if TYPE_CHECKING:
    from novel2comic.src.models import Scene, StyleProfile, CharacterSheet


class ImageGenerationService:
    """图片生成服务。

    封装 ImageGenAdapter，增加批量编排逻辑。
    原本在 agent.py generate_images 工具中的循环逻辑移到这里。
    """

    def __init__(self, adapter: Optional[ImageGenAdapter] = None):
        self._adapter = adapter or ImageGenAdapter()

    @property
    def use_placeholder(self) -> bool:
        """是否使用占位图。"""
        return self._adapter.use_placeholder

    def generate_panel(
        self,
        sd_prompt: str,
        output_dir: str,
        width: int = 1024,
        height: int = 1024,
        ref_image_path: str = "",
    ) -> str:
        """生成单格图片。返回文件路径。"""
        return self._adapter.generate(
            sd_prompt, output_dir, width, height, ref_image_path,
        )

    def generate_all_panels(
        self,
        scenes: list,
        style_profile,
        output_dir: str,
    ) -> int:
        """为场景列表中所有待处理的分格生成图片。

        复用原先 agent.py generate_images 工具的编排逻辑。

        Args:
            scenes: Scene 对象列表（含 panels）
            style_profile: StyleProfile 风格配置
            output_dir: 图片输出目录

        Returns:
            生成的图片数量
        """
        images_dir = os.path.join(output_dir, "images")
        os.makedirs(images_dir, exist_ok=True)

        # 从风格获取尺寸
        ar = style_profile.aspect_ratio
        if ar == "9:16":
            w, h = 576, 1024
        elif ar == "4:3":
            w, h = 1024, 768
        elif ar == "16:9":
            w, h = 1024, 576
        else:
            w, h = 1024, 1024

        generated_count = 0
        for scene in scenes:
            for panel in scene.panels:
                if panel.generated_image_path and os.path.exists(panel.generated_image_path):
                    continue  # 已有图片，跳过

                # 注入风格基座
                full_prompt = panel.sd_prompt
                if style_profile.sd_base_prompt and style_profile.sd_base_prompt not in full_prompt:
                    full_prompt = f"{style_profile.sd_base_prompt}, {full_prompt}"

                # 注入角色触发词
                for ref_name in getattr(panel, "character_refs", []) or []:
                    if ref_name not in full_prompt:
                        full_prompt += f", {ref_name}"

                filepath = self._adapter.generate(
                    full_prompt, images_dir, w, h,
                )
                panel.generated_image_path = filepath
                panel.status = "generated"
                generated_count += 1

        return generated_count
