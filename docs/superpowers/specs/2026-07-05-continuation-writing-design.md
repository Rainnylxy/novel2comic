# 续写系统设计

> 日期: 2026-07-05  
> 状态: 待审批  
> 目标: 将项目从 roleplay（角色扮演）模式改为 AI 自主续写模式

---

## 1. 用户故事

用户加载小说（如《破云》），AI 从最后一章结尾开始流式续写故事内容。前端以"聊天小说"（Chat Fiction）形式展示——角色对话以聊天气泡呈现、旁白以居中卡片呈现、动作以附属小字呈现。用户可随时通过自然语言指令介入调整续写方向。

---

## 2. 核心需求

| 编号 | 需求 | 优先级 |
|------|------|--------|
| R1 | AI 自主续写，从小说最后一章结尾开始 | P0 |
| R2 | 流式输出，逐 fragment 推送至前端（SSE） | P0 |
| R3 | 用户可随时输入自然语言指令调整方向 | P0 |
| R4 | 前端以"聊天小说"形式展示（对话气泡 + 旁白卡片） | P0 |
| R5 | 多 Agent 协作（大纲 → 写作 → 审校 → 修订） | P1 |
| R6 | 作者文风蒸馏 + 注入 Writer，保证风格一致 | P1 |
| R7 | 基于 KG 的一致性校验，防止角色 OOC / 时间线冲突 | P1 |
| R8 | 续写完成后更新 KG（新事件、新关系） | P2 |

---

## 3. 不适用 / 需移除的现有模块

以下角色扮演相关模块不适用于续写，保留不动但不参与新流程：

- `src/interactive/story_state.py` — 亲密度/旗标/抉择状态
- `src/interactive/choice_engine.py` — 玩家选项生成
- `src/interactive/npc_manager.py` — NPC Agent 池
- `src/interactive/director_agent.py` — 互动游戏导演
- `src/agents/roleplay_agent.py` — 角色扮演心智引擎
- `frontend/index.html` — 需重写为续写模式

---

## 4. 复用清单

| 现有模块 | 续写系统中的用法 |
|----------|-----------------|
| `src/knowledge_graph.py` | 所有 Agent 查询角色/事件/关系/伏笔 |
| `src/chapter_parser.py` | 加载小说章节 |
| `src/models.py` | Novel, StoryGraph, Person 等数据模型 |
| `src/llm.py` | UnifiedLLM（Writer 额外走原生 streaming API） |
| `src/character_distiller.py` | Voice/Boundary/Policy Anchors → Writer 角色行为约束 |
| `src/context.py` | GlobalContext + ServiceRegistry |
| `src/services/kg_service.py` | KG 查询服务 |
| `src/services/project_service.py` | 项目存储/加载 |
| `src/agent_memory.py` | Agent 记忆系统 |
| `src/agents/base_agent.py` | Agent 基类（Plot Architect / Reviewer / Editor 继承） |
| `src/scene_engine.py` | 场景上下文构建 |
| `src/server/session_manager.py` | 参考其 session 管理模式 |

---

## 5. 新增文件

```
src/
├── continuation/                    ← 新目录（与 interactive/ 平级）
│   ├── __init__.py
│   ├── author_style_distiller.py    ← 新增：作者文风蒸馏器
│   ├── author_style_profile.py      ← 新增：文风 Profile 数据模型
│   ├── plot_architect.py            ← ① Plot Architect Agent
│   ├── chapter_writer.py            ← ② Chapter Writer（流式核心）
│   ├── consistency_reviewer.py      ← ③ 一致性审校 Agent
│   ├── revision_editor.py           ← ④ 修订编辑 Agent
│   ├── pipeline.py                  ← 流水线编排器
│   └── fragment.py                  ← Story Fragment 数据模型

├── server/
│   └── write_handlers.py            ← 新增：续写 REST + SSE 端点

frontend/
└── index.html                       ← 重写：续写模式 UI
```

---

## 6. 架构总览

