# -*- coding: utf-8 -*-
"""小说注册表——管理已上传小说，支持"一次解析、多次访问"。"""

import os
import json
import hashlib
from datetime import datetime
from typing import Optional


def _get_registry_path() -> str:
    """获取注册表文件路径。"""
    # 放在 novel2comic/projects/ 下
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    projects_dir = os.path.join(base, "projects")
    os.makedirs(projects_dir, exist_ok=True)
    return os.path.join(projects_dir, "novel_registry.json")


def _compute_file_hash(file_path: str) -> str:
    """计算文件的 SHA256 哈希（用于检测文件变化）。"""
    sha = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha.update(chunk)
    return sha.hexdigest()


def load_registry() -> dict:
    """加载注册表。返回 { file_hash_or_path: entry } 的字典。"""
    path = _get_registry_path()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_registry(registry: dict):
    """保存注册表。"""
    path = _get_registry_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)


class NovelRegistryEntry:
    """注册表中的单条记录。"""
    def __init__(
        self,
        novel_path: str,
        title: str,
        total_chapters: int,
        project_dir: str,
        style: str = "",
        file_hash: str = "",
        last_accessed: str = "",
    ):
        self.novel_path = novel_path
        self.title = title
        self.total_chapters = total_chapters
        self.project_dir = project_dir       # novel.json 所在目录
        self.style = style                   # 全书风格
        self.file_hash = file_hash           # 文件哈希（检测变化）
        self.last_accessed = last_accessed   # 最后访问时间

    def to_dict(self) -> dict:
        return {
            "novel_path": self.novel_path,
            "title": self.title,
            "total_chapters": self.total_chapters,
            "project_dir": self.project_dir,
            "style": self.style,
            "file_hash": self.file_hash,
            "last_accessed": self.last_accessed,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "NovelRegistryEntry":
        return cls(**d)


def register_novel(
    novel_path: str,
    title: str,
    total_chapters: int,
    project_dir: str,
    style: str = "",
) -> NovelRegistryEntry:
    """注册一本新小说（或更新已有记录）。"""
    registry = load_registry()

    file_hash = _compute_file_hash(novel_path)
    encoded = _encode_key(novel_path)

    entry = NovelRegistryEntry(
        novel_path=os.path.abspath(novel_path),
        title=title,
        total_chapters=total_chapters,
        project_dir=project_dir,
        style=style,
        file_hash=file_hash,
        last_accessed=datetime.now().isoformat(),
    )
    registry[encoded] = entry.to_dict()
    save_registry(registry)
    return entry


def find_novel(novel_path: str) -> Optional[NovelRegistryEntry]:
    """在注册表中查找小说。

    如果找到且文件哈希未变 → 返回缓存条目（无需重新解析）。
    如果找到但文件哈希变了 → 返回 None（需要重新解析）。
    如果未找到 → 返回 None。
    """
    registry = load_registry()
    encoded = _encode_key(novel_path)

    if encoded not in registry:
        # 也尝试用绝对路径匹配
        abs_path = os.path.abspath(novel_path)
        for key, entry_dict in registry.items():
            if entry_dict.get("novel_path") == abs_path:
                encoded = key
                break
        else:
            return None

    entry = NovelRegistryEntry.from_dict(registry[encoded])

    # 检查文件是否还存在
    if not os.path.exists(entry.novel_path):
        return None

    # 检查文件是否被修改过
    current_hash = _compute_file_hash(entry.novel_path)
    if current_hash != entry.file_hash:
        return None  # 文件已变化，需要重新解析

    return entry


def list_all_novels() -> list[NovelRegistryEntry]:
    """列出所有已注册的小说。"""
    registry = load_registry()
    entries = []
    for entry_dict in registry.values():
        entry = NovelRegistryEntry.from_dict(entry_dict)
        # 检查文件是否还存在
        if os.path.exists(entry.novel_path):
            entries.append(entry)
    entries.sort(key=lambda e: e.last_accessed or "", reverse=True)
    return entries


def update_novel_access(novel_path: str):
    """更新小说的最后访问时间。"""
    registry = load_registry()
    encoded = _encode_key(novel_path)
    if encoded in registry:
        registry[encoded]["last_accessed"] = datetime.now().isoformat()
        save_registry(registry)


def update_novel_style(novel_path: str, style: str):
    """更新小说的风格。"""
    registry = load_registry()
    encoded = _encode_key(novel_path)
    if encoded in registry:
        registry[encoded]["style"] = style
        save_registry(registry)


def update_novel_chapters(novel_path: str, total_chapters: int):
    """更新小说的章节数。"""
    registry = load_registry()
    encoded = _encode_key(novel_path)
    if encoded in registry:
        registry[encoded]["total_chapters"] = total_chapters
        save_registry(registry)


def _encode_key(path: str) -> str:
    """将文件路径编码为注册表 key（避免文件系统特殊字符问题）。"""
    return hashlib.md5(os.path.abspath(path).encode()).hexdigest()
