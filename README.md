# Novel2Comic — 小说→漫画智能生成系统

> v0.2 | ~5200 行 Python | 10 个源文件 | 9 个集成测试

## 功能概述

输入一本小说（.txt），Agent 自动完成：
1. 解析章节（支持"第X章"/Chapter X/数字顿号等格式）
2. 提取故事知识图谱（人物/事件/地点/组织/物品 + 9种关系边）
3. 生成漫画分镜脚本 + SD 生图 prompt + 排版输出

用户通过**自然语言对话**与 Agent 交互——可以说"生成第3章""苏墨的眼神不够冷""将军是谁？"。

## 快速开始

### 环境准备

```bash
# 1. 安装 agentflow（依赖框架）
cd /path/to/AgentFlow
pip install -e .

# 2. 安装 novel2comic
cd /path/to/novel2comic
pip install -e .

# 或者开发阶段用 PYTHONPATH
export PYTHONPATH=/path/to/AgentFlow:$PYTHONPATH
```

### 环境变量

```bash
export AGENTFLOW_API_KEY=sk-your-key        # LLM API key（必填）
export AGENTFLOW_BASE_URL=https://api.deepseek.com/v1
export AGENTFLOW_MODEL=deepseek-chat
export N2C_IMG_API_KEY=sk-your-key          # 生图 API key（可选，无则用占位图）
export N2C_PROXY=                           # HTTP 代理（可选）
```

### 运行

```bash
# 全书模式
python agent.py --novel 斗破苍穹.txt

# 单章模式
python agent.py chapter1.txt 月下初遇
```

## 文件结构

```
novel2comic/
│
├── agent.py                    # [1608行] 主入口：14个 @tool + AgentBuilder + 对话循环
├── pyproject.toml              # 包配置
├── requirements.txt
├── skills/
│   └── novel2comic.md          # Agent Skill 定义（风格约束 + 工作流程）
│
├── src/
│   ├── models.py               # [1527行] 全部数据模型
│   │   │                       #   - StyleProfile, CharacterSheet, Panel, Scene
│   │   │                       #   - Novel, ChapterData, ChapterInfo
│   │   │                       #   - CharacterGraph (V1, 人物+关系)
│   │   │                       #   - StoryGraph (V2, 6类节点+9类边)
│   │   │                       #   - EventNode, LocationNode, OrganizationNode, ItemNode
│   │   │                       #   - 9 种边类型 (BelongsTo, Owns, Participates...)
│   │   │                       #   - JSON 序列化 via to_dict/from_dict
│   │   │
│   ├── knowledge_graph.py      # [928行] 图谱提取引擎
│   │   │                       #   - FULL_EXTRACTION_PROMPT_V2: 4KB LLM prompt
│   │   │                       #   - extract_story_graph_from_text(): 全书一次提取
│   │   │                       #   - update_story_graph_with_chapter(): 增量更新
│   │   │                       #   - extract_graph_from_text(): V1 向后兼容
│   │   │                       #   - graph_to_context(): 图谱→LLM 上下文
│   │   │
│   ├── chapter_parser.py       # [155行] 章节解析
│   │   │                       #   - 支持"第X章"/"Chapter X"/数字顿号
│   │   │                       #   - 中文数字→阿拉伯数字转换
│   │   │
│   ├── novel_registry.py       # [184行] 小说注册表
│   │   │                       #   - SHA256 文件指纹去重
│   │   │                       #   - 缓存命中 → 跳过重新解析
│   │   │                       #   - list_all_novels / find_novel / resume_novel
│   │   │
│   ├── styles.py               # [91行] 漫画风格系统
│   │   │                       #   - manga / webtoon / gufeng 三套参数
│   │   │                       #   - detect_style(): 从题材标签自动判断
│   │   │
│   └── img_adapter.py          # [111行] 图像生成适配器
│       │                       #   - 云端 API (DALL-E 兼容)
│       │                       #   - 占位图兜底（无 API key 时）
│       │                       #   - 中文 Windows 字体检测
│
├── tests/
│   └── test_pipeline.py        # [626行] 集成测试
│       │                       #   - MockLLM: 模拟 4 阶段 LLM 响应
│       │                       #   - 9 个测试覆盖全部 tool + 注册表 + 图谱算法
│
└── projects/                   # 用户项目存储（gitignore）
    ├── novel_registry.json     # 注册表
    └── {timestamp}/
        ├── novel.json          # 全书数据 + StoryGraph
        └── chapter_0003/       # 第3章输出
            ├── chapter_data.json
            ├── images/
            └── comics/
```

## 架构

```
用户输入
    │
    ▼
┌──────────────────────────────────────┐
│ AgentFlow Agent (REACT 模式)         │
│  Skill: novel2comic.md               │
│  Memory: Working + Episodic          │
│  Thinking: ReActStrategy             │
│                                      │
│  14 个 @tool 供 Agent 决策调用:      │
│  ┌──────────────────────────────┐    │
│  │ 全书管理: load_novel         │    │
│  │          list_novels         │    │
│  │          resume_novel        │    │
│  ├──────────────────────────────┤    │
│  │ 章节选择: list_chapters      │    │
│  │          select_chapter      │    │
│  ├──────────────────────────────┤    │
│  │ 故事图谱: query_graph        │    │
│  │          query_character_    │    │
│  │            relations         │    │
│  │          query_events        │    │
│  │ 情节问答: ask_plot           │    │
│  ├──────────────────────────────┤    │
│  │ 生成管线: analyze_text       │    │
│  │          design_characters   │    │
│  │          extract_scenes      │    │
│  │          storyboard_scene    │    │
│  │          generate_images     │    │
│  │          compile_comic       │    │
│  │          save_project        │    │
│  └──────────────────────────────┘    │
│                                      │
│  Agent 自主决定: 何时调哪个 Tool     │
│  用户自然语言: "继续第5章" → Agent   │
│  理解为 select_chapter(5)→管线      │
└──────────────────────────────────────┘
```

