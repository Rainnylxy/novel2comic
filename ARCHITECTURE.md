# Novel2Comic 架构图

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              用户输入层                                       │
│                                                                             │
│   python main.py comic --novel novels/poyun.txt --chapter 3                 │
│   python main.py continue --novel novels/poyun.txt --from-chapter 50        │
│   python main.py roleplay --novel novels/poyun.txt --character 苏墨          │
│   python main.py recommend --novel novels/poyun.txt                         │
│   python main.py summarize --novel novels/poyun.txt --theme                 │
│   python main.py "自然语言输入"            ← 自动路由                         │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         CLI + Router 层                                      │
│                                                                             │
│  ┌──────────────┐    ┌─────────────────┐                                    │
│  │   cli.py     │───▶│  IntentRouter   │                                    │
│  │  参数解析     │    │  关键词分类      │                                    │
│  │  子命令分发   │    │  置信度评估      │                                    │
│  └──────────────┘    │  自动路由 ───────┼─── comic/continue/roleplay/       │
│                      └─────────────────┘    recommend/summarize              │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               │
          ┌────────────────────┼────────────────────────┐
          ▼                    ▼                        ▼
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────────┐
│  Comic Agent    │  │ Continue Agent  │  │  RolePlay Agent     │
│  (漫画改编)      │  │ (续写)          │  │  (角色扮演)          │
├─────────────────┤  ├─────────────────┤  ├─────────────────────┤
│ 5 tools:        │  │ 4 tools:        │  │ 5 tools:            │
│ · analyze_text  │  │ · plan_arc      │  │ · start_conversation│
│ · design_chars  │  │ · write_draft   │  │ · respond           │
│ · extract_scenes│  │ · review_consis │  │ · switch_character  │
│ · storyboard    │  │ · revise_draft  │  │ · reflect_on_rel    │
│ · revise_scene  │  │                 │  │ · advance_scenario  │
├─────────────────┤  ├─────────────────┤  ├─────────────────────┤
│ 创意决策:        │  │ 创意决策:        │  │ 创意决策:            │
│ 砍什么留什么     │  │ 伏笔如何推进     │  │ "作为"角色而非"知道" │
│ 文字→画面翻译   │  │ 角色弧线发展     │  │ 情感状态动态演变     │
│ 节奏+景别分配   │  │ 节奏+悬念控制    │  │ 知识不对称过滤      │
└────────┬────────┘  └────────┬────────┘  └──────────┬──────────┘
         │                    │                       │
         └────────────────────┼───────────────────────┘
                              │
          ┌───────────────────┼───────────────────┐
          ▼                   ▼                   ▼
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│ Recommend Agent │  │ Summarize Agent │  │  (可扩展...)     │
│ (推荐)           │  │ (摘要)           │  │                 │
├─────────────────┤  ├─────────────────┤  │                 │
│ 3 tools:        │  │ 3 tools:        │  │                 │
│ · search_catalog│  │ · summarize_ch  │  │                 │
│ · explain_match │  │ · summarize_char│  │                 │
│ · compare_novels│  │ · analyze_theme │  │                 │
└────────┬────────┘  └────────┬────────┘  └─────────────────┘
         │                    │
         └────────────────────┘
                              │
                              │  所有 Agent 共享
                              ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                        ServiceRegistry (服务注册)                            │
