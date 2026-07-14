# Session恢复 + 章节分页 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现服务重启后自动恢复续写进度 + 续写完成后章节级分页浏览

**Architecture:** pipeline 端新增 `_restore_session()` 扫描已生成章节恢复状态；后端新增 2 个 REST API 提供章节列表和详情；前端新增分页控件，续写完成前保持 SSE 流式，完成后切换为分页浏览模式

**Tech Stack:** Python 3.11, Tornado, vanilla JS (SSE + fetch), 现有 `StoryFragment` / `PipelineEvent` 数据结构

## Global Constraints

- 仅浏览已生成的续写章节（不涉及原文章节）
- 分页控件仅在收到 SSE `done` 事件后显示
- 续写过程中保持现有流式渲染不变
- 不引入新依赖

---

### Task 1: Session 恢复 — `_restore_session()` 方法

**Files:**

- Modify: `src/pipeline/pipeline.py`

**Interfaces:**

- Produces: `ContinuationPipeline._restore_session(project_dir: str) -> None`
- Produces: `ContinuationPipeline._scan_generated_chapters(project_dir: str, original_count: int) -> list[int]`
- Produces: `ContinuationPipeline._load_chapter_full_from_disk(project_dir: str, chapter_number: int) -> dict`

- [ ] **Step 1: 新增 `_scan_generated_chapters` 和 `_load_chapter_full_from_disk` 辅助方法**

在 `ContinuationPipeline` 类中，`_save_chapter_full` 方法之后（约1070行附近）添加：

```python
@staticmethod
def _scan_generated_chapters(project_dir: str, original_count: int) -> list:
    """扫描 project_dir 中已生成的续写章节号列表。

    Args:
        project_dir: 项目输出目录
        original_count: 原文章节数，仅返回大于此数的章节号

    Returns:
        已排序的章节号列表，仅包含续写生成的章节
    """
    if not project_dir or not os.path.isdir(project_dir):
        return []
    chapters = []
    pattern = re.compile(r'^chapter_(\d+)\.json$')
    for fname in os.listdir(project_dir):
        m = pattern.match(fname)
        if m:
            ch_num = int(m.group(1))
            if ch_num > original_count:
                chapters.append(ch_num)
    chapters.sort()
    return chapters

@staticmethod
def _load_chapter_full_from_disk(project_dir: str, chapter_number: int) -> dict:
    """从磁盘加载单章完整数据（规划 + fragments + 审校）。

    Returns:
        章节数据 dict，文件不存在或解析失败返回空 dict
    """
    path = os.path.join(project_dir, f"chapter_{chapter_number:04d}.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}
```

- [ ] **Step 2: 新增 `_restore_session` 方法**

在上述两个方法之后添加：

```python
def _restore_session(self, project_dir: str):
    """扫描已生成章节，恢复续写进度。

    从 project_dir/chapter_XXXX.json 检测已生成的续写章节，
    恢复 _chapter、_previous_chapter_ending、_story_memory。

    Args:
        project_dir: 项目输出目录
    """
    if not project_dir:
        return

    original_count = self._chapter
    generated = self._scan_generated_chapters(project_dir, original_count)
    if not generated:
        return

    max_gen = max(generated)
    self._chapter = max_gen
    print(f"[Resume] 检测到 {len(generated)} 章已生成续写章节 "
          f"(第{min(generated)}-{max_gen}章)，从第{max_gen + 1}章继续")

    # 恢复最后一章结尾上下文（供下一章 Writer 衔接）
    last = self._load_chapter_full_from_disk(project_dir, max_gen)
    if last:
        fragments = last.get("fragments", [])
        if fragments:
            recent = fragments[-3:]
            self._previous_chapter_ending = "\n".join(
                (f"{f.get('character', '')}: " if f.get('character') else "")
                + f.get('text', '')
                for f in recent
            )
            print(f"[Resume] 从第{max_gen}章恢复结尾上下文 "
                  f"({len(self._previous_chapter_ending)} 字)")

    # 恢复 StoryMemory：章节规划历史 + 伏笔列表
    restored_plans = 0
    restored_threads = 0
    for ch_num in sorted(generated):
        ch_data = self._load_chapter_full_from_disk(project_dir, ch_num)
        if not ch_data:
            continue
        plan_summary = {
            "chapter_number": ch_data.get("chapter_number", ch_num),
            "title": ch_data.get("title", ""),
            "synopsis": ch_data.get("synopsis", ""),
            "characters_involved": (
                list(ch_data.get("character_beats", {}).keys())
                if isinstance(ch_data.get("character_beats"), dict) else []
            ),
            "key_events": [
                s.get("goal", "")
                for s in ch_data.get("sections", [])
            ],
            "plot_threads_introduced": ch_data.get("plot_threads_introduced", []),
        }
        self._story_memory.add_chapter_plan(plan_summary)
        restored_plans += 1

        for t in ch_data.get("plot_threads_introduced", []):
            self._story_memory.add_thread(t, ch_num)
            restored_threads += 1

    self._chapter_plan_history = self._story_memory.chapter_plans
    self._introduced_threads = self._story_memory.get_pending_threads()
    print(f"[Resume] StoryMemory 已恢复: {restored_plans} 个章节规划, "
          f"{restored_threads} 条伏笔")
```

