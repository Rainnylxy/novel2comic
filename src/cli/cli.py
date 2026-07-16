# -*- coding: utf-8 -*-
"""CLI —— 续写引擎入口。

用法:
    python main.py write --novel novels/poyun.txt
    python main.py server --port 8000
    python main.py frontend --port 3000
"""

import argparse
import asyncio
import json
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ..core.context import AppContext, ServiceRegistry
from ..core.llm import UnifiedLLM
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
    """构建 AppContext 和 ServiceRegistry。

    LLM 统一放入 ServiceRegistry，ctx 只持有 novel + services。
    """
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

    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    projects_dir = os.path.join(root_dir, "projects")

    project_svc = ProjectService(projects_dir)
    kg_svc = KnowledgeGraphService(llm=llm)

    services = ServiceRegistry(
        kg=kg_svc,
        project=project_svc,
        llm=llm,
        agent_llm=agent_llm,
    )

    ctx = AppContext(services=services)
    return ctx, services, llm


def _load_novel(novel_path: str, services: ServiceRegistry, ctx: AppContext,
                force_rebuild: bool = False):
    """加载小说 + 逐章提取 KG。

    如果已有缓存的 KG（projects 目录下），直接加载，跳过提取。
    force_rebuild=True 强制重新提取。
    """
    from ..core.chapter_parser import parse_novel_chapters
    from ..core.models import Novel

    print(f"[Loading] 正在加载 {novel_path}...")
    text = PS.read_text_file(novel_path)
    base_name = os.path.splitext(os.path.basename(novel_path))[0]
    chapters = parse_novel_chapters(text, base_name)

    # 尝试加载已有缓存
    cached = None
    if not force_rebuild:
        cached = _find_cached_novel(services, base_name, len(chapters))

    if cached:
        ctx.novel = cached
        print(f"[KG] 从缓存加载：{ctx.novel.story_graph.total_node_count} 个节点，"
              f"{ctx.novel.story_graph.total_edge_count} 条边")
        return base_name, chapters, text

    # 无缓存，执行 KG 提取
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

    return base_name, chapters, text


