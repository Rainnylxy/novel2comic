"""
在线 Eval Runner —— 完整端到端评估。

流程:
  1. 初始化 Pipeline（LLM + KG + 蒸馏）
  2. 跑 Pipeline 写 N 章
  3. 自动采集: agent_trace.log → trace fixtures
  4. 自动采集: chapter 输出 → quality/errors fixtures
  5. 运行 trace verifier
  6. 写 verdicts

用法:
  python -m tests.eval.run_online_eval --novel "novels/The Sound of Silence.txt" --chapters 1

首次运行会做 KG 提取 + 角色蒸馏（较慢），后续运行走缓存（秒级）。
"""

import asyncio
import json
import os
import sys
import argparse
import time
from datetime import datetime
from typing import Optional

# 确保项目根目录在 path 中
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

# 自动加载 .env
def _load_dotenv():
    env_path = os.path.join(ROOT_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip()
                if key and key not in os.environ:
                    os.environ[key] = val

_load_dotenv()

# Windows 修复
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


EVAL_DIR = os.path.dirname(os.path.abspath(__file__))
FIXTURES_DIR = os.path.join(EVAL_DIR, "fixtures")
VERDICTS_DIR = os.path.join(EVAL_DIR, "verdicts")
INPUTS_DIR = os.path.join(EVAL_DIR, "inputs")

if EVAL_DIR not in sys.path:
    sys.path.insert(0, EVAL_DIR)


# ================================================================
# Pipeline 初始化
# ================================================================

def _build_services():
    """构建 LLM + ServiceRegistry。从环境变量读取配置。"""
    import openai
    import httpx
    from agentflow.runtime.llm_client import OpenAIClient
    from src.core.context import AppContext, ServiceRegistry
    from src.core.llm import UnifiedLLM
    from src.services import KnowledgeGraphService, ProjectService

    api_key = os.getenv("AGENTFLOW_API_KEY", "")
    base_url = os.getenv("AGENTFLOW_BASE_URL", "https://api.deepseek.com/v1")
    model = os.getenv("AGENTFLOW_MODEL", "deepseek-chat")
    proxy = os.getenv("AGENTFLOW_PROXY", "")

    if not api_key:
        raise RuntimeError("未设置 API key，请检查 .env 文件")

    agent_llm = OpenAIClient(
        api_key=api_key, model=model,
        base_url=base_url, proxy=proxy or None,
    )

    http_client = httpx.Client(proxy=proxy) if proxy else None
    sync_openai = openai.OpenAI(
        api_key=api_key, base_url=base_url, http_client=http_client,
    )

    tool_model = os.getenv("AGENTFLOW_TOOL_MODEL", model)
    llm = UnifiedLLM(sync_openai, tool_model)

    projects_dir = os.path.join(ROOT_DIR, "src", "projects")
    project_svc = ProjectService(projects_dir)
    kg_svc = KnowledgeGraphService(llm=llm)

    services = ServiceRegistry(
        kg=kg_svc, project=project_svc,
        llm=llm, agent_llm=agent_llm,
    )
    ctx = AppContext(services=services)
    return ctx, services, llm


# ================================================================
# 主流程
# ================================================================

async def run_online_eval(
    novel_path: str,
    num_chapters: int = 1,
    instruction: str = "",
    force_rebuild: bool = False,
):
    """运行在线 eval。

    Args:
        novel_path: 小说文件路径
        num_chapters: 续写章数
        instruction: 用户指令（可选）
        force_rebuild: 是否强制重建 KG（跳过缓存）
    """
    from src.pipeline.pipeline import ContinuationPipeline
    from src.services.project_service import ProjectService as PS
    from src.chapter_parser import parse_novel_chapters
    from src.core.models import Novel

    print("=" * 60)
    print("在线 Eval Runner")
    print(f"  小说: {novel_path}")
    print(f"  续写章数: {num_chapters}")
    print(f"  强制重建: {force_rebuild}")
    print(f"  时间: {datetime.now().isoformat()}")
    print("=" * 60)

    # ── 1. 初始化服务 ──
    print("\n[1/5] 初始化 LLM + 服务...")
    t0 = time.time()
    ctx, services, llm = _build_services()
    print(f"  模型: {os.getenv('LLM_MODEL', 'deepseek-chat')} ({time.time() - t0:.1f}s)")

    # ── 2. 加载小说 + KG 提取 + 蒸馏 ──
    print(f"\n[2/5] 加载小说 + KG 提取 + 蒸馏...")
    t0 = time.time()

    pipeline = ContinuationPipeline(ctx, services, llm)

    if force_rebuild:
        # 清除缓存
        base_name = os.path.splitext(os.path.basename(novel_path))[0]
        import glob
        for d in glob.glob(os.path.join(ROOT_DIR, "src", "projects", f"{base_name}*")):
            import shutil
            shutil.rmtree(d, ignore_errors=True)
            print(f"  清除缓存: {d}")

    pipeline.load_novel(novel_path)
    print(f"  加载完成 ({time.time() - t0:.1f}s)")
    print(f"  原文章数: {pipeline.chapter}")

    # ── 3. 运行 Pipeline ──
    print(f"\n[3/5] 运行 Pipeline (续写 {num_chapters} 章)...")
    t0 = time.time()

    events = []
    chapter_outputs = []
    async for event in pipeline.run(
        instruction=instruction or f"请从第{pipeline.chapter + 1}章开始续写",
        auto_loop=(num_chapters > 1),
    ):
        events.append(event)
        # 实时输出阶段切换
        if event.event_type == "phase":
            print(f"  → {event.data.get('phase', '?')}")
        elif event.event_type == "complete":
            ch = event.data.get("chapter", "?")
            title = event.data.get("title", "")
            frags = len(event.data.get("revised_fragments", []))
            changes = len(event.data.get("changes", []))
            print(f"  ✓ 第{ch}章「{title}」完成: {frags}片段, {changes}处修改")
            chapter_outputs.append(event.data)

    elapsed = time.time() - t0
    print(f"  完成 ({elapsed:.1f}s, {len(chapter_outputs)} 章)")

    # ── 4. 采集 fixtures ──
    print(f"\n[4/5] 采集 eval 数据...")

    # 4a. 从 agent_trace.log 提取 trace fixtures
    from collect_fixtures import parse_agent_trace_log, extract_trace_fixtures

    trace_log_path = os.path.join(ROOT_DIR, "agent_trace.log")
    traces = parse_agent_trace_log(trace_log_path)

    # 只取本次运行产生的 trace（最近 N 条）
    # Architect 每章1条 + Writer 每章每节1条（4节）+ Review 每章1条（当前不走ReAct）
    expected_trace_count = num_chapters * (1 + 4)  # Architect + Writer*4节
    if len(traces) > expected_trace_count:
        traces = traces[-expected_trace_count:]
        print(f"  截取最近 {expected_trace_count} 条 trace（Architect×{num_chapters} + Writer×{num_chapters*4}）")

    trace_fixtures = extract_trace_fixtures(traces)

    for fid, data in trace_fixtures.items():
        path = os.path.join(FIXTURES_DIR, f"trace_{fid}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"  trace fixtures: {len(trace_fixtures)} 个")

    # 4b. 从 project dir 提取 chapter 输出
    project_dir = ctx.novel.output_dir if ctx.novel else ""
    if project_dir and chapter_outputs:
        from collect_fixtures import extract_quality_fixtures, extract_review_fixtures

        quality_fixtures = extract_quality_fixtures(project_dir)
        for fid, data in quality_fixtures.items():
            path = os.path.join(FIXTURES_DIR, f"quality_{fid}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"  quality fixtures: {len(quality_fixtures)} 个")

        review_fixtures = extract_review_fixtures(project_dir)
        for fid, data in review_fixtures.items():
            path = os.path.join(FIXTURES_DIR, f"errors_{fid}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"  review fixtures: {len(review_fixtures)} 个")

    # 4c. 保存完整上下文
    from collect_fixtures import extract_full_context
    context = extract_full_context(project_dir) if project_dir else None
    if context:
        path = os.path.join(FIXTURES_DIR, "context.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(context, f, ensure_ascii=False, indent=2)
        print(f"  context: {len(context.get('character_profiles', {}))} 角色")

    # ── 5. 运行 trace verifier ──
    print(f"\n[5/5] 运行 trace verifier...")

    # 加载 trace 用例
    trace_cases_path = os.path.join(INPUTS_DIR, "trace-v1.jsonl")
    trace_cases = []
    if os.path.exists(trace_cases_path):
        with open(trace_cases_path, "r", encoding="utf-8") as f:
            trace_cases = [json.loads(line) for line in f if line.strip()]

    # 运行 verifier
    from run_eval import run_trace_eval, write_verdicts

    if trace_cases and trace_fixtures:
        # 给没有 fixture 字段的 case 自动匹配
        for case in trace_cases:
            if "fixture" not in case:
                # 按 agent 类型自动匹配
                agent = case.get("agent", "")
                fixtures_of_type = [k for k in trace_fixtures.keys()
                                   if ("arch" in k and "Architect" in agent)
                                   or ("writer" in k and "Writer" in agent)]
                if fixtures_of_type:
                    case["fixture"] = fixtures_of_type[0]

        verdicts = run_trace_eval(trace_cases)
        write_verdicts(verdicts, "trace", "online", "online")
    else:
        print("  无 trace 用例或 trace fixture，跳过")

    # ── 总结 ──
    print(f"\n{'=' * 60}")
    print("在线 Eval 完成")
    print(f"  章节产出: {len(chapter_outputs)} 章")
    print(f"  Trace fixtures: {len(trace_fixtures)} 个")
    if chapter_outputs:
        for ch in chapter_outputs:
            print(f"    第{ch.get('chapter','?')}章「{ch.get('title','?')}」: "
                  f"{len(ch.get('revised_fragments',[]))}片段, "
                  f"{len(ch.get('changes',[]))}处修改, "
                  f"评分={ch.get('review',{}).get('overall_score','?') if isinstance(ch.get('review'), dict) else '?'}")
    print(f"{'=' * 60}")

    return {
        "chapter_outputs": chapter_outputs,
        "trace_fixtures": trace_fixtures,
        "elapsed": elapsed,
    }


def parse_args():
    ap = argparse.ArgumentParser(description="在线 Eval Runner")
    ap.add_argument("--novel", default="novels/The Sound of Silence.txt",
                    help="小说文件路径")
    ap.add_argument("--chapters", type=int, default=1,
                    help="续写章数")
    ap.add_argument("--instruction", default="",
                    help="用户指令")
    ap.add_argument("--force-rebuild", action="store_true",
                    help="强制重建 KG（清除缓存）")
    ap.add_argument("--skip-pipeline", action="store_true",
                    help="跳过 Pipeline 运行，仅从已有数据采集")
    return ap.parse_args()


def main():
    args = parse_args()

    # 确保目录
    os.makedirs(FIXTURES_DIR, exist_ok=True)
    os.makedirs(VERDICTS_DIR, exist_ok=True)

    if args.skip_pipeline:
        print("跳过 Pipeline 运行，仅从已有数据采集")
        from collect_fixtures import (
            parse_agent_trace_log, extract_trace_fixtures,
            extract_quality_fixtures, extract_review_fixtures,
            extract_full_context,
        )
        trace_log_path = os.path.join(ROOT_DIR, "agent_trace.log")
        traces = parse_agent_trace_log(trace_log_path)
        print(f"  解析到 {len(traces)} 条 trace")

        trace_fixtures = extract_trace_fixtures(traces)
        for fid, data in trace_fixtures.items():
            path = os.path.join(FIXTURES_DIR, f"trace_{fid}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"  保存 {len(trace_fixtures)} 个 trace fixtures")
        return

    asyncio.run(run_online_eval(
        novel_path=args.novel,
        num_chapters=args.chapters,
        instruction=args.instruction,
        force_rebuild=args.force_rebuild,
    ))


if __name__ == "__main__":
    main()