│                                                                             │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐          │
│  │ KnowledgeGraph   │  │ ImageGeneration  │  │ ComicCompilation │          │
│  │ Service          │  │ Service          │  │ Service          │          │
│  ├──────────────────┤  ├──────────────────┤  ├──────────────────┤          │
│  │ · get_person()   │  │ · generate_panel │  │ · compile_scene  │          │
│  │ · get_relations()│  │ · generate_all   │  │ · draw_dialogue  │          │
│  │ · get_events()   │  │   _panels()      │  │ · compile_all    │          │
│  │ · short_path()   │  │ · placeholder    │  │ · font loading   │          │
│  │ · centrality()   │  │   fallback       │  │ · layout modes   │          │
│  │ · enemy_pairs()  │  │                  │  │                  │          │
│  │ · storyboard_    │  │                  │  │                  │          │
│  │   hints()        │  │                  │  │                  │          │
│  └──────────────────┘  └──────────────────┘  └──────────────────┘          │
│                                                                             │
│  ┌──────────────────┐  ┌──────────────────┐                                 │
│  │ ProjectService   │  │ SearchService    │                                 │
│  ├──────────────────┤  ├──────────────────┤                                 │
│  │ · save_novel()   │  │ · extract_kw()   │                                 │
│  │ · load_novel()   │  │ · search_ch()    │                                 │
│  │ · force_load()   │  │ · STOP_WORDS     │                                 │
│  │ · read_text_file │  │                  │                                 │
│  │ · registry CRUD  │  │                  │                                 │
│  └──────────────────┘  └──────────────────┘                                 │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         SharedContext (共享状态)                             │
│                                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐   │
│  │ novel        │  │ chapter_data │  │ agent_llm    │  │ sync_openai  │   │
│  │ (全书数据)    │  │ (当前章)     │  │ (Agent异步)  │  │ (Tool同步)   │   │
│  └──────┬───────┘  └──────────────┘  └──────────────┘  └──────────────┘   │
│         │                                                                   │
│         │  Novel 持有 StoryGraph (知识图谱)                                  │
│         ▼                                                                   │
│  ┌─────────────────────────────────────────────────────────────┐           │
│  │                     StoryGraph (V2)                          │           │
│  │                     networkx.MultiDiGraph                    │           │
│  │                                                              │           │
│  │   6 种节点                   9 种边                          │           │
│  │   ┌──────────┐              ┌──────────────┐                │           │
│  │   │ Person   │              │ Relationship │ (人物→人物)     │           │
│  │   │ Event    │              │ AppearsIn    │ (人物→章节)     │           │
│  │   │ Location │              │ Participates │ (人物→事件)     │           │
│  │   │ Org      │              │ OccursIn     │ (事件→章节)     │           │
│  │   │ Item     │              │ LocatedAt    │ (事件→地点)     │           │
│  │   │ Chapter  │              │ BelongsTo    │ (人物→组织)     │           │
│  │   └──────────┘              │ Owns         │ (人物→物品)     │           │
│  │                             │ EventRelation│ (事件→事件)     │           │
│  │                             │ LocHierarchy │ (地点→地点)     │           │
│  │                             └──────────────┘                │           │
│  │                                                              │           │
│  │   算法: shortest_path / centrality / faction / enemy_pairs   │           │
│  │         storyboard_hints / event_timeline / ask_plot         │           │
│  └─────────────────────────────────────────────────────────────┘           │
└─────────────────────────────────────────────────────────────────────────────┘
                               │
                               ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         基础设施层 (不变)                                     │
│                                                                             │
│  ┌────────────┐ ┌──────────────┐ ┌────────────┐ ┌──────────────┐          │
│  │ models.py  │ │ knowledge_   │ │ chapter_   │ │ novel_       │          │
│  │ 数据模型    │ │ graph.py     │ │ parser.py  │ │ registry.py  │          │
│  │ 1527 lines │ │ LLM提取+算法 │ │ 章节解析    │ │ SHA256注册表 │          │
│  └────────────┘ └──────────────┘ └────────────┘ └──────────────┘          │
│                                                                             │
│  ┌────────────┐ ┌──────────────┐ ┌────────────┐                           │
│  │ styles.py  │ │ img_adapter  │ │ llm.py      │                           │
│  │ 3种漫画风格│ │ .py          │ │ UnifiedLLM  │                           │
│  │ 自动检测    │ │ 图片生成适配 │ │ JSON+token  │                           │
│  └────────────┘ └──────────────┘ └────────────┘                           │
└─────────────────────────────────────────────────────────────────────────────┘


                          ┌─────────────────┐
                          │    外部服务       │
                          │                 │
                          │  ┌───────────┐  │
                          │  │ LLM API   │  │  (DeepSeek / OpenAI)
                          │  │ async 循环 │  │
                          │  └───────────┘  │
                          │  ┌───────────┐  │
                          │  │ LLM API   │  │  (工具内部同步调用)
                          │  │ sync 调用  │  │
                          │  └───────────┘  │
                          │  ┌───────────┐  │
                          │  │ Image API │  │  (Stability AI / DALL-E)
                          │  └───────────┘  │
                          └─────────────────┘
```

---

## 数据流

```
用户输入 "生成第3章"
    │
    ▼
CLI 解析 → IntentRouter.classify("生成第3章") → Intent.COMIC (100%)
    │
    ▼