def _find_cached_novel(services: ServiceRegistry, base_name: str,
                       expected_chapters: int):
    """在 projects 目录中查找已缓存的 novel.json。

    匹配条件: 目录名以 base_name 开头 + novel.json 存在 + 章节数匹配。
    返回 Novel 对象或 None。
    """
    from ..core.models import Novel

    projects_dir = services.project._projects_dir
    if not projects_dir or not os.path.isdir(projects_dir):
        return None

    # 找匹配的 project 目录
    candidates = sorted(
        [d for d in os.listdir(projects_dir)
         if d.startswith(base_name)],
        reverse=True,  # 最新的在前
    )

    for dir_name in candidates:
        novel_path = os.path.join(projects_dir, dir_name, "novel.json")
        if not os.path.exists(novel_path):
            continue
        try:
            with open(novel_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # 章节数匹配才复用（避免小说文件更新后 KG 过期）
            saved_chapters = len(data.get("chapters", []))
            if saved_chapters != expected_chapters:
                continue
            novel = Novel.from_dict(data)
            novel.file_path = ""  # 不从缓存恢复文件路径
            novel.output_dir = os.path.join(projects_dir, dir_name)
            if novel.story_graph and novel.story_graph.total_node_count > 0:
                return novel
        except Exception:
            continue

    return None



def _require_api_key():
    api_key = os.getenv("AGENTFLOW_API_KEY", "")
    if not api_key:
        print("[!] AGENTFLOW_API_KEY not set.")
        print("    请设置环境变量: $env:AGENTFLOW_API_KEY='sk-your-key'")
        sys.exit(1)
    return api_key


# ============================================================
# Write 模式 —— 续写模式
# ============================================================

async def run_write(args):
    """续写模式 —— 终端流式输出。"""
    api_key = _require_api_key()
    base_url = os.getenv("AGENTFLOW_BASE_URL", "https://api.deepseek.com/v1")
    model = os.getenv("AGENTFLOW_MODEL", "deepseek-chat")
    tool_model = os.getenv("AGENTFLOW_TOOL_MODEL", model)
    proxy = os.getenv("AGENTFLOW_PROXY", "")

    ctx, services, llm = _build_context_and_services(
        api_key, base_url, model, proxy, tool_model,
    )

    from ..pipeline.pipeline import ContinuationPipeline

    pipeline = ContinuationPipeline(ctx, services, llm)
    pipeline.load_novel(args.novel)

    print(f"\n[续写] 小说: {ctx.novel.title}")
    print(f"[续写] 当前进度: {pipeline.chapter} 章")
    print(f"[续写] 正在规划第 {pipeline.chapter + 1} 章...")
    print()

    instruction = getattr(args, "instruction", "")

    try:
        async for event in pipeline.run(instruction):
            if event.event_type == "phase":
                phase = event.data.get("phase", "")
                labels = {
                    "planning": "📋 正在规划大纲...",
                    "writing": "✍️ 正在写作...",
                    "reviewing": "🔍 正在一致性审校...",
                    "revising": "📝 正在修订...",
                }
                if phase in labels:
                    print(f"\n{labels[phase]}")
            elif event.event_type == "outline":
                outline = event.data
                print(f"  章标题: {outline.get('title', '?')}")
                print(f"  梗概: {outline.get('synopsis', '?')[:120]}...")
            elif event.event_type == "fragment":
                frag = event.data
                _print_fragment_terminal(frag)
            elif event.event_type == "review":
                issues = event.data.get("issues", [])
                score = event.data.get("overall_score", "?")
                print(f"\n  审校: {len(issues)} 个问题 | 评分: {score}")
            elif event.event_type == "complete":
                ch_num = event.data.get("chapter", "?")
                ch_title = event.data.get("title", "")
                chapter_count = event.data.get("chapter_count", 1)
                print(f"\n  ✅ 第{ch_num}章「{ch_title}」完成 ({chapter_count}/{chapter_count})")
            elif event.event_type == "done":
                written = event.data.get("chapters_written", 0)
                reason = event.data.get("reason", "")
                print(f"\n{'='*50}")
                print(f"  ✅ 续写结束: {written} 章 ({reason})")
                print(f"{'='*50}")
            elif event.event_type == "error":
                print(f"\n❌ 错误: {event.data.get('message', '')}")
    except KeyboardInterrupt:
        print("\n[续写] 用户中断")
    except Exception as e:
        print(f"\n❌ 错误: {e}")


def _print_fragment_terminal(frag: dict):
    """在终端中显示一个 fragment。"""
    ftype = frag.get("type", "narration")
    text = frag.get("text", "")
    character = frag.get("character", "")

    if ftype == "narration":
        print(f"\n  {text}")
    elif ftype == "dialogue":
        print(f"\n  [{character}] {text}")
    elif ftype == "action":
        print(f"    ({text})")
    elif ftype == "inner_thought":
        print(f"\n  [{character}] ┆ {text} ┆")
    elif ftype == "divider":
        label = frag.get("divider_label", "")
        print(f"\n  {'─' * 20} {label} {'─' * 20}")


# ============================================================
# CLI 入口
# ============================================================

def run_server(args):
    """启动 Web 服务（同步，Tornado 自己管理事件循环）。"""
    api_key = _require_api_key()
    base_url = os.getenv("AGENTFLOW_BASE_URL", "https://api.deepseek.com/v1")
    model = os.getenv("AGENTFLOW_MODEL", "deepseek-chat")
    tool_model = os.getenv("AGENTFLOW_TOOL_MODEL", model)
    proxy = os.getenv("AGENTFLOW_PROXY", "")

    ctx, services, llm = _build_context_and_services(api_key, base_url, model, proxy, tool_model)

    port = getattr(args, "port", 8000)

    from ..server import start_server
    start_server(ctx, services, llm, port)


def run_frontend(args):
    """启动前端开发服务器（同步阻塞）。"""
    import http.server
    import socketserver

    port = getattr(args, "port", 3000)
    backend = getattr(args, "backend", "http://localhost:8000")

    # 找到 frontend 目录
    root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    frontend_dir = os.path.join(root_dir, "frontend")

    # 动态注入 API_BASE
    html_path = os.path.join(frontend_dir, "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        html_content = f.read()
    html_content = html_content.replace(
        "window.API_BASE || 'http://localhost:8000'",
        f"'{backend}'",
    )

    class FrontendHandler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=frontend_dir, **kwargs)

        def do_GET(self):
            if self.path == "/" or self.path == "/index.html":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html_content.encode("utf-8"))
            else:
                super().do_GET()

        def log_message(self, format, *args):
            print(f"[Frontend] {args[0]}")

    with socketserver.TCPServer(("", port), FrontendHandler) as httpd:
        print(f"\n{'='*50}")
        print(f"  🖥  互动小说前端")
        print(f"  地址: http://localhost:{port}")
        print(f"  后端: {backend}")
        print(f"  按 Ctrl+C 停止")
        print(f"{'='*50}\n")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[Frontend] 已停止")


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
# Write 模式 —— 续写模式
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Novel2Comic - 续写引擎")
    subparsers = parser.add_subparsers(dest="command", help="命令")

    # write — 续写模式 (CLI)
    wr = subparsers.add_parser("write", help="终端续写模式")
    wr.add_argument("--novel", type=str, required=True, help="小说文件路径")
    wr.add_argument("--instruction", type=str, default="",
                    help="初始续写方向指令（可选）")

    # server — Web 服务模式
    srv = subparsers.add_parser("server", help="启动后端 API 服务")
    srv.add_argument("--port", type=int, default=8000, help="监听端口 (默认 8000)")

    # frontend — 前端开发服务器
    fe = subparsers.add_parser("frontend", help="启动前端开发服务器")
    fe.add_argument("--port", type=int, default=3000, help="监听端口 (默认 3000)")
    fe.add_argument("--backend", type=str, default="http://localhost:8000",
                    help="后端 API 地址 (默认 http://localhost:8000)")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return

    if args.command == "server":
        run_server(args)
    elif args.command == "frontend":
        run_frontend(args)
    elif args.command == "write":
        asyncio.run(run_write(args))
    else:
        parser.print_help()