## 核心数据流

```
load_novel("小说.txt")
  │
  ├─→ _read_text_file()          编码检测 (UTF-8→GBK→...)
  ├─→ find_novel()               SHA256 注册表查缓存
  ├─→ parse_novel_chapters()     正则匹配章节 → ChapterInfo[]
  ├─→ extract_story_graph()      LLM 提取 6 类实体 → StoryGraph
  ├─→ Novel.save(novel.json)     持久化
  └─→ register_novel()           写入注册表

select_chapter(3)
  │
  ├─→ 继承全书角色库 (novel.characters)
  ├─→ 继承全书风格 (novel.style_profile)
  ├─→ update_story_graph()       增量更新图谱
  └─→ 创建 ChapterData → 数据总线就绪

管线执行 (Agent 按序调用):
  analyze_text → detect_style → AnalysisResult
  design_characters → 注入图谱 → CharacterSheet[]
  extract_scenes → Scene[]
  storyboard_scene(×N) → 注入分镜指导 → Panel[] (含 sd_prompt)
  generate_images → 云端/占位图
  compile_comic → 条漫拼接 + 对话框
  save_project → JSON 持久化
```

## StoryGraph — 知识图谱核心

基于 NetworkX MultiDiGraph 的异构知识图谱：

| 节点类型 | key 格式 | 示例 |
|----------|----------|------|
| person | `person:苏墨` | role_type, faction, importance, status |
| event | `event:三年之约` | event_type, chapter_start/end, cause, effect |
| location | `location:长安城` | location_type, parent, factions |
| org | `org:将军府` | org_type, leader, members, status |
| item | `item:锈剑` | item_type, grade, owner_history |
| chapter | `chapter:3` | index, title, summary |

| 边类型 | 连接 |
|--------|------|
| relationship | person ↔ person |
| participates | person → event |
| located_at | event → location |
| belongs_to | person → org |
| owns | person → item |
| event_relation | event → event (causes/before/after) |
| location_hierarchy | location → location (child/parent) |

## Agent 通信协议

所有 Tool 返回 JSON，格式统一：
```json
{"status": "ok", "message": "人类可读摘要", "...": "具体数据"}
{"error": "错误描述"}
```

Agent 通过 AgentFlow 的 `OpenAIClient` 对外通信，Tool 内部通过 `_ctx.openai_client`（同步 OpenAI SDK）做 LLM 调用。两套客户端独立：AgentFlow 客户端负责"思考"，内部客户端负责"执行"。

## 已知限制 & 后续开发方向

### 确定要做
- [ ] **Manga 格阵排版**: 当前只有条漫竖拼模式，日式漫画的多格排版未实现
- [ ] **角色定妆照生成**: `design_characters` 只生成文本描述和触发词，未生成 reference image
- [ ] **反馈记忆系统**: 设计文档 Section 7 已规划，三层记忆（项目/用户/风格）未实现
- [ ] **Graph 算法仪表盘**: 中心度/最短路径/阵营分析已有 API，缺少可视化

### 待讨论
- [ ] **Web UI**: 图片预览 + 并排对比 + 分镜编辑
- [ ] **采样策略可配置**: 当前硬编码采样 10 章、每章 1500 字
- [ ] **LLM Provider 热切换**: 当前需要重启改环境变量

### 已知坑
- **StoryGraph 未持久化**: `extract_graph_from_text` 返回的图谱写入了 novel.json，但 `select_chapter` 增量更新后未立即保存（只在调用 `save_project` 时存）
- **Windows GBK 终端**: emoji 打印会乱码，已全部替换为 `[OK]` 等 ASCII 标签
- **Python 3.9**: 不支持 `X | None` 类型语法，必须用 `Optional[X]`

## 测试

```bash
# 需要 PYTHONPATH 指向 agentflow 父目录
PYTHONPATH=/path/to/AgentFlow python tests/test_pipeline.py
```

9 个测试全覆盖：
- `test_style_detection` — 风格自动判断
- `test_agent_tools_end_to_end` — 7 个 Pipeline Tool 端到端（Mock LLM）
- `test_data_serialization` — JSON 序列化往返
- `test_chapter_parser` — 章节解析（第X章/Chapter X）
- `test_novel_model` — Novel 数据模型 + 角色去重
- `test_novel_agent_tools` — load/select/chapter/
- `test_novel_registry` — 注册表缓存命中/过期
- `test_load_novel_cache_hit` — 二次加载缓存命中
- `test_graph_algorithms` — NetworkX 图算法（最短路径/中心度/阵营）

## 依赖

```
agentflow>=0.1.0        # Agent 框架（需从本地安装）
openai>=1.0.0           # LLM API
Pillow>=10.0.0          # 漫画排版
networkx>=3.0           # 知识图谱
httpx>=0.27.0           # HTTP 代理
requests>=2.28.0        # 图片下载
```
