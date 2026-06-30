# -*- coding: utf-8 -*-
"""CLI —— 角色扮演模式入口。

用法:
    python main.py roleplay --novel 小说.txt --character 江停
    python main.py interactive --novel 小说.txt --mode roleplay --character 江停
"""

import argparse
import asyncio
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ..context import GlobalContext, ServiceRegistry
from ..llm import UnifiedLLM
from ..services import (
    KnowledgeGraphService,
    ProjectService,
)
from ..services.project_service import ProjectService as PS


# ============================================================
# 基础设施
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

    services = ServiceRegistry(
        kg=kg_svc,
        project=project_svc,
    )
    ctx.services = services
    return ctx, services, llm


def _load_novel(novel_path: str, services: ServiceRegistry, ctx: GlobalContext):
    """加载小说 + 逐章提取 KG。"""
    from ..chapter_parser import parse_novel_chapters
    from ..models import Novel

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

    _auto_viz(ctx, base_name)

    return base_name, chapters, text


def _auto_viz(ctx: GlobalContext, base_name: str):
    """KG 提取完成后自动生成 DOT 图。"""
    graph = ctx.novel.story_graph if ctx.novel else None
    if not graph or graph.total_node_count == 0:
        return
    from ..services.graph_viz import KnowledgeGraphVisualizer
    viz = KnowledgeGraphVisualizer()
    output_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "outputs",
    )
    dot_path = os.path.join(output_dir, f"{base_name}_kg.dot")
    viz.render_dot(graph, dot_path)
    print(f"  → DOT 图: {dot_path}")
    print(f"  → 在线查看: https://dreampuf.github.io/GraphvizOnline/")


def _require_api_key():
    api_key = os.getenv("AGENTFLOW_API_KEY", "")
    if not api_key:
        print("[!] AGENTFLOW_API_KEY not set.")
        print("    请设置环境变量: $env:AGENTFLOW_API_KEY='sk-your-key'")
        sys.exit(1)
    return api_key


# ============================================================
# 交互循环
# ============================================================

async def _run_interactive_loop(agent, first_task: str = None):
    """交互对话循环。"""
    print()
    print(f"[Agent] 角色扮演模式 | 扮演: {agent.rp.active_character}")
    print("[Agent] 输入 /help 查看可用命令，quit 退出")
    print()

    if first_task:
        print(f"[System] {first_task[:100]}...")
        result = await agent.run(first_task)
        print(f"\n{agent.rp.active_character}: {result}\n")

    while True:
        try:
            user_input = input("You > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[Agent] 再见。")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("[Agent] 再见。")
            break
        if user_input == "/help":
            print("命令: /switch <角色名> | /state | /help | quit")
            continue
        if user_input.startswith("/switch "):
            char = user_input[len("/switch "):].strip()
            agent.switch_character(char)
            print(f"[Agent] 切换至: {char}")
            continue
        if user_input == "/state":
            rp = agent.rp
            print(f"[State] {rp.format_state_for_prompt()}")
            print(f"[Emotion] {rp.get_emotion_summary()}")
            continue

        result = await agent.run(user_input)
        print(f"\n{agent.rp.active_character}: {result}\n")


# ============================================================
# 命令入口
# ============================================================

async def run_roleplay(args):
    """角色扮演模式。"""
    api_key = _require_api_key()
    base_url = os.getenv("AGENTFLOW_BASE_URL", "https://api.deepseek.com/v1")
    model = os.getenv("AGENTFLOW_MODEL", "deepseek-chat")
    tool_model = os.getenv("AGENTFLOW_TOOL_MODEL", model)
    proxy = os.getenv("AGENTFLOW_PROXY", "")

    ctx, services, llm = _build_context_and_services(api_key, base_url, model, proxy, tool_model)

    character = getattr(args, "character", "主角")

    if getattr(args, "novel", None):
        _load_novel(args.novel, services, ctx)

    from ..agents.roleplay_agent import RolePlayAgent
    agent = RolePlayAgent(ctx, services, llm)
    agent.init_character(character)

    novel_name = ctx.novel.title if ctx.novel else "未知"
    first_task = (
        f"角色 {character} 已加载。小说: 《{novel_name}》。"
        f"现在以 {character} 的身份开始对话。"
        f"记住: 每次回复前先 Thought（内心独白），然后根据需要调用 "
        f"retrieve_memory / adjust_emotion / check_boundary，最后 speak 输出。"
    )

    await _run_interactive_loop(agent, first_task)


# ============================================================
# CLI 入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Novel2Comic - 角色扮演")
    subparsers = parser.add_subparsers(dest="command", help="命令")

    # roleplay
    rp = subparsers.add_parser("roleplay", help="角色扮演")
    rp.add_argument("--novel", type=str, required=True, help="小说文件路径")
    rp.add_argument("--character", type=str, required=True, help="角色名")

    # interactive
    inter = subparsers.add_parser("interactive", help="交互模式")
    inter.add_argument("--novel", type=str, required=True, help="小说文件路径")
    inter.add_argument("--character", type=str, default="主角", help="角色名")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return

    asyncio.run(run_roleplay(args))
