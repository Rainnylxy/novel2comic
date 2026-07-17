# -*- coding: utf-8 -*-
"""续写质量 Eval Runner —— 用前 N 章建 KG，续写 M 章，LLM Judge 打分。

用法:
  python -m tests.eval.run_quality_eval --case poyun-5-3
  python -m tests.eval.run_quality_eval --all
"""

import asyncio
import json
import os
import sys
import argparse
import time
import tempfile
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
                key, val = key.strip(), val.strip().strip("\"'")
                if key and key not in os.environ:
                    os.environ[key] = val

_load_dotenv()

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

EVAL_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(EVAL_DIR, "results")
TEST_SET_PATH = os.path.join(EVAL_DIR, "test_set.json")


# ================================================================
# 辅助: 截取小说前 N 章
# ================================================================

def _read_novel(novel_path: str, encoding: str) -> str:
    """读取小说全文。"""
    full_path = os.path.join(ROOT_DIR, novel_path) if not os.path.isabs(novel_path) else novel_path
    with open(full_path, "r", encoding=encoding) as f:
        return f.read()


def truncate_novel(text: str, n_chapters: int) -> str:
    """截取小说前 N 章内容，返回截断后的文本。

    章节边界由章节标题行确定。截取到第 N+1 章的标题行之前。
    """
    from src.core.chapter_parser import CHAPTER_PATTERNS, _parse_cn_number

    lines = text.split("\n")
    chapter_lines = []  # [(line_index, chapter_number)]

    for i, line in enumerate(lines):
        stripped = line.strip().strip("　")
        if not stripped:
            continue
        for pattern in CHAPTER_PATTERNS:
            m = pattern.match(stripped)
            if m:
                try:
                    num = _parse_cn_number(m.group(1))
                    chapter_lines.append((i, num))
                except (ValueError, IndexError):
                    pass
                break

    # 去重
    seen = set()
    unique = []
    for idx, num in chapter_lines:
        if num not in seen:
            unique.append((idx, num))
            seen.add(num)

    if len(unique) <= n_chapters:
        return text  # 章节不足，返回全文

    # 找到第 N+1 章的行号
    cutoff_line = unique[n_chapters][0]
    return "\n".join(lines[:cutoff_line])


# ================================================================
# 基础设施初始化
# ================================================================

def _build_services():
    """构建 LLM + ServiceRegistry。"""
    import openai
    import httpx
    from agentflow.runtime.llm_client import OpenAIClient
    from src.core.context import AppContext, ServiceRegistry
    from src.core.llm import UnifiedLLM
    from src.services import KnowledgeGraphService, ProjectService

    api_key = os.getenv("AGENTFLOW_API_KEY", "")
    base_url = os.getenv("AGENTFLOW_BASE_URL", "https://api.deepseek.com/")
    model = os.getenv("AGENTFLOW_MODEL", "deepseek-v4-flash")
    proxy = os.getenv("AGENTFLOW_PROXY", "")

    if not api_key:
        raise RuntimeError("未设置 API key")

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

    projects_dir = os.path.join(ROOT_DIR, "projects")
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

