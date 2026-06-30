# -*- coding: utf-8 -*-
"""CLI 参数解析和分发。

将命令行参数解析为 Agent 任务，
构建 GlobalContext + ServiceRegistry，
路由到对应 Agent 执行。

支持模式：
- 显式子命令：python main.py comic --novel 小说.txt
- 交互模式：python main.py interactive --novel 小说.txt（支持 /roleplay 切换）
- 自动路由：python main.py "自然语言输入"
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from novel2comic.src.context import GlobalContext, ServiceRegistry
from novel2comic.src.llm import UnifiedLLM
from novel2comic.src.services import (
    KnowledgeGraphService,
    ImageGenerationService,
    ComicCompilationService,
    ProjectService,
    SearchService,
)
from novel2comic.src.services.project_service import ProjectService as PS


# ============================================================
# 基础设施：构建上下文 + 加载小说
# ============================================================

def _build_context_and_services(
    api_key: str, base_url: str, model: str, proxy: str,
    tool_model: str = "",
) -> tuple:
    """构建 GlobalContext 和 ServiceRegistry。"""
    import openai
    import httpx
    from agentflow.runtime.llm_client import OpenAIClient

    agent_llm = OpenAIClient(
        api_key=api_key, model=model,
        base_url=base_url, proxy=proxy or None,
    )

    http_client = httpx.Client(proxy=proxy) if proxy else None
    sync_openai = openai.OpenAI(
        api_key=api_key, base_url=base_url,
        http_client=http_client,
    )

    llm = UnifiedLLM(sync_openai, tool_model or model)

    ctx = GlobalContext(
        llm_model=model,
        agent_llm=agent_llm,
        sync_openai=sync_openai,
    )

    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    projects_dir = os.path.join(root_dir, "projects")

    project_svc = ProjectService(projects_dir)
    kg_svc = KnowledgeGraphService(llm=llm)

    img_api_key = os.getenv("N2C_IMG_API_KEY", "")
    img_base_url = os.getenv("N2C_IMG_BASE_URL", "")
    img_adapter = None
    if img_api_key:
        from novel2comic.src.img_adapter import ImageGenAdapter
        img_adapter = ImageGenAdapter(api_key=img_api_key, base_url=img_base_url)

    services = ServiceRegistry(
        kg=kg_svc,
        image=ImageGenerationService(img_adapter),
        comic=ComicCompilationService(),
        project=project_svc,
        search=SearchService(),
    )

    ctx.services = services
    return ctx, services, llm


def _load_novel(novel_path: str, services: ServiceRegistry, ctx: GlobalContext):
    """加载小说 + 提取知识图谱。所有 Agent 共享的初始化逻辑。"""
    from novel2comic.src.chapter_parser import parse_novel_chapters
    from novel2comic.src.models import Novel

    print(f"[Loading] 正在加载 {novel_path}...")
    text = PS.read_text_file(novel_path)
    base_name = os.path.splitext(os.path.basename(novel_path))[0]
    chapters = parse_novel_chapters(text, base_name)

    project_dir = services.project.create_project_dir(base_name)
    ctx.novel = Novel(
        title=base_name,
        file_path=os.path.abspath(novel_path),
        chapters=chapters,
        output_dir=project_dir,
    )

    print(f"[KG] 正在逐章提取知识图谱（{len(chapters)} 章）...")
    ctx.novel.story_graph = services.kg.extract_incremental(
        chapters,
        batch_size=int(os.getenv("KG_BATCH_SIZE", "10")),
    )
    services.project.save_novel(ctx.novel)
    graph = ctx.novel.story_graph
    print(f"[KG] 完成：{graph.total_node_count} 个节点，"
          f"{graph.total_edge_count} 条边")

    # 自动生成知识图谱可视化
    _auto_viz(ctx, base_name)

    return base_name, chapters, text


def _auto_viz(ctx: GlobalContext, base_name: str):
    """KG 提取完成后自动生成 DOT 图。"""
    graph = ctx.novel.story_graph if ctx.novel else None
    if not graph or graph.total_node_count == 0:
        return
    from novel2comic.src.services.graph_viz import KnowledgeGraphVisualizer
    viz = KnowledgeGraphVisualizer()
    output_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "outputs",
    )
    dot_path = os.path.join(output_dir, f"{base_name}_kg.dot")
    viz.render_dot(graph, dot_path)
    print(f"  → 浏览器查看: https://dreampuf.github.io/GraphvizOnline/")
    print(f"     (复制 {dot_path} 内容粘贴即可)")
    print(f"  → 或安装 Graphviz: winget install graphviz")
    print(f"     dot -Tpng {dot_path} -o {dot_path.replace('.dot', '.png')}")


def _require_api_key():
    """检查 API key 是否已设置。"""
    api_key = os.getenv("AGENTFLOW_API_KEY", "")
    if not api_key:
        print("[!] AGENTFLOW_API_KEY not set.")
        print("    请设置环境变量: $env:AGENTFLOW_API_KEY='sk-your-key'")
        sys.exit(1)
    return api_key


# ============================================================
# Agent 工厂
# ============================================================

def _create_agent(intent: str, ctx: GlobalContext, services: ServiceRegistry, llm: UnifiedLLM):
    """根据意图创建对应的 Agent 实例。"""
    if intent == "comic":
        from novel2comic.src.agents.comic_agent import ComicAdaptationAgent
        return ComicAdaptationAgent(ctx, services, llm)
    elif intent == "continue":
        from novel2comic.src.agents.continuation_agent import ContinuationAgent
        return ContinuationAgent(ctx, services, llm)
    elif intent == "roleplay":
        from novel2comic.src.agents.roleplay_agent import RolePlayAgent
        return RolePlayAgent(ctx, services, llm)
    elif intent == "recommend":
        from novel2comic.src.agents.recommendation_agent import RecommendationAgent
        return RecommendationAgent(ctx, services, llm)
    elif intent == "summarize":
        from novel2comic.src.agents.summarization_agent import SummarizationAgent
        return SummarizationAgent(ctx, services, llm)
    else:
        raise ValueError(f"Unknown intent: {intent}")


# ============================================================
# 交互模式 —— 支持运行时切换 Agent
# ============================================================

# 显式切换命令
SWITCH_COMMANDS = {
    "/comic": "comic",
    "/c": "comic",
    "/continue": "continue",
    "/cont": "continue",
    "/roleplay": "roleplay",
    "/rp": "roleplay",
    "/recommend": "recommend",
    "/rec": "recommend",
    "/summarize": "summarize",
    "/sum": "summarize",
}

HELP_TEXT = """
┌─────────────────────────────────────────────────┐
│  可用命令:                                       │
│  /comic, /c        切换到漫画改编 Agent           │
│  /continue, /cont  切换到续写 Agent              │
│  /roleplay <角色>, /rp <角色>  切换到角色扮演     │
│  /recommend, /rec  切换到推荐 Agent              │
│  /summarize, /sum  切换到摘要 Agent              │
│  /help             显示此帮助                    │
│  quit, exit, q     退出                         │
│                                                 │
│  也可以直接输入自然语言，Router 会自动判断意图     │
└─────────────────────────────────────────────────┘
"""


def _parse_switch(user_input: str) -> tuple[Optional[str], Optional[str]]:
    """解析切换命令。返回 (intent, extra_arg)。

    Examples:
        "/roleplay 苏墨" → ("roleplay", "苏墨")
        "/rp 江停"      → ("roleplay", "江停")
        "/comic"        → ("comic", None)
        "普通消息"       → (None, None)
    """
    user_input = user_input.strip()
    if not user_input.startswith("/"):
        return None, None

    parts = user_input.split(maxsplit=1)
    cmd = parts[0].lower()
    extra = parts[1] if len(parts) > 1 else None

    if cmd in SWITCH_COMMANDS:
        return SWITCH_COMMANDS[cmd], extra
    return None, None


async def _run_interactive_loop(
    agent,
    first_task: str,
    ctx: GlobalContext,
    services: ServiceRegistry,
    llm: UnifiedLLM,
    intent: str,
    extra: Optional[str] = None,
):
    """统一的交互循环，支持运行时切换 Agent。

    用户输入处理优先级：
    1. quit / exit / q → 退出
    2. /comic / /roleplay 等 → 切换 Agent
    3. 其他 → 交给当前 Agent 处理
    """
    label = _agent_label(intent, extra)
    print(f"\n[Agent] {label}")
    print("[Agent] 输入 /help 查看可用命令，quit 退出\n")

    # 首次任务
    if first_task:
        result = await agent.run(first_task)
        print(f"\n{result.output}\n")
        _auto_pipeline(agent, intent)
    else:
        # 无任务时（仅切换情况），提示当前 Agent 可用
        print(f"[Agent] 已切换到 {label}。请告诉我你想做什么。\n")

    while True:
        try:
            user_input = input("[You] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[Agent] 再见！")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("[Agent] 再见！")
            break
        if user_input.lower() in ("/help", "/h", "help"):
            print(HELP_TEXT)
            continue

        # 检查显式切换命令
        new_intent, extra_arg = _parse_switch(user_input)
        if new_intent:
            intent = new_intent
            extra = extra_arg
            agent = _create_agent(intent, ctx, services, llm)

            # 构建新 Agent 的初始任务
            task = _build_switch_task(intent, extra_arg, ctx)
            label = _agent_label(intent, extra_arg)
            print(f"\n[Switch] → {label}")

            if task:
                result = await agent.run(task)
                print(f"\n{result.output}\n")
                _auto_pipeline(agent, intent)
            else:
                print(f"[Agent] 请告诉我你想做什么。\n")
            continue

        # 检查自然语言是否表达了切换意图
        if _is_natural_switch(user_input):
            intent, new_extra = _detect_natural_switch(user_input)
            agent = _create_agent(intent, ctx, services, llm)
            task = _build_switch_task(intent, new_extra, ctx)
            label = _agent_label(intent, new_extra)
            print(f"\n[Router] → {label}")
            result = await agent.run(task)
            print(f"\n{result.output}\n")
            _auto_pipeline(agent, intent)
            continue

        # 正常交给当前 Agent
        result = await agent.run(user_input)
        print(f"\n{result.output}\n")
        _auto_pipeline(agent, intent)


def _agent_label(intent: str, extra: Optional[str] = None) -> str:
    """生成 Agent 的显示标签。"""
    labels = {
        "comic": "漫画改编模式",
        "continue": "续写模式",
        "roleplay": f"角色扮演模式 | 扮演: {extra or '未指定'}",
        "recommend": "推荐模式",
        "summarize": "摘要模式",
    }
    return labels.get(intent, intent)


def _build_switch_task(intent: str, extra: Optional[str], ctx: GlobalContext) -> Optional[str]:
    """为切换后的 Agent 构建初始任务。"""
    novel_name = ctx.novel.title if ctx.novel else "未加载"

    if intent == "roleplay" and extra:
        return (
            f"请调用 start_conversation('{extra}', '小说《{novel_name}》的世界') "
            f"以 {extra} 的身份开始对话。"
        )
    elif intent == "summarize":
        return f"请对《{novel_name}》进行摘要分析。你可以调用 summarize_chapter 或 analyze_theme。"
    elif intent == "recommend":
        return f"请基于《{novel_name}》的特征推荐类似作品。先调用 search_catalog，然后调用 explain_match。"
    elif intent == "continue":
        last_ch = ctx.novel.story_graph.last_updated_chapter if ctx.novel and ctx.novel.story_graph else 1
        return f"从第 {last_ch} 章结尾开始续写。请先调用 plan_arc 规划，然后调用 write_draft。"
    elif intent == "comic":
        if ctx.novel and ctx.novel.chapters:
            chs = ", ".join([f"第{ch.index}章" for ch in ctx.novel.chapters[:5]])
            return f"小说《{novel_name}》已加载。可用章节：{chs}... 请告诉我你想改编哪一章。"
        return None
    return None


def _is_natural_switch(user_input: str) -> bool:
    """检测自然语言是否表达了切换意图。"""
    switch_triggers = [
        "切换到", "换到", "帮我扮演", "你来扮演",
        "帮我续写", "帮我推荐", "帮我总结", "帮我分析",
        "生成漫画", "做成漫画", "改成漫画",
    ]
    return any(t in user_input for t in switch_triggers)


def _detect_natural_switch(user_input: str) -> tuple[str, Optional[str]]:
    """从自然语言中检测切换意图和参数。"""
    from novel2comic.src.router.router import IntentRouter

    intent, _ = IntentRouter.classify(user_input)

    extra = None
    if intent == "roleplay":
        # 尝试提取角色名
        for trigger in ["扮演", "当成", "假装你是", "你是"]:
            if trigger in user_input:
                rest = user_input.split(trigger, 1)[-1].strip()
                # 取前 4 个字作为角色名候选
                extra = rest[:4].rstrip("。，！？")
                break

    return intent.value, extra


def _auto_pipeline(agent, intent: str):
    """漫画 Agent 的自动管线触发。"""
    if intent == "comic":
        ctx = agent._ctx
        if ctx.chapter_data and ctx.chapter_data.scenes:
            # 检查是否有新生成的（未编译的）分镜
            has_new = any(
                p.status != "generated"
                for s in ctx.chapter_data.scenes
                for p in s.panels
            )
            if has_new:
                print("[Pipeline] 正在生成图片...")
                result = agent.execute_pipeline()
                print(f"[Pipeline] {result['message']}")


# ============================================================
# 单次命令模式（向后兼容）
# ============================================================

async def run_interactive(args):
    """交互模式：加载小说后进入统一交互循环，支持 /roleplay 切换。"""
    api_key = _require_api_key()
    base_url = os.getenv("AGENTFLOW_BASE_URL", "https://api.deepseek.com/v1")
    model = os.getenv("AGENTFLOW_MODEL", "deepseek-chat")
    tool_model = os.getenv("AGENTFLOW_TOOL_MODEL", model)  # 蒸馏/反思用
    proxy = os.getenv("AGENTFLOW_PROXY", "")

    ctx, services, llm = _build_context_and_services(api_key, base_url, model, proxy, tool_model)

    # 确定初始意图
    if args.command == "interactive":
        intent = args.mode or "comic"
    else:
        intent = args.command

    extra = None
    if intent == "roleplay":
        extra = getattr(args, "character", None)
    if intent == "roleplay" and not extra:
        # 如果是旧用法路由过来的，尝试从 title 获取角色名
        extra = getattr(args, "title", None)

    # 加载小说
    if getattr(args, "novel", None):
        _load_novel(args.novel, services, ctx)

    # 构建首次任务
    first_task = _build_first_task(args, intent, ctx)

    # 创建 Agent 并进入交互循环
    agent = _create_agent(intent, ctx, services, llm)
    await _run_interactive_loop(
        agent, first_task, ctx, services, llm, intent, extra,
    )


def _build_first_task(args, intent: str, ctx: GlobalContext) -> Optional[str]:
    """根据 CLI 参数构建首次任务。"""
    if intent == "comic":
        if hasattr(args, "novel") and args.novel:
            if hasattr(args, "chapter") and args.chapter:
                return (
                    f"小说已加载：{args.novel}\n"
                    f"请选择第 {args.chapter} 章，"
                    f"将其改编为漫画。按顺序执行 analyze_text → design_characters → extract_scenes → storyboard_scene。"
                    f"每步完成后简短汇报。"
                )
            else:
                return (
                    f"小说已加载：{args.novel}\n"
                    f"请列出所有章节，告诉我有哪些章节，等待我选择要改编哪一章。"
                )
        elif hasattr(args, "text") and args.text:
            title = getattr(args, "title", None) or "未命名章节"
            # 创建 ChapterData
            project_dir = ctx.services.project.create_project_dir(title)
            from novel2comic.src.models import ChapterData
            ctx.chapter_data = ChapterData(
                title=title,
                source_text=args.text,
                output_dir=project_dir,
                created_at=datetime.now().isoformat(),
            )
            return (
                f"章节标题：{title}\n\n"
                f"请将以下小说文本改编为漫画分镜：\n\n{args.text}\n\n"
                f"按顺序执行 analyze_text → design_characters → extract_scenes → storyboard_scene"
            )
        return None

    elif intent == "continue":
        from_ch = getattr(args, "from_chapter", 1)
        goal = getattr(args, "goal", "")
        if goal:
            return (
                f"从第 {from_ch} 章结尾开始续写。续写目标：{goal}\n"
                f"请先调用 plan_arc 规划叙事弧线，然后调用 write_draft 起草章节，"
                f"最后调用 review_consistency 检查一致性。"
            )
        else:
            return (
                f"从第 {from_ch} 章结尾开始续写。\n"
                f"请先分析现有剧情状态，告诉我你的续写构思，等我确认后再开始写。"
            )

    elif intent == "roleplay":
        char = getattr(args, "character", "主角")
        novel_name = ctx.novel.title if ctx.novel else "未知"
        return f"请调用 start_conversation('{char}', '小说《{novel_name}》的世界') 以 {char} 的身份开始对话。"

    elif intent == "recommend":
        prefs = getattr(args, "preferences", "")
        novel_name = ctx.novel.title if ctx.novel else "未知"
        if prefs:
            return f"请基于以下偏好推荐小说：{prefs}\n参考《{novel_name}》的特征，先调用 search_catalog，然后调用 explain_match。"
        else:
            return f"请分析《{novel_name}》的特征，推荐类似作品。先调用 search_catalog，然后调用 explain_match。"

    elif intent == "summarize":
        novel_name = ctx.novel.title if ctx.novel else "未知"
        if hasattr(args, "theme") and args.theme:
            return "请调用 analyze_theme 进行全书主题分析。"
        elif hasattr(args, "character") and args.character:
            return f"请调用 summarize_character('{args.character}') 分析该角色。"
        elif hasattr(args, "chapter") and args.chapter:
            persp = getattr(args, "perspective", "")
            if persp:
                return f"请调用 summarize_chapter({args.chapter}, '{persp}') 以 {persp} 的视角总结第 {args.chapter} 章。"
            else:
                return f"请调用 summarize_chapter({args.chapter}) 总结第 {args.chapter} 章。"
        else:
            return f"请对《{novel_name}》进行总结。你可以调用 summarize_chapter 获取各章节摘要，或调用 analyze_theme 进行主题分析。"

    return None


# ============================================================
# Viz 命令 —— 知识图谱可视化
# ============================================================

async def run_viz(args):
    """生成知识图谱交互式 HTML 可视化文件。

    优先从缓存（novel_registry）加载已有 KG，无需 API key。
    如果缓存不存在，需要 AGENTFLOW_API_KEY 来提取 KG。
    """
    from novel2comic.src.services.graph_viz import KnowledgeGraphVisualizer
    from novel2comic.src.chapter_parser import parse_novel_chapters
    from novel2comic.src.models import Novel
    from novel2comic.src.novel_registry import find_novel

    # 先尝试从缓存加载
    cache_entry = find_novel(args.novel)
    if cache_entry and os.path.exists(cache_entry.project_dir):
        novel_json = os.path.join(cache_entry.project_dir, "novel.json")
        if os.path.exists(novel_json):
            print(f"[Cache] 从缓存加载: {novel_json}")
            novel = Novel.load(novel_json)
            if novel.story_graph and hasattr(novel.story_graph, 'total_node_count'):
                n = novel.story_graph.total_node_count
                e = novel.story_graph.total_edge_count if hasattr(novel.story_graph, 'total_edge_count') else 0
                print(f"[Cache] KG: {n} 节点, {e} 边")

                output_path = args.output or os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "outputs", "kg_viz.html",
                )
                viz = KnowledgeGraphVisualizer()
                viz.render(novel.story_graph, output_path)
                print(f"\n请用浏览器打开: file:///{output_path.replace(os.sep, '/')}")
                return

        print("[Cache] 缓存数据无有效 KG，需要重新提取")

    # 无缓存，需要 API key 提取 KG
    api_key = _require_api_key()
    base_url = os.getenv("AGENTFLOW_BASE_URL", "https://api.deepseek.com/v1")
    model = os.getenv("AGENTFLOW_MODEL", "deepseek-chat")
    tool_model = os.getenv("AGENTFLOW_TOOL_MODEL", model)
    proxy = os.getenv("AGENTFLOW_PROXY", "")

    ctx, services, llm = _build_context_and_services(api_key, base_url, model, proxy, tool_model)

    print(f"[Loading] 正在加载 {args.novel}...")
    text = PS.read_text_file(args.novel)
    base_name = os.path.splitext(os.path.basename(args.novel))[0]
    chapters = parse_novel_chapters(text, base_name)

    project_dir = services.project.create_project_dir(base_name)
    ctx.novel = Novel(
        title=base_name, file_path=os.path.abspath(args.novel),
        chapters=chapters, output_dir=project_dir,
    )

    print(f"[KG] 正在提取知识图谱（{len(chapters)} 章）...")
    ctx.novel.story_graph = services.kg.extract_incremental(
        chapters,
        batch_size=int(os.getenv("KG_BATCH_SIZE", "10")),
    )
    services.project.save_novel(ctx.novel)
    print(f"[KG] 完成：{ctx.novel.story_graph.total_node_count} 个节点, "
          f"{ctx.novel.story_graph.total_edge_count} 条边")

    output_path = args.output or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "outputs", "kg_viz.html",
    )
    viz = KnowledgeGraphVisualizer()
    viz.render(ctx.novel.story_graph, output_path)
    print(f"\n请用浏览器打开: file:///{output_path.replace(os.sep, '/')}")


# ============================================================
# 主入口
# ============================================================

async def main():
    """主入口：解析参数并分发。"""
    parser = argparse.ArgumentParser(
        description="Novel2Comic — 小说到漫画智能生成系统",
    )

    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # interactive 子命令（推荐）
    interactive_parser = subparsers.add_parser(
        "interactive", aliases=["i"],
        help="交互模式（支持运行时切换 Agent）",
    )
    interactive_parser.add_argument("--novel", type=str, required=True, help="小说文件路径")
    interactive_parser.add_argument("--mode", type=str, default="comic",
                                    choices=["comic", "continue", "roleplay", "recommend", "summarize"],
                                    help="初始 Agent 模式")

    # comic 子命令
    comic_parser = subparsers.add_parser("comic", help="漫画改编")
    comic_parser.add_argument("--novel", type=str, help="小说文件路径")
    comic_parser.add_argument("--text", type=str, help="直接输入小说文本")
    comic_parser.add_argument("--chapter", type=int, help="章节编号")
    comic_parser.add_argument("--title", type=str, help="章节标题")

    # continue 子命令
    continue_parser = subparsers.add_parser("continue", help="续写")
    continue_parser.add_argument("--novel", type=str, required=True, help="小说文件路径")
    continue_parser.add_argument("--from-chapter", type=int, default=1, help="从第几章开始续写")
    continue_parser.add_argument("--goal", type=str, default="", help="续写目标")

    # roleplay 子命令
    roleplay_parser = subparsers.add_parser("roleplay", help="角色扮演")
    roleplay_parser.add_argument("--novel", type=str, required=True, help="小说文件路径")
    roleplay_parser.add_argument("--character", type=str, required=True, help="角色名")

    # recommend 子命令
    recommend_parser = subparsers.add_parser("recommend", help="推荐")
    recommend_parser.add_argument("--novel", type=str, required=True, help="小说文件路径")
    recommend_parser.add_argument("--preferences", type=str, help="偏好描述")

    # summarize 子命令
    summarize_parser = subparsers.add_parser("summarize", help="摘要")
    summarize_parser.add_argument("--novel", type=str, required=True, help="小说文件路径")
    summarize_parser.add_argument("--chapter", type=int, help="章节编号")
    summarize_parser.add_argument("--character", type=str, help="角色名")
    summarize_parser.add_argument("--theme", action="store_true", help="全书主题分析")
    summarize_parser.add_argument("--perspective", type=str, help="摘要视角")

    # viz 子命令
    viz_parser = subparsers.add_parser("viz", help="知识图谱可视化")
    viz_parser.add_argument("--novel", type=str, required=True, help="小说文件路径")
    viz_parser.add_argument("--output", type=str, default="", help="输出 HTML 路径（默认 outputs/kg_viz.html）")

    # 兼容旧用法
    parser.add_argument("text_or_file", nargs="?", help="小说文本或文件路径")
    parser.add_argument("title", nargs="?", help="章节标题")

    args = parser.parse_args()

    # 路由：子命令 > 交互模式 > 自动路由 > 帮助
    if args.command in ("comic", "continue", "roleplay", "recommend", "summarize"):
        await run_interactive(args)
    elif args.command in ("interactive", "i"):
        await run_interactive(args)
    elif args.command == "viz":
        await run_viz(args)
    elif args.text_or_file:
        # 自动路由
        from novel2comic.src.router.router import IntentRouter, Intent

        input_text = args.text_or_file

        class LegacyArgs:
            command = None
            novel = None
            text = None
            chapter = None
            title = None
            character = None
            goal = None
            preferences = None
            from_chapter = 1
            mode = "comic"

        legacy = LegacyArgs()

        if os.path.isfile(input_text):
            intent, confidence = IntentRouter.classify(args.title or "漫画改编")
            legacy.novel = input_text
            legacy.title = args.title
        else:
            intent, confidence = IntentRouter.classify(input_text)
            legacy.text = input_text
            legacy.title = args.title or "未命名章节"

        print(IntentRouter.format_clarification(input_text, intent, confidence))
        legacy.command = intent.value

        if intent == Intent.ROLEPLAY:
            legacy.character = args.title or "主角"
        elif intent == Intent.CONTINUATION:
            legacy.goal = input_text if not os.path.isfile(input_text) else ""

        await run_interactive(legacy)
    else:
        parser.print_help()


if __name__ == "__main__":
    asyncio.run(main())