```
┌──────────────────────────────────────────────────────────────────────┐
│                         前端 (续写模式)                                 │
│                                                                        │
│   ┌────────────────────────────────────────────────────────────┐    │
│   │  聊天小说阅读器                                              │    │
│   │  · 角色气泡 (dialogue)                                       │    │
│   │  · 旁白卡片 (narration)                                      │    │
│   │  · 动作小字 (action)                                         │    │
│   │  · 内心独白虚线框 (inner_thought)                             │    │
│   │  · 场景分隔线 (divider)                                      │    │
│   │  · 底部指令输入栏 ─── POST /api/write/inject                  │    │
│   │  · "开始续写" 按钮 ─── POST /api/write/start                  │    │
│   └────────────────────────────────────────────────────────────┘    │
│                                                                        │
│   通信: SSE (fragment stream) + REST                                 │
└────────────────────────────────┬─────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         后端 API 层                                    │
│                                                                        │
│   POST /api/write/start  ── 启动续写 → 返回 SSE stream                 │
│   POST /api/write/inject ── 注入用户指令 → 中断流 → 重组 → 恢复        │
│   GET  /api/write/state  ── 查询当前续写状态                            │
│                                                                        │
│   技术: Tornado handler + SSE                                          │
└────────────────────────────────┬─────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│                  Continuation Pipeline (续写流水线)                    │
│                                                                        │
│                    加载小说                                            │
│                       │                                                │
│          ┌────────────┼────────────┐                                   │
│          ▼            ▼            ▼                                   │
│    Character    AuthorStyle     KG                                     │
│    Distiller    Distiller    Extraction                                │
│          │            │            │                                   │
│          └────────────┼────────────┘                                   │
│                       │                                                │
│                       ▼                                                │
│           ┌───────────────────────┐                                    │
│           │ ① Plot Architect      │  KG → 大纲 + 弧线                   │
│           │   剧情架构师           │                                    │
│           └───────────┬───────────┘                                    │
│                       │ outline                                        │
│                       ▼                                                │
│           ┌───────────────────────┐                                    │
│           │ ② Chapter Writer      │  streaming → 逐 fragment 输出       │
│           │   章节写手 (流式核心)   │  监听 inject 中断                   │
│           └───────────┬───────────┘                                    │
│                       │ draft                                          │
│                       ▼                                                │
│           ┌───────────────────────┐                                    │
│           │ ③ Consistency Reviewer│  KG 校验 → 问题列表                 │
│           │   一致性审校           │                                    │
│           └───────────┬───────────┘                                    │
│                       │ issues                                         │
│                       ▼                                                │
│           ┌───────────────────────┐                                    │
│           │ ④ Revision Editor     │  局部修改 → 终稿                    │
│           │   修订编辑             │                                    │
│           └───────────────────────┘                                    │
│                                                                        │
│  共享: KG Service | Char Distiller | Style Distiller | LLM | 章节文本  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 7. 各组件详细设计

### 7.1 Author Style Distiller（作者文风蒸馏器）— 新增

**定位**: 一次性分析、缓存复用。与 CharacterDistiller 平级但互补。

**文风指纹维度**:

```
AuthorStyleProfile:
  ① 句法特征 (Syntax)
     · 平均句长 / 句长分布 / 长短句交替模式
     · 惯用句式
     · 对话占比 vs 叙述占比

  ② 词汇指纹 (Lexicon)
     · 高频词 / 特色词汇
     · 禁用现代口语（古风/年代感约束）
     · 动作描写的习惯用词

  ③ 叙事惯用手法 (Narrative Patterns)
     · 视角切换频率
     · 心理描写密度
     · 章尾钩子的典型写法
     · 场景过渡方式

  ④ 氛围基调 (Atmosphere)
     · 情感倾向（冷峻/温暖/压抑）
     · 环境描写占比
     · 暴力/亲密的描写尺度

  ⑤ 写作范式样本 (Exemplars)
     · 3-5 段典型段落作为"风格锚点"
```

**实现**:

```python
class AuthorStyleDistiller:
    def distill(self, novel_text: str, sample_chapters: int = 10) -> AuthorStyleProfile:
        # 1. 均匀抽样 N 个章节（覆盖前中后期）
        # 2. 统计分析（句长、词频、对话占比）← 本地计算
        # 3. LLM 分析叙事模式和氛围基调 ← AI 分析
        # 4. 抽取 3-5 段典范段落作为 Exemplars
        # 5. 汇总为 AuthorStyleProfile → 随 novel.json 缓存
```

---

### 7.2 Fragment 数据模型

```python
from dataclasses import dataclass
from typing import Optional, Literal

FragmentType = Literal["dialogue", "narration", "action", "inner_thought", "divider"]

@dataclass
class StoryFragment:
    type: FragmentType
    text: str
    character: Optional[str] = None   # dialogue / action / inner_thought 时必填
    divider_label: Optional[str] = None  # divider 时可选（如 "三小时后"）
    
    def to_sse(self) -> str:
        """序列化为 SSE 事件。"""
        import json
        return json.dumps(self.to_dict(), ensure_ascii=False)
    
    def to_dict(self) -> dict:
        d = {"type": self.type, "text": self.text}
        if self.character:
            d["character"] = self.character
        if self.divider_label:
            d["divider_label"] = self.divider_label
        return d