async def run_case(case: dict) -> dict:
    """运行单个测试用例。

    Args:
        case: 测试用例 dict

    Returns:
        {"case_id": ..., "verdicts": [...], "summary": {...}}
    """
    from src.pipeline.pipeline import ContinuationPipeline
    from tests.eval.judge import QualityJudge, fragments_to_text

    case_id = case["id"]
    novel = case["novel"]
    encoding = case.get("encoding", "utf-8")
    genre = case.get("genre", "")
    kg_chapters = case.get("kg_chapters", 5)
    num_chapters = case.get("continuation_chapters", 3)

    print(f"\n{'=' * 60}")
    print(f"[{case_id}] 开始评估")
    print(f"  小说: {novel} ({genre})")
    print(f"  KG前{kg_chapters}章 → 续写{num_chapters}章")
    print(f"{'=' * 60}")

    t_start = time.time()

    # ── 1. 截取小说前 N 章 ──
    print(f"\n[1/4] 截取前 {kg_chapters} 章...")
    full_text = _read_novel(novel, encoding)
    truncated = truncate_novel(full_text, kg_chapters)

    # 源文本（用于 Judge）
    source_text = truncated[-5000:] if len(truncated) > 5000 else truncated

    # 写入临时文件供 pipeline 加载
    tmpdir = tempfile.mkdtemp(prefix="n2c_eval_")
    tmp_novel = os.path.join(tmpdir, os.path.basename(novel))
    with open(tmp_novel, "w", encoding="utf-8") as f:
        f.write(truncated)
    print(f"  截取完成: {len(truncated)} chars → {tmp_novel}")

    # ── 2. 初始化 Pipeline ──
    print(f"\n[2/4] 初始化 Pipeline + 加载 KG...")
    ctx, services, llm = _build_services()
    pipeline = ContinuationPipeline(ctx, services, llm)

    try:
        pipeline.load_novel(tmp_novel)
        print(f"  原文章数: {pipeline.chapter}")

        # 提取蒸馏档案（作为 judge 参考标准）
        style_profile = pipeline._style_profile
        character_profiles = pipeline._character_profiles
        if style_profile:
            print(f"  文风档案: 已蒸馏")
        if character_profiles:
            print(f"  角色档案: {len(character_profiles)} 个")

    except Exception as e:
        print(f"  Pipeline 初始化失败: {e}")
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
        return {"case_id": case_id, "error": str(e), "verdicts": []}

    # ── 3. 运行续写 ──
    print(f"\n[3/4] 运行续写 {num_chapters} 章...")
    chapter_outputs = []

    try:
        async for event in pipeline.run(
            instruction=f"请从第{pipeline.chapter + 1}章开始续写",
            auto_loop=(num_chapters > 1),
        ):
            if event.event_type == "phase":
                print(f"  → {event.data.get('phase', '?')}")
            elif event.event_type == "complete":
                ch = event.data.get("chapter", "?")
                title = event.data.get("title", "")
                frags = len(event.data.get("revised_fragments", []))
                chapter_outputs.append(event.data)
                print(f"  [OK] 第{ch}章「{title}」: {frags} 片段")
            elif event.event_type == "error":
                print(f"  [ERR] {event.data.get('message', '?')}")
    except Exception as e:
        print(f"  Pipeline 运行失败: {e}")

    print(f"  生成 {len(chapter_outputs)} 章")

    if not chapter_outputs:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
        return {"case_id": case_id, "error": "No chapters generated", "verdicts": []}

    # ── 4. LLM Judge 评分 ──
    print(f"\n[4/4] LLM Judge 评分...")
    judge = QualityJudge(llm)

    verdicts = []
    for ch_data in chapter_outputs:
        ch_num = ch_data.get("chapter", "?")
        ch_title = ch_data.get("title", "")
        fragments = ch_data.get("revised_fragments", [])
        generated_text = fragments_to_text(fragments)

        print(f"  → 评估第{ch_num}章「{ch_title}」({len(fragments)} 片段)...")
        result = judge.evaluate(source_text, generated_text, genre,
                                style_profile=style_profile,
                                character_profiles=character_profiles)

        verdict = {
            "case_id": case_id,
            "chapter": ch_num,
            "title": ch_title,
            "genre": genre,
            "fragment_count": len(fragments),
            **result,
        }
        verdicts.append(verdict)

        # 打印得分概况
        scores = result.get("scores", {})
        dim_scores = ", ".join(
            f"{d.split('_')[0]}={s.get('score', '?')}"
            for d, s in scores.items()
        )
        print(f"    评分: {dim_scores} | overall={result.get('overall', '?')}")

    # ── 清理临时文件 ──
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)

    elapsed = time.time() - t_start

    # ── 汇总 ──
    overall_scores = [v.get("overall", 0) for v in verdicts if v.get("overall")]
    dim_averages = {}
    for dim in ["character_consistency", "setting_consistency",
                "style_consistency", "plot_coherence", "writing_quality"]:
        scores = [v.get("scores", {}).get(dim, {}).get("score", 0)
                   for v in verdicts]
        dim_averages[dim] = sum(scores) / len(scores) if scores else 0

    summary = {
        "case_id": case_id,
        "genre": genre,
        "chapters_generated": len(chapter_outputs),
        "elapsed_seconds": round(elapsed, 1),
        "overall_avg": (sum(overall_scores) / len(overall_scores)
                        if overall_scores else 0),
        "dimension_averages": dim_averages,
    }

    print(f"\n{'─' * 40}")
    print(f"[{case_id}] 完成 ({elapsed:.0f}s)")
    print(f"  平均总分: {summary['overall_avg']:.1f}")
    for dim, avg in dim_averages.items():
        print(f"  {dim}: {avg:.1f}")
    print(f"{'─' * 40}")

    return {"case_id": case_id, "verdicts": verdicts, "summary": summary}