ComicAdaptationAgent.run(task)
    │
    ├─ Agent 思考: "我需要先 analyze_text..."
    │
    ├─ [Tool] analyze_text(text)
    │   └─ LLM 调用 → 返回 style=gufeng, 3 characters
    │
    ├─ [Tool] design_characters()
    │   └─ LLM 调用 + KG 查询 → 创建 3 个 CharacterSheet
    │
    ├─ [Tool] extract_scenes()
    │   └─ LLM 调用 → 拆分出 5 个场景
    │
    ├─ [Tool] storyboard_scene(1)
    │   ├─ KG.get_storyboard_hints("苏墨", "老者") → 关系线索
    │   └─ LLM 调用 → 4 格分镜 + SD prompts
    │   ... (对每个场景重复)
    │
    ├─ [Tool] revise_scene(3, "画面太暗了")
    │   └─ LLM 调用 → 修改后重新生成
    │
    └─ Agent 完成 → 返回摘要
        │
        ▼
    execute_pipeline() 自动触发
        │
        ├─ ImageService.generate_all_panels() → 调用 Stability AI
        ├─ ComicService.compile_all() → PIL 拼接漫画页面
        └─ ProjectService.save_project() → 保存 JSON
```

---

## Agent 工具对比

```
┌──────────────────────────────────────────────────────────────────┐
│                      旧架构 (agent.py)                           │
│                                                                  │
│  1 个 Agent ─── 19 个工具                                        │
│                                                                  │
│  ████████████████████████████████████████  30 步迭代上限          │
│  ████████████ 查询工具 (7个, 本应是服务)                          │
│  ████████████ 管线工具 (8个, 混在一起)                            │
│  ██████ 管理工具 (4个)                                           │
│                                                                  │
│  提示词: 逐步操作手册 ("第1步...第2步...")                        │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│                    新架构 (Multi-Agent)                          │
│                                                                  │
│  Comic Agent       Continue Agent    RolePlay Agent               │
│  ┌──────────┐      ┌──────────┐      ┌──────────┐               │
│  │ 5 tools  │      │ 4 tools  │      │ 5 tools  │               │
│  │ ████     │      │ ███      │      │ ████     │               │
│  └──────────┘      └──────────┘      └──────────┘               │
│                                                                  │
│  Recommend Agent   Summarize Agent                               │
│  ┌──────────┐      ┌──────────┐                                  │
│  │ 3 tools  │      │ 3 tools  │                                  │
│  │ ██       │      │ ██       │                                  │
│  └──────────┘      └──────────┘                                  │
│                                                                  │
│  每个 Agent 专注一个领域，3-5 个工具，15 步上限                     │
│  提示词: 创意目标 ("将第3章改编为漫画，保持苍凉基调")              │
│  KG 查询 → 服务调用，不是 Agent 工具                              │
│  图片生成/排版/保存 → 自动触发，不是 Agent 工具                     │
└──────────────────────────────────────────────────────────────────┘
```

---

## 文件分层视图

```
novel2comic/
│
├── main.py                          ◀── 入口层
│
├── src/
│   ├── cli/cli.py                   ◀── CLI + 分发层
│   ├── router/router.py             ◀── 意图路由层
│   │
│   ├── agents/                      ◀── Agent 层 (导演)
│   │   ├── base_agent.py                · 抽象基类
│   │   ├── comic_agent.py               · 漫画改编 (5 tools)
│   │   ├── continuation_agent.py        · 续写 (4 tools)
│   │   ├── roleplay_agent.py            · 角色扮演 (5 tools)
│   │   ├── recommendation_agent.py      · 推荐 (3 tools)
│   │   └── summarization_agent.py       · 摘要 (3 tools)
│   │
│   ├── services/                    ◀── 执行层 (剧组)
│   │   ├── kg_service.py                · 知识图谱
│   │   ├── image_service.py             · 图片生成
│   │   ├── comic_service.py             · 漫画排版
│   │   ├── project_service.py           · 项目管理
│   │   └── search_service.py            · 章节搜索
│   │
│   ├── context.py                   ◀── 共享状态
│   ├── llm.py                       ◀── LLM 封装
│   ├── roleplay_session.py          ◀── 角色扮演状态
│   │
│   ├── models.py                    ◀── 数据模型 (不变)
│   ├── knowledge_graph.py           ◀── KG 核心 (不变)
│   ├── chapter_parser.py            ◀── 章节解析 (不变)
│   ├── novel_registry.py            ◀── 注册表 (不变)
│   ├── styles.py                    ◀── 风格系统 (不变)
│   └── img_adapter.py               ◀── 图片适配 (不变)
│
├── skills/                          ◀── Agent 角色定义
│   ├── comic_adaptation.md
│   ├── continuation.md
│   ├── roleplay.md
│   ├── recommendation.md
│   └── summarization.md
│
└── tests/                           ◀── 测试
    └── test_pipeline.py
```
