# -*- coding: utf-8 -*-
"""项目管理服务 —— 保存/加载/注册表操作。

封装 src/novel_registry.py 加上 novel.json 的保存/加载。
"""

import os
import json
from datetime import datetime
from typing import Optional, TYPE_CHECKING

from novel2comic.src.novel_registry import (
    register_novel,
    find_novel,
    list_all_novels,
    update_novel_access,
    update_novel_style,
    update_novel_chapters,
)

if TYPE_CHECKING:
    from novel2comic.src.models import Novel, ChapterData


class ProjectService:
    """项目管理服务。

    封装小说注册表 + novel.json / chapter_data.json 的持久化。
    提取自 agent.py 中 save_project、load_novel、_read_text_file 等逻辑。
    """

    def __init__(self, projects_dir: str = ""):
        self._projects_dir = projects_dir

    def set_projects_dir(self, path: str):
        """设置项目根目录。"""
        self._projects_dir = path

    @property
    def projects_dir(self) -> str:
        return self._projects_dir

    def create_project_dir(self, prefix: str = "") -> str:
        """创建带时间戳的项目目录。"""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"{prefix}_{ts}" if prefix else ts
        project_dir = os.path.join(self._projects_dir, name)
        os.makedirs(project_dir, exist_ok=True)
        return project_dir

    def save_novel(self, novel: "Novel") -> str:
        """保存 novel.json 并更新注册表。

        Returns:
            保存的文件路径
        """
        filepath = os.path.join(novel.output_dir, "novel.json")
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(novel.to_dict(), f, ensure_ascii=False, indent=2)

        # 更新注册表
        register_novel(
            novel_path=novel.file_path,
            title=novel.title,
            total_chapters=len(novel.chapters) if novel.chapters else 0,
            project_dir=novel.output_dir,
            style=getattr(novel, "style", ""),
        )
        return filepath

    def save_chapter_data(self, chapter_data: "ChapterData") -> str:
        """保存 chapter_data.json。

        Returns:
            保存的文件路径
        """
        filepath = os.path.join(
            chapter_data.output_dir, "chapter_data.json",
        )
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        chapter_data.save(filepath)
        return filepath

    def save_project(
        self, novel: "Novel", chapter_data: "ChapterData",
    ) -> list[str]:
        """保存所有项目文件。

        Returns:
            已保存的文件路径列表
        """
        saved = []
        if novel:
            saved.append(self.save_novel(novel))
        if chapter_data:
            saved.append(self.save_chapter_data(chapter_data))
        return saved

    def load_novel(self, file_path: str) -> Optional["Novel"]:
        """尝试从注册表加载小说（缓存命中），失败返回 None。"""
        from novel2comic.src.models import Novel

        entry = find_novel(file_path)
        if not entry:
            return None

        novel_json_path = os.path.join(entry.project_dir, "novel.json")
        if not os.path.exists(novel_json_path):
            return None

        novel = Novel.load(novel_json_path)
        update_novel_access(file_path)
        return novel

    def force_load_novel(
        self,
        file_path: str,
        text: str,
        chapters: list,
        style: str = "",
    ) -> "Novel":
        """强制首次加载：解析章节、创建 Novel、注册。

        Args:
            file_path: 小说文件路径
            text: 小说全文
            chapters: ChapterInfo 列表
            style: 漫画风格

        Returns:
            新创建的 Novel 对象
        """
        from novel2comic.src.models import Novel

        project_dir = self.create_project_dir(
            os.path.splitext(os.path.basename(file_path))[0]
        )

        novel = Novel(
            title=os.path.splitext(os.path.basename(file_path))[0],
            file_path=file_path,
            chapters=chapters,
            output_dir=project_dir,
        )
        if style:
            novel.style = style

        return novel

    def list_novels(self) -> list:
        """列出所有已注册小说。"""
        return list_all_novels()

    def resume_novel(self, index: int) -> Optional["Novel"]:
        """按索引恢复小说。"""
        novels = self.list_novels()
        if 0 <= index < len(novels):
            entry = novels[index]
            return self.load_novel(entry.novel_path)
        return None

    @staticmethod
    def read_text_file(file_path: str) -> str:
        """读取文本文件，自动检测编码。

        复用 agent.py _read_text_file 的逻辑。
        """
        encodings = ["utf-8", "utf-16", "gbk", "gb18030", "latin-1"]
        for enc in encodings:
            try:
                with open(file_path, "r", encoding=enc) as f:
                    return f.read()
            except (UnicodeDecodeError, UnicodeError):
                continue
        raise ValueError(f"无法解码文件: {file_path}")