```

**前端渲染映射**:

| type | 展示 |
|------|------|
| `dialogue` | 角色头像 + 聊天气泡 |
| `narration` | 居中灰色文字卡片 |
| `action` | 小字附加在角色名下 |
| `inner_thought` | 虚线边框气泡（区分对话） |
| `divider` | 水平分割线 + 可选标签 |

---

### 7.3 ① Plot Architect（剧情架构师）

**职责**: 基于 KG 上下文 + 上一章结尾 + 用户指令，生成下一章的故事大纲。

**继承**: `BaseAgent`（`SKILL_NAME = "plot_architect"`）

**输入**:
- 上一章原文结尾（~3000 字）
- KG 上下文：角色状态、未解决伏笔（因果链）、活跃冲突
- 用户初始指令（可选）
- AuthorStyleProfile（参考叙事节奏）
- 角色 Profile（Voice/Boundary）

**工具**:
- `analyze_hanging_threads()` — 从 KG 因果链中提取未解决伏笔
- `sketch_character_beats()` — 为每个主要角色规划本章情绪/行为节拍
- `plan_structure()` — 生成章节结构（起承转合 + 悬念钩子）

**输出**:
```json
{
  "chapter_number": 135,
  "title": "暗流",
  "synopsis": "江停在旧案卷宗中发现了一个被忽略的细节...",
  "character_beats": {
    "江停": {"arc": "从犹豫到决断", "key_action": "独自前往废弃仓库"},
    "严峫": {"arc": "察觉异常→追踪", "key_action": "发现江停留下的线索"}
  },
  "structure": {
    "opening": "场景锚定——刑侦支队深夜加班",
    "rising": "江停发现关键线索→内部冲突→决定独自行动",
    "climax": "废弃仓库对峙",
    "hook": "严峫赶到时，仓库已空，只留下一个熟悉的符号"
  },
  "plot_threads_advanced": ["黑桃K身份线"],
  "plot_threads_introduced": ["新线：神秘符号的来源"],
  "tone": "悬疑紧张，冷峻",
  "target_word_count": 3000
}
```

---

### 7.4 ② Chapter Writer（章节写手）— 流式核心

**职责**: 根据大纲生成章节内容，以 StoryFragment 为单位逐条流式输出。这是唯一不走 AgentFlow ReAct 循环的组件，直接调用 LLM 原生 streaming API。

**输入**:
- ① 的大纲
- KG 角色档案（Voice / Boundary / Policy Anchors）
- AuthorStyleProfile + Exemplars
- 前一章结尾原文
- 用户中途注入的指令

**System Prompt 构成**:

```
## 角色
你是专业小说续写者。

## 文风约束
{style_profile_summary}

## 风格参考段落
{exemplar_1}
{exemplar_2}
{exemplar_3}

## 角色行为约束
### 江停
- Voice: {voice_summary}
- Boundary: {hard_rules}
- Policy Anchors: {typical_behaviors}
### 严峫
- ...

## 当前大纲
{outline}

## 前一章结尾（上下文衔接）
{previous_chapter_ending}

## 输出格式
以 StoryFragment 的 JSON 序列逐行输出，每行一个完整的 fragment:
{"type": "narration", "text": "..."}
{"type": "dialogue", "character": "江停", "text": "..."}
...
严格每行一个 JSON，不要输出其他内容。
```

**流式控制**:
```
Writer 持有:
  - _stream: AsyncGenerator      ← LLM streaming 响应
  - _generated_fragments: list   ← 已生成但未 flush 的 fragments
  - _inject_signal: asyncio.Event ← 注入指令触发
  
run():
  1. 构建 prompt → 调用 LLM chat completions (stream=true)
  2. 逐行读取 → 解析 JSON → yield StoryFragment
  3. 每个 fragment 通过 SSE 推送到前端
  4. 收到 inject_signal → 
     a. abort 当前 LLM stream (close HTTP connection)
     b. 将指令 + 已生成文本注入 context
     c. 重新发起 streaming 请求（continue 模式）
     d. 继续 yield
  5. 正常结束 → 返回完整 draft 给 pipeline
