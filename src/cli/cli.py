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
            print("命令: /switch <角色名> | /state | /scene | /goto <章节号> | /help | quit")
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
        if user_input == "/scene":
            scene = agent.rp.scene
            if scene and scene.chapter_index > 0:
                print(scene.format_detail())
            else:
                print("[Scene] 场景信息不可用（小说未加载或 KG 未构建）")
            continue
        if user_input.startswith("/goto "):
            try:
                ch = int(user_input[len("/goto "):].strip())
                agent.switch_chapter(ch)
                scene = agent.rp.scene
                if scene and scene.chapter_index > 0:
                    print(f"[Scene] 已跳转至第 {scene.chapter_index} 章"
                          + (f"「{scene.chapter_title}」" if scene.chapter_title else ""))
                else:
                    print(f"[Scene] 已跳转至第 {ch} 章")
            except ValueError:
                print("[Scene] 用法: /goto <章节号>")
            continue

        result = await agent.run(user_input)
        print(f"\n{agent.rp.active_character}: {result}\n")


# ============================================================
# Play 模式 —— 互动小说引擎
# ============================================================

async def _run_play_loop(director, first_task: str = None):
    """互动小说交互循环 —— 支持抉择交互。"""
    print()
    print(f"[Director] 互动小说模式 | 用户角色: {director.state.user_character.name}")
    print(f"[Director] 在场角色: {director._npcs.get_present_characters_str()}")
    print("[Director] 输入 /help 查看可用命令，quit 退出")
    print()

    if first_task:
        print(f"[Director] 正在设置场景...")
        result = await director.run(first_task)
        if result:
            _display_director_output(result)
        print()

    while True:
        try:
            user_input = input("You > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[Director] 正在判定结局...")
            _show_ending(director)
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("[Director] 正在判定结局...")
            _show_ending(director)
            break
        if user_input == "/help":
            print("命令: /intimacy | /flags | /scene | /cast | /help | quit")
            continue
        if user_input == "/intimacy":
            print(director.state.intimacy_summary())
            continue
        if user_input == "/flags":
            print(f"剧情旗标: {director.state.plot_flags}")
            continue
        if user_input == "/cast":
            print(f"在场角色: {director._npcs.get_present_characters_str()}")
            continue
        if user_input == "/scene":
            print(f"第{director.state.chapter}章 | 总轮次: {director.state.total_turns}")
            continue

        result = await director.run(user_input)
        if result:
            _display_director_output(result)
        print()


def _display_director_output(text: str):
    """显示 Director 输出。检测抉择标记并展示。"""
    if "<!--CHOICE-->" in text and "<!--ENDCHOICE-->" in text:
        # 分离普通文本和抉择
        parts = text.split("<!--CHOICE-->", 1)
        normal_text = parts[0].strip()
        choice_part = parts[1].split("<!--ENDCHOICE-->")[0].strip()
        after_text = parts[1].split("<!--ENDCHOICE-->")[-1].strip()

        if normal_text:
            print(f"\n{normal_text}")
        if choice_part:
            print(choice_part)
        if after_text:
            print(f"\n{after_text}")
    else:
        print(f"\n{text}")


def _show_ending(director):
    """显示结局判定。"""
    state = director.state
    print("\n" + "═" * 42)
    print("  📖 故事结束")
    print("═" * 42)
    print(f"\n  用户角色: {state.user_character.name} ({state.user_character.identity})")
    print(f"  经历章节: 第{state.chapter}章")
    print(f"  对话轮次: {state.total_turns}")
    print(f"\n  最终亲密度:")
    print(state.intimacy_summary())
    print(f"\n  关键决策: {len(state.pivot_decisions) + len(state.regular_decisions)} 次")
    print(f"  结局倾向: {state.active_ending}")
    print("═" * 42)