- [ ] **Step 3: 在 `load_novel` 末尾调用**

找到 `load_novel` 方法中 Agent 初始化完成的位置（约250行 `print("完成")` 之后），添加：

```python
        # 7. Session 恢复：检测已生成章节并从断点继续
        self._restore_session(project_dir)
```

- [ ] **Step 4: 验证 Session 恢复逻辑**

启动一次续写生成 1-2 章后停止，重新启动服务，确认日志输出：

```
[Resume] 检测到 2 章已生成续写章节 (第51-52章)，从第53章继续
[Resume] 从第52章恢复结尾上下文 (xxx 字)
[Resume] StoryMemory 已恢复: 2 个章节规划, x 条伏笔
```

- [ ] **Step 5: Commit**

```bash
git add src/pipeline/pipeline.py
git commit -m "feat: add session resume — auto-detect generated chapters and restore pipeline state"
```

---

### Task 2: 章节列表 + 详情 API

**Files:**

- Modify: `src/server/write_handlers.py`
- Modify: `src/server/__init__.py`

**Interfaces:**

- Consumes: `_active_pipeline` (global, from existing module)
- Produces: `ChapterListHandler` — GET /api/write/chapters → `{chapters: [...], current_chapter: int, original_count: int}`
- Produces: `ChapterDetailHandler` — GET /api/write/chapter/(\d+) → 章节完整 JSON 或 404

- [ ] **Step 1: 新增 `ChapterListHandler`**

在 `write_handlers.py` 文件末尾（`WriteStateHandler` 之后）添加：

```python
class ChapterListHandler(tornado.web.RequestHandler):
    """GET /api/write/chapters — 返回已生成续写章节的索引列表。"""

    def set_default_headers(self):
        self.set_header("Access-Control-Allow-Origin", "*")
        self.set_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.set_header("Access-Control-Allow-Headers", "Content-Type")

    def options(self):
        self.set_status(204)
        self.finish()

    def get(self):
        if _active_pipeline is None:
            self.write({
                "chapters": [],
                "current_chapter": 0,
                "original_count": 0,
            })
            return

        project_dir = (_active_pipeline._ctx.novel.output_dir
                       if _active_pipeline._ctx.novel else "")
        original_count = (
            len(_active_pipeline._ctx.novel.chapters)
            if _active_pipeline._ctx.novel else 0
        )

        chapters = []
        from ..pipeline.pipeline import ContinuationPipeline
        generated = ContinuationPipeline._scan_generated_chapters(
            project_dir, original_count,
        )
        for ch_num in sorted(generated):
            ch_data = ContinuationPipeline._load_chapter_full_from_disk(
                project_dir, ch_num,
            )
            chapters.append({
                "chapter_number": ch_num,
                "title": ch_data.get("title", ""),
                "fragment_count": ch_data.get("fragment_count", 0),
            })

        self.write({
            "chapters": chapters,
            "current_chapter": _active_pipeline.chapter,
            "original_count": original_count,
        })
```

- [ ] **Step 2: 新增 `ChapterDetailHandler`**

在 `ChapterListHandler` 之后添加：

```python
class ChapterDetailHandler(tornado.web.RequestHandler):
    """GET /api/write/chapter/(\d+) — 返回单章完整数据。"""

    def set_default_headers(self):
        self.set_header("Access-Control-Allow-Origin", "*")
        self.set_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.set_header("Access-Control-Allow-Headers", "Content-Type")

    def options(self):
        self.set_status(204)
        self.finish()

    def get(self, chapter_number_str: str):
        chapter_number = int(chapter_number_str)

        if _active_pipeline is None:
            self.set_status(404)
            self.write({"error": "No active session"})
            return

        project_dir = (_active_pipeline._ctx.novel.output_dir
                       if _active_pipeline._ctx.novel else "")

        from ..pipeline.pipeline import ContinuationPipeline
        ch_data = ContinuationPipeline._load_chapter_full_from_disk(
            project_dir, chapter_number,
        )

        if not ch_data:
            self.set_status(404)
            self.write({"error": f"Chapter {chapter_number} not found"})
            return

        self.write({
            "chapter_number": ch_data.get("chapter_number", chapter_number),
            "title": ch_data.get("title", ""),
            "synopsis": ch_data.get("synopsis", ""),
            "fragments": ch_data.get("fragments", []),
            "review": ch_data.get("review", {}),
        })
```