```

**关键实现**: 不走 AgentFlow，直接用 `httpx` 异步流式请求 LLM API，解析每行 SSE 数据，匹配 JSON fragment 模式后 yield。片段不完整的行做缓冲拼接。

---

### 7.5 ③ Consistency Reviewer（一致性审校）

**职责**: 对照 KG 检查草稿中的角色一致性、时间线正确性、设定矛盾。

**继承**: `BaseAgent`（`SKILL_NAME = "consistency_reviewer"`）

**检查维度**:
1. **角色 OOC**: 对话是否符合 Voice / Boundary
2. **状态一致性**: 角色生死/位置与 KG 是否一致
3. **时间线**: 事件顺序是否与已有事件冲突
4. **关系一致性**: 关系亲密度变化是否合理
5. **设定矛盾**: 新建设定是否推翻已有设定
6. **风格一致性**: 是否偏离 AuthorStyleProfile（语气、节奏）

**工具**:
- `check_character_consistency(draft)` — 角色 OOC 检查
- `check_timeline(draft)` — 时间线校验
- `check_setting_consistency(draft)` — 设定矛盾检查
- `check_style_consistency(draft)` — 风格一致性检查

**输出**:
```json
{
  "issues": [
    {
      "type": "character_ooc",
      "severity": "medium",
      "location": "片段序号 12",
      "character": "江停",
      "description": "江停主动拥抱这个行为与他'情感表达克制'的 Voice 冲突",
      "suggestion": "改为'江停的手指微微动了动，最终没有抬起手'"
    }
  ],
  "overall_score": 7.5
}
```

---

### 7.6 ④ Revision Editor（修订编辑）

**职责**: 根据审校问题列表，对草稿做局部修订。

**继承**: `BaseAgent`（`SKILL_NAME = "revision_editor"`）

**原则**:
- 只修改有问题的 fragment，不重写整章
- 修订后保持叙事流畅
- 输出修订记录

**输入**: ②的完整草稿（fragment 列表）+ ③的问题列表

**输出**:
```json
{
  "revised_fragments": [...],
  "changes": [
    {"fragment_index": 12, "original": "...", "revised": "...", "reason": "..."}
  ]
}
```

---

### 7.7 Pipeline 编排器

**职责**: 串联 4 个 Agent，管理 SSE 事件总线。

```python
class ContinuationPipeline:
    def __init__(self, ctx, services, llm):
        self.architect = PlotArchitect(ctx, services, llm)
        self.writer = ChapterWriter(ctx, services, llm)
        self.reviewer = ConsistencyReviewer(ctx, services, llm)
        self.editor = RevisionEditor(ctx, services, llm)
        self._inject_queue = asyncio.Queue()  # 用户注入的指令
    
    async def run(self, instruction: str = "") -> AsyncGenerator[PipelineEvent, None]:
        # 1. Plot Architect 生成大纲
        yield PipelineEvent("phase", "planning")
        outline = await self.architect.run(instruction)
        yield PipelineEvent("outline", outline)
        
        # 2. Chapter Writer 流式生成
        yield PipelineEvent("phase", "writing")
        draft = []
        async for fragment in self.writer.stream(outline):
            draft.append(fragment)
            yield PipelineEvent("fragment", fragment)
        
        # 3. Consistency Reviewer 异步审校（不阻塞 yield）
        yield PipelineEvent("phase", "reviewing")
        issues = await self.reviewer.run(draft)
        yield PipelineEvent("review", issues)
        
        # 4. Revision Editor 修订
        if issues["issues"]:
            yield PipelineEvent("phase", "revising")
            revised = await self.editor.run(draft, issues)
            yield PipelineEvent("complete", revised)
        else:
            yield PipelineEvent("complete", {"fragments": draft})
    
    async def inject(self, instruction: str):
        """接收用户注入指令。"""
        await self.writer.inject(instruction)
```

---

## 8. API 设计

### POST /api/write/start

```json
// Request
{
  "novel_path": "novels/poyun.txt",
  "instruction": "让江停更主动一些"  // 可选
}

// Response: SSE Stream
event: phase
data: {"phase": "planning"}

event: outline
data: {"chapter_number": 135, "synopsis": "...", ...}

event: phase
data: {"phase": "writing"}

event: fragment
data: {"type": "narration", "text": "夜色如墨..."}

event: fragment
data: {"type": "dialogue", "character": "江停", "text": "..."}

...

event: phase
data: {"phase": "reviewing"}

event: review
data: {"issues": [...], "overall_score": 7.5}

event: phase
data: {"phase": "revising"}

event: complete
data: {"fragments": [...]}