async def run_play(args):
    """互动小说模式。"""
    api_key = _require_api_key()
    base_url = os.getenv("AGENTFLOW_BASE_URL", "https://api.deepseek.com/v1")
    model = os.getenv("AGENTFLOW_MODEL", "deepseek-chat")
    tool_model = os.getenv("AGENTFLOW_TOOL_MODEL", model)
    proxy = os.getenv("AGENTFLOW_PROXY", "")

    ctx, services, llm = _build_context_and_services(api_key, base_url, model, proxy, tool_model)

    if getattr(args, "novel", None):
        _load_novel(args.novel, services, ctx)

    start_chapter = getattr(args, "chapter", 0) or 1
    npc_names = getattr(args, "npcs", []) or []
    user_name = getattr(args, "user", "林默")
    user_identity = getattr(args, "identity", "新调来的刑警")

    graph = ctx.novel.story_graph if ctx.novel else None
    chapters = ctx.novel.chapters if ctx.novel else []
    total = len(chapters)

    # 如果没有指定 NPC，从 KG 自动检测
    if not npc_names and graph:
        from ..scene_engine import SceneEngine
        events = SceneEngine.get_events_in_chapter(graph, start_chapter)
        # 从事件参与者中提取角色名
        detected = set()
        for ev in events:
            for p in ev.get("participants", []):
                name = p.get("name", "") if isinstance(p, dict) else str(p)
                if name:
                    detected.add(name)
        # 从 appears_in_edges 提取
        for edge in graph.appears_in_edges:
            if edge.chapter == start_chapter:
                detected.add(edge.person)
        # 过滤掉不重要角色，取前 5 个
        persons = graph.person_nodes
        importance_map = {p.name: p.importance for p in persons}
        npc_names = sorted(detected, key=lambda n: -importance_map.get(n, 0))[:5]
        if not npc_names:
            # 至少取 2 个最重要的角色
            top = sorted(persons, key=lambda p: -p.importance)[:3]
            npc_names = [p.name for p in top]

    if not npc_names:
        print("[!] 无法确定 NPC 角色。请用 --npcs 指定。")
        sys.exit(1)

    print(f"[Director] NPC 角色: {', '.join(npc_names)}")

    # 导入互动引擎
    from ..interactive import (
        StoryState, UserCharacter, ChoiceEngine, NPCManager, DirectorAgent,
    )

    # 创建 StoryState
    user_char = UserCharacter(
        name=user_name,
        identity=user_identity,
        backstory=f"第{start_chapter}章开始出现的{user_identity}",
        first_appearance_chapter=start_chapter,
    )
    state = StoryState(
        user_character=user_char,
        chapter=start_chapter,
        total_chapters=total,
    )

    # 初始化亲密度（从 KG 关系推导基础值，默认 0）
    for name in npc_names:
        state.intimacy[name] = 0

    # 创建引擎
    choice_engine = ChoiceEngine(llm)
    npc_manager = NPCManager(ctx, services, llm, npc_names, start_chapter)
    npc_manager.set_user_context(user_name, state)

    # 创建 Director
    director = DirectorAgent(ctx, services, llm, state, npc_manager, choice_engine)

    novel_name = ctx.novel.title if ctx.novel else "未知"
    first_task = (
        f"用户角色 {user_name} ({user_identity}) 已进入《{novel_name}》的世界。"
        f"当前是第 {start_chapter} 章。"
        f"请 narrate 设置初始场景，然后等待用户说话。"
    )

    await _run_play_loop(director, first_task)


# ============================================================
# 命令入口
# ============================================================

async def run_server(args):
    """启动 Web 服务。"""
    api_key = _require_api_key()
    base_url = os.getenv("AGENTFLOW_BASE_URL", "https://api.deepseek.com/v1")
    model = os.getenv("AGENTFLOW_MODEL", "deepseek-chat")
    tool_model = os.getenv("AGENTFLOW_TOOL_MODEL", model)
    proxy = os.getenv("AGENTFLOW_PROXY", "")

    ctx, services, llm = _build_context_and_services(api_key, base_url, model, proxy, tool_model)

    port = getattr(args, "port", 8000)

    from ..server import start_server
    start_server(ctx, services, llm, port)


async def run_roleplay(args):
    """角色扮演模式。"""
    api_key = _require_api_key()
    base_url = os.getenv("AGENTFLOW_BASE_URL", "https://api.deepseek.com/v1")
    model = os.getenv("AGENTFLOW_MODEL", "deepseek-chat")
    tool_model = os.getenv("AGENTFLOW_TOOL_MODEL", model)
    proxy = os.getenv("AGENTFLOW_PROXY", "")

    ctx, services, llm = _build_context_and_services(api_key, base_url, model, proxy, tool_model)

    character = getattr(args, "character", "主角")
    start_chapter = getattr(args, "chapter", 0) or 0

    if getattr(args, "novel", None):
        _load_novel(args.novel, services, ctx)

    from ..agents.roleplay_agent import RolePlayAgent
    agent = RolePlayAgent(ctx, services, llm)
    agent.init_character(character, start_chapter=start_chapter)

    novel_name = ctx.novel.title if ctx.novel else "未知"
    ch_info = f"第{start_chapter}章" if start_chapter else "首次出场章节"
    first_task = (
        f"角色 {character} 已加载。小说: 《{novel_name}》({ch_info})。"
        f"现在以 {character} 的身份开始对话。"
        f"记住: 每次回复前先 Thought（内心独白），然后根据需要调用 "
        f"retrieve_memory / adjust_emotion / check_boundary，最后直接输出对话。"
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
    rp.add_argument("--chapter", type=int, default=0,
                    help="起始章节 (1-based，默认使用角色首次出场章节)")

    # interactive
    inter = subparsers.add_parser("interactive", help="交互模式")
    inter.add_argument("--novel", type=str, required=True, help="小说文件路径")
    inter.add_argument("--character", type=str, default="主角", help="角色名")
    inter.add_argument("--chapter", type=int, default=0, help="起始章节")

    # play — 互动小说模式
    play = subparsers.add_parser("play", help="互动小说模式 (用户角色 × 抉择 × 多结局)")
    play.add_argument("--novel", type=str, required=True, help="小说文件路径")
    play.add_argument("--chapter", type=int, default=1, help="起始章节 (默认第1章)")
    play.add_argument("--user", type=str, default="林默", help="用户角色名")
    play.add_argument("--identity", type=str, default="新调来的刑警", help="用户角色身份")
    play.add_argument("--npcs", nargs="+", default=[],
                      help="指定 NPC 角色列表，如: 江停 严峫。不指定则自动从 KG 检测")

    # server — Web 服务模式
    srv = subparsers.add_parser("server", help="启动 Web 服务 (互动小说前端)")
    srv.add_argument("--port", type=int, default=8000, help="监听端口 (默认 8000)")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return

    if args.command == "play":
        asyncio.run(run_play(args))
    elif args.command == "server":
        run_server(args)
    else:
        asyncio.run(run_roleplay(args))