- [ ] **Step 3: 注册路由**

在 `src/server/__init__.py` 中：

更新 import：

```python
from .write_handlers import (
    WriteStartHandler,
    WriteInjectHandler,
    WriteStateHandler,
    ChapterListHandler,
    ChapterDetailHandler,
)
```

在路由表中新增两行：

```python
(r"/api/write/chapters", ChapterListHandler),
(r"/api/write/chapter/(\d+)", ChapterDetailHandler),
```

同时更新 `start_server` 中的启动提示打印，新增两行 API 说明。

- [ ] **Step 4: 手动测试 API**

启动服务后：

```bash
# 测试章节列表
curl http://localhost:8000/api/write/chapters
# 预期: {"chapters": [...], "current_chapter": N, "original_count": N}

# 测试章节详情
curl http://localhost:8000/api/write/chapter/51
# 预期: {"chapter_number": 51, "title": "...", "fragments": [...], ...}
```

- [ ] **Step 5: Commit**

```bash
git add src/server/write_handlers.py src/server/__init__.py
git commit -m "feat: add chapter list and detail REST API endpoints for pagination"
```

---

### Task 3: 前端章节分页

**Files:**

- Modify: `frontend/index.html`

**Interfaces:**

- Consumes: `GET /api/write/chapters` → 章节列表
- Consumes: `GET /api/write/chapter/{n}` → 单章详情

- [ ] **Step 1: 新增分页控件 CSS 样式**

在 `</style>` 之前添加以下样式：

```css
/* ===== 分页控件 ===== */
#pagination-bar {
  display: none;
  align-items: center;
  justify-content: center;
  gap: 0.8rem;
  padding: 0.8rem 1rem;
  background: var(--bg-panel);
  border-bottom: 1px solid var(--border);
  position: sticky;
  top: 0;
  z-index: 5;
}
#pagination-bar button {
  padding: 0.3rem 0.8rem;
  background: var(--bg-input);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 6px;
  cursor: pointer;
  font-family: inherit;
  font-size: 0.9rem;
  transition: border-color 0.2s;
}
#pagination-bar button:hover:not(:disabled) {
  border-color: var(--accent);
}
#pagination-bar button:disabled {
  opacity: 0.3;
  cursor: not-allowed;
}
#chapter-indicator {
  color: var(--accent);
  font-size: 0.95rem;
  min-width: 200px;
  text-align: center;
}
#chapter-select {
  padding: 0.3rem 0.5rem;
  background: var(--bg-input);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 6px;
  font-family: inherit;
  font-size: 0.85rem;
  cursor: pointer;
}
#chapter-select:focus {
  outline: none;
  border-color: var(--accent);
}
```

- [ ] **Step 2: 新增分页控件 DOM**

在 `#reader-screen` 内，`#fragment-container` 之前添加：

```html
<!-- 分页控件 -->
<div id="pagination-bar">
  <button id="prev-chapter-btn" disabled>&#9664; 上一章</button>
  <span id="chapter-indicator">-</span>
  <select id="chapter-select"></select>
  <button id="next-chapter-btn" disabled>下一章 &#9654;</button>
</div>
```

- [ ] **Step 3: 新增 JS 变量和分页逻辑函数**

在 `var fragmentCount = 0;` 之后添加分页相关变量：

```javascript
// 分页状态
let chapterList = []; // 从 API 加载的章节索引
let currentViewChapter = 0; // 当前正在查看的章节号
let paginationActive = false; // 是否已进入分页模式
let isStreaming = false; // 是否正在流式接收
```

在 `scrollToBottom` 函数后面添加分页逻辑函数：