event: done
```

### POST /api/write/inject

```json
// Request
{
  "instruction": "让江停更果断，不要那么多内心戏"
}

// Response: 200 OK（指令已入队，Writer 收到后流自动调整）
// 新的 fragment 继续从原有 SSE stream 推送
```

### GET /api/write/state

```json
// Response
{
  "phase": "writing",
  "chapter": 135,
  "fragment_count": 47,
  "novel_title": "poyun"
}
```

---

## 9. 前端设计要点

### 视图结构

```
┌────────────────────────────────────────┐
│  📖 破云 · 续写                   [⚙]  │  ← 顶部栏
├────────────────────────────────────────┤
│                                        │
│  ──── 第135章 ────                     │  ← divider
│                                        │
│  夜色如墨，建宁市刑侦支队大楼依然...      │  ← narration (居中灰字卡片)
│                                        │
│  [头像] 严峫                            │
│  ┌──────────────────────────┐         │
│  │ 江停，你看看这个。          │         │  ← dialogue 气泡
│  └──────────────────────────┘         │
│  将一份档案袋扔在桌上                    │  ← action 小字
│                                        │
│              [头像] 江停                │
│              ┌───────────────────┐    │
│              │ ……                │    │  ← dialogue 气泡
│              └───────────────────┘    │
│              ┆ 这个案子，和那年...  ┆    │  ← inner_thought 虚线框
│                                        │
│  ────                                 │  ← divider 场景转换
│                                        │
│  ...（继续流式展示新 fragment）           │
│                                        │
├────────────────────────────────────────┤
│  💬 输入指令...                    [发送]│  ← 底部输入栏
├────────────────────────────────────────┤
│  [开始续写] [暂停] [继续]               │  ← 控制按钮
└────────────────────────────────────────┘
```

### 交互逻辑

1. 用户点击"开始续写" → POST /api/write/start → 建立 SSE 连接
2. 流式到达的每个 `fragment` 事件 → 追加渲染到对话视图
3. 用户在底部输入指令 → POST /api/write/inject → Writer 调整 → 新的 fragment 继续推送
4. 点击"暂停" → 前端暂停渲染，但 SSE 连接保持
5. 收到 `complete` 事件 → 显示"审校完成"标记 + 修订对比（可折叠）

### 流式输出中的光标动画

当前正在输出的 fragment 显示打字机光标效果（blinking cursor）。前一个 fragment 的动画已结束。

---

## 10. 文件变更清单

### 新增
- `src/continuation/__init__.py`
- `src/continuation/author_style_distiller.py`
- `src/continuation/author_style_profile.py`
- `src/continuation/fragment.py`
- `src/continuation/plot_architect.py`
- `src/continuation/chapter_writer.py`
- `src/continuation/consistency_reviewer.py`
- `src/continuation/revision_editor.py`
- `src/continuation/pipeline.py`
- `src/server/write_handlers.py`
- `skills/plot_architect.md`
- `skills/consistency_reviewer.md`
- `skills/revision_editor.md`
- （Chapter Writer 不走 AgentFlow，不需要 skill 文件）

### 修改
- `src/server/__init__.py` — 注册新 handler
- `src/server/handlers.py` — 可保留（roleplay API 不受影响）
- `src/cli/cli.py` — 新增 `write` 子命令
- `frontend/index.html` — 重写为续写模式
- `main.py` — 可选：添加 `write` 入口

### 移除
- `skills/continuation.md` — 旧版单 Agent 续写 skill，被新多 Agent 体系替代

### 不变
- `src/knowledge_graph.py`
- `src/chapter_parser.py`
- `src/models.py`
- `src/llm.py`
- `src/context.py`
- `src/character_distiller.py`
- `src/agents/base_agent.py`
- `src/services/`
- `src/interactive/`（全部保留不动）

---

## 11. 测试策略

| 层级 | 测试内容 | 方式 |
|------|---------|------|
| 单元测试 | Fragment 序列化/反序列化 | pytest |
| 单元测试 | AuthorStyleDistiller 统计分析 | pytest |
| 单元测试 | Pipeline 编排逻辑 (mock LLM) | pytest + asyncio |
| 集成测试 | Chapter Writer + streaming 输出 | 手动 + 录屏 |
| 集成测试 | Inject 中断 → 恢复流 | 手动 |
| 集成测试 | 完整流水线: 大纲 → 写作 → 审校 → 修订 | 手动 |
| E2E | 前端 SSE 渲染 + inject 交互 | 手动 |
| 回归测试 | roleplay 模式仍可正常启动 | pytest |