# ================================================================
# 入口
# ================================================================

def load_test_set() -> dict:
    """加载测试集配置。"""
    if not os.path.exists(TEST_SET_PATH):
        print(f"测试集不存在: {TEST_SET_PATH}")
        sys.exit(1)
    with open(TEST_SET_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_args():
    ap = argparse.ArgumentParser(description="续写质量 Eval Runner")
    ap.add_argument("--all", action="store_true",
                    help="运行所有测试用例")
    ap.add_argument("--case", type=str, default="",
                    help="运行指定 case（如 poyun-5-3）")
    ap.add_argument("--output", type=str, default="",
                    help="输出文件路径（默认 results/YYYY-MM-DD-HHMMSS.json）")
    return ap.parse_args()


def main():
    args = parse_args()
    test_set = load_test_set()
    cases = test_set.get("cases", [])

    if args.case:
        cases = [c for c in cases if c["id"] == args.case]
        if not cases:
            print(f"未找到 case: {args.case}")
            print(f"可用: {[c['id'] for c in test_set.get('cases', [])]}")
            sys.exit(1)
    elif not args.all:
        # 默认: 运行第一个未跳过的 case
        cases = [c for c in cases if not c.get("skip")]
        if cases:
            print(f"默认运行第一个 case: {cases[0]['id']}")
            print(f"使用 --all 运行全部，--case <id> 运行指定")
            cases = cases[:1]
        else:
            print("没有可运行的 case")
            sys.exit(1)
    else:
        cases = [c for c in cases if not c.get("skip")]

    print("=" * 60)
    print("续写质量 Eval Runner")
    print(f"  测试用例: {len(cases)} 个")
    print(f"  时间: {datetime.now().isoformat()}")
    print("=" * 60)

    # 运行所有 case
    all_results = []
    for i, case in enumerate(cases):
        print(f"\n{'#' * 60}")
        print(f"# Case {i+1}/{len(cases)}: {case['id']}")
        print(f"{'#' * 60}")
        result = asyncio.run(run_case(case))
        all_results.append(result)

    # ── 保存结果 ──
    os.makedirs(RESULTS_DIR, exist_ok=True)

    if args.output:
        output_path = args.output
    else:
        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        output_path = os.path.join(RESULTS_DIR, f"quality_eval_{timestamp}.json")

    output_data = {
        "test_set": test_set.get("test_set", "?"),
        "timestamp": datetime.now().isoformat(),
        "results": all_results,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, indent=2)

    # ── 总览 ──
    print(f"\n{'=' * 60}")
    print("Eval 完成!")
    print(f"  结果保存到: {output_path}")
    print(f"{'=' * 60}")
    print(f"\n{'Case':<30} {'Genre':<12} {'Avg':>5} {'Ch':>3}")
    print("-" * 55)
    for r in all_results:
        s = r.get("summary", {})
        print(f"{s.get('case_id', '?'):<30} {s.get('genre', '?'):<12} "
              f"{s.get('overall_avg', 0):>5.1f} {s.get('chapters_generated', 0):>3}")
    print("-" * 55)

    # 全局平均
    all_overall = [r.get("summary", {}).get("overall_avg", 0)
                    for r in all_results if r.get("summary")]
    if all_overall:
        print(f"{'全局平均':<30} {'':<12} {sum(all_overall)/len(all_overall):>5.1f}")

    print(f"\n详细结果: {output_path}")


if __name__ == "__main__":
    main()
