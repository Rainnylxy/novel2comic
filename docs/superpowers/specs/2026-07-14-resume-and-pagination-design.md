# Session 恢复 + 章节分页 设计文档

日期: 2026-07-14
状态: 待实现

## 概述

两个功能：

1. **Session 恢复**：服务重启后自动检测已生成章节，从断点继续续写
2. **章节级分页**：续写完成后支持上一章/下一章翻页浏览

---

## 一、Session 恢复

### 现状

`load_novel()` 已完成 KG 缓存、roadmap 加载、status_fixes 恢复，但缺少"检测已生成章节并恢复进度"这一步。`_chapter` 始终等于原文章节数，重启后从原文末尾续写，覆盖已有生成章节。

### 设计

在 `load_novel()` 末尾（Agent 初始化之后）新增 `_restore_session()` 调用。

```
load_novel() 流程:
  1. 解析章节 → _chapter = original_count
  2. 加载 KG / Style / Char Profiles / StoryMemory
  3. 提取最后一章结尾 → _previous_chapter_ending
  4. 初始化 Agent
  5. [NEW] _restore_session()
```

**`_restore_session()` 逻辑**：

1. 扫描 `project_dir/chapter_XXXX.json`，提取数字章节号，排序
2. 若最大生成章节号 `max_gen > original_count`：
   - `_chapter = max_gen`
   - 加载 `chapter_{max_gen:04d}.json`，取 fragments 尾部 3 个拼接为 `_previous_chapter_ending`
   - 遍历已生成章节 JSON，重建 `_story_memory`（章节规划历史、伏笔列表）
   - `_roadmap_chapter_index` 已在前面从 `roadmap_index.json` 加载，无需额外处理
3. 日志：`[Resume] 检测到 X 章已生成章节，从第 Y 章继续`

### 改动文件

- `src/pipeline/pipeline.py`：新增 `_restore_session()` 方法，在 `load_novel()` 末尾调用

---

## 二、章节级分页

### 后端 API

新增两个端点，在 `src/server/write_handlers.py`：

**`GET /api/write/chapters`**

返回章节索引列表：

```json
{
  "chapters": [
    { "chapter_number": 51, "title": "暗流涌动", "fragment_count": 42 },
    { "chapter_number": 52, "title": "真相逼近", "fragment_count": 38 }
  ],
  "current_chapter": 52,
  "original_count": 50
}
```

- `chapters` 仅列出已生成的续写章节（chapter_number > original_count）
- 数据来源：扫描 `project_dir/chapter_XXXX.json`

**`GET /api/write/chapter/{n}`**

返回单章完整数据：

```json
{
  "chapter_number": 51,
  "title": "暗流涌动",
  "synopsis": "...",
  "fragments": [{"type": "narration", "text": "...", ...}, ...],
  "review": {"overall_score": 8.5, "changes": [...]}
}
```

- 数据来源：读取 `project_dir/chapter_{n:04d}.json`

两个 Handler 类：`ChapterListHandler` 和 `ChapterDetailHandler`。

### 路由注册

在 `src/server/__init__.py` 的路由表中新增：

- `(r"/api/write/chapters", ChapterListHandler)`
- `(r"/api/write/chapter/(\d+)", ChapterDetailHandler)`

### 前端

**状态机**：

- **续写中**：SSE 流式渲染，分页控件隐藏，自动滚动到底部
- **续写完成**（收到 `done` 事件）：显示分页控件，停止自动滚动

**分页控件**（在 `#fragment-container` 顶部）：

```
[◀ 上一章]    第 52 / 60 章「暗流涌动」 [▼]    [下一章 ▶]
```

- 上一章/下一章按钮：边界时置灰
- 章节下拉 `[▼]`：点击展开列表，可快速跳转任意章节
- 下拉列表首次打开时调 `GET /api/write/chapters` 获取，缓存到变量

**翻页流程**：

1. 调 `GET /api/write/chapter/{n}`
2. 清空容器（保留分页控件）
3. 遍历 fragments，调 `appendFragment()` 逐个渲染
4. 滚动到顶部

### 改动文件

- `src/server/write_handlers.py`：新增 2 个 Handler 类
- `src/server/__init__.py`：注册 2 个新路由
- `frontend/index.html`：新增分页控件 DOM、翻页逻辑、状态机切换

---

## 三、边界情况

| 场景                 | 处理                                                                          |
| -------------------- | ----------------------------------------------------------------------------- |
| 无已生成章节时启动   | `_restore_session()` 检测不到文件，跳过，从原文末尾正常续写                   |
| 已生成章节 JSON 损坏 | 跳过损坏文件，记录 warning，不影响恢复                                        |
| 翻页时章节不存在     | 返回 404，前端保持当前页不动                                                  |
| 续写结束后立即翻页   | 分页控件在 `done` 事件后显示，`complete` 事件后章节数据已落盘                 |
| 续写过程中翻看历史   | 可以（API 读的是磁盘上的已完成章节），但控件隐藏；若手动调 API，不影响 SSE 流 |

## 四、不涉及

- 原文章节的浏览（只浏览生成的续写章节）
- 章节编辑/删除
- 多 session 并发
