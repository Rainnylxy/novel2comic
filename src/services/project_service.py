# -*- coding: utf-8 -*-
"""项目管理服务 —— novel.json 持久化。"""

import os
import json
from datetime import datetime
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import Novel


class ProjectService:
    """项目管理服务。"""

    def __init__(self, projects_dir: str = ""):
        self._projects_dir = projects_dir

    def create_project_dir(self, prefix: str = "") -> str:
        """创建带时间戳的项目目录。"""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"{prefix}_{ts}" if prefix else ts
        project_dir = os.path.join(self._projects_dir, name)
        os.makedirs(project_dir, exist_ok=True)
        return project_dir

    def save_novel(self, novel: "Novel") -> str:
        """保存 novel.json。"""
        filepath = os.path.join(novel.output_dir, "novel.json")
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(novel.to_dict(), f, ensure_ascii=False, indent=2)
        return filepath

    @staticmethod
    def read_text_file(file_path: str) -> str:
        """读取文本文件，自动检测编码。"""
        encodings = ["utf-8", "utf-16", "gbk", "gb18030", "latin-1"]
        for enc in encodings:
            try:
                with open(file_path, "r", encoding=enc) as f:
                    return f.read()
            except (UnicodeDecodeError, UnicodeError):
                continue
        raise ValueError(f"无法解码文件: {file_path}")