```javascript
// ============================================================
// 分页逻辑
// ============================================================
async function loadChapterList() {
  try {
    var resp = await fetch(API_BASE + "/api/write/chapters");
    if (!resp.ok) return [];
    var data = await resp.json();
    chapterList = data.chapters || [];
    return chapterList;
  } catch (_) {
    return [];
  }
}

async function loadChapter(chapterNumber) {
  try {
    var resp = await fetch(API_BASE + "/api/write/chapter/" + chapterNumber);
    if (!resp.ok) return null;
    return await resp.json();
  } catch (_) {
    return null;
  }
}

function renderChapterFragments(data) {
  // 清空容器（保留分页控件）
  var bar = document.getElementById("pagination-bar");
  fragContainer.innerHTML = "";
  fragContainer.appendChild(bar);
  fragContainer.appendChild(
    document.getElementById("fragment-placeholder") || null,
  );

  var fragments = data.fragments || [];
  fragmentCount = 0;
  fragments.forEach(function (frag) {
    appendFragment(frag);
  });
  currentViewChapter = data.chapter_number;
  updatePaginationUI();
  fragContainer.scrollTop = 0;
}

function updatePaginationUI() {
  var indicator = document.getElementById("chapter-indicator");
  var prevBtn = document.getElementById("prev-chapter-btn");
  var nextBtn = document.getElementById("next-chapter-btn");
  var select = document.getElementById("chapter-select");

  if (chapterList.length === 0) {
    indicator.textContent = "-";
    prevBtn.disabled = true;
    nextBtn.disabled = true;
    return;
  }

  var total = chapterList.length;
  var currentChNum =
    currentViewChapter || chapterList[total - 1].chapter_number;
  var currentItem = chapterList.find(function (c) {
    return c.chapter_number === currentChNum;
  });
  var title = currentItem ? currentItem.title : "";

  indicator.textContent =
    "第 " +
    currentChNum +
    " / " +
    chapterList[total - 1].chapter_number +
    " 章「" +
    title +
    "」";

  // 更新下拉选择器（首次加载）
  if (select.options.length === 0) {
    chapterList.forEach(function (ch) {
      var opt = document.createElement("option");
      opt.value = ch.chapter_number;
      opt.textContent = "第" + ch.chapter_number + "章 · " + ch.title;
      select.appendChild(opt);
    });
  }
  select.value = currentChNum;

  // 边界按钮状态
  var currentIdx = chapterList.findIndex(function (c) {
    return c.chapter_number === currentChNum;
  });
  prevBtn.disabled = currentIdx <= 0;
  nextBtn.disabled = currentIdx >= chapterList.length - 1;
}

async function goToPrevChapter() {
  var currentIdx = chapterList.findIndex(function (c) {
    return c.chapter_number === currentViewChapter;
  });
  if (currentIdx <= 0) return;
  var prev = chapterList[currentIdx - 1];
  var data = await loadChapter(prev.chapter_number);
  if (data) renderChapterFragments(data);
}

async function goToNextChapter() {
  var currentIdx = chapterList.findIndex(function (c) {
    return c.chapter_number === currentViewChapter;
  });
  if (currentIdx < 0 || currentIdx >= chapterList.length - 1) return;
  var next = chapterList[currentIdx + 1];
  var data = await loadChapter(next.chapter_number);
  if (data) renderChapterFragments(data);
}

async function jumpToChapter(chapterNumber) {
  var data = await loadChapter(chapterNumber);
  if (data) renderChapterFragments(data);
}

function showPaginationBar() {
  document.getElementById("pagination-bar").style.display = "flex";
  paginationActive = true;
}

async function activatePagination() {
  await loadChapterList();
  if (chapterList.length > 0) {
    showPaginationBar();
    // 默认跳到最后一章（最新生成）
    var lastCh = chapterList[chapterList.length - 1];
    currentViewChapter = lastCh.chapter_number;
    updatePaginationUI();
  }
}
```

- [ ] **Step 4: 修改 SSE 事件处理，接入分页状态**

在 `handleSSEEvent` 函数中，修改 `phase` 事件处理，标记流式状态：

```javascript
case "phase":
  isStreaming = true;
  // ... 保留现有逻辑 ...
  break;
```

在 `done` 事件处理末尾，触发分页模式：

```javascript
case "done":
  isStreaming = false;
  // ... 保留现有逻辑 ...
  removeTypingCursor();
  activatePagination();
  break;
```

**注意**：`scrollToBottom` 函数也需要检查分页状态——分页模式下不应自动滚动：

```javascript
function scrollToBottom() {
  if (!paginationActive) {
    fragContainer.scrollTop = fragContainer.scrollHeight;
  }
}
```

- [ ] **Step 5: 绑定分页控件事件**

在现有事件绑定区域（`startForm.addEventListener("submit", startWriting);` 附近）添加：

```javascript
// 分页控件事件
document
  .getElementById("prev-chapter-btn")
  .addEventListener("click", goToPrevChapter);
document
  .getElementById("next-chapter-btn")
  .addEventListener("click", goToNextChapter);
document
  .getElementById("chapter-select")
  .addEventListener("change", function () {
    jumpToChapter(parseInt(this.value));
  });
```

- [ ] **Step 6: 手动测试分页功能**

1. 启动服务，开始续写
2. 观察续写过程中分页控件保持隐藏
3. 续写完成（done 事件）后，分页控件出现
4. 点击上一章/下一章按钮，确认能正确加载和渲染
5. 用下拉选择器跳转到指定章节
6. 边界章节时按钮应置灰

- [ ] **Step 7: Commit**

```bash
git add frontend/index.html
git commit -m "feat: add chapter-level pagination — prev/next navigation + chapter dropdown"
```
