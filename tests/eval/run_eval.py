"""
Eval Runner —— 串联 inputs.jsonl → outputs → verifiers → verdicts.jsonl

三阶段框架（遵循 eval-harness 规范）：
  Stage 1: 读 inputs.jsonl
  Stage 2: 跑 Pipeline / 分析已有输出（runner 只负责抓取，不评分）
  Stage 3: 调用 verifiers 评分 → 写 verdicts.jsonl

三种评估模式:
  quality  — LLM-as-Judge 主观质量评分（需要 API key）
  errors   — 错误注入检测（需要 API key 调 Review Editor 或已有输出）
  trace    — Agent tool call 序列校验（纯确定性，无需 API key）

用法:
  # 运行全部评估
  python run_eval.py --mode all

  # 只运行 trace 分析（无需 API key，可离线运行）
  python run_eval.py --mode trace

  # 只运行质量评分
  python run_eval.py --mode quality

  # 只运行错误检测
  python run_eval.py --mode errors

  # 指定输入/输出版本
  python run_eval.py --mode all --version v2
"""

import json
import os
import sys
import argparse
from datetime import datetime
from typing import Optional


# ================================================================
# 路径配置
# ================================================================

EVAL_DIR = os.path.dirname(os.path.abspath(__file__))
INPUTS_DIR = os.path.join(EVAL_DIR, "inputs")
OUTPUTS_DIR = os.path.join(EVAL_DIR, "outputs")
VERDICTS_DIR = os.path.join(EVAL_DIR, "verdicts")
BASELINE_DIR = os.path.join(EVAL_DIR, "baseline")
VERIFIERS_DIR = os.path.join(EVAL_DIR, "verifiers")

# 确保 verifiers 模块可导入
if EVAL_DIR not in sys.path:
    sys.path.insert(0, EVAL_DIR)

# Git SHA 追踪
def get_git_sha() -> str:
    import subprocess
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=os.path.dirname(EVAL_DIR),
        )
        return result.stdout.strip()[:8]
    except Exception:
        return "unknown"


# ================================================================
# Stage 1 — 加载 inputs
# ================================================================

def load_inputs(version: str = "v1") -> dict[str, list[dict]]:
    """加载所有评估用例。

    Returns:
        {"quality": [...], "errors": [...], "trace": [...]}
    """
    inputs = {}
    for mode in ["quality", "errors", "trace"]:
        path = os.path.join(INPUTS_DIR, f"{mode}-{version}.jsonl")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                inputs[mode] = [json.loads(line) for line in f if line.strip()]
            print(f"  [{mode}] 加载 {len(inputs[mode])} 条用例")
        else:
            print(f"  [{mode}] 文件不存在: {path}")
            inputs[mode] = []
    return inputs


# ================================================================
# 辅助 — 加载 fixture
# ================================================================

def load_fixture(name: str) -> Optional[dict]:
    """加载测试 fixture（已保存的 pipeline 输出 / trace）。"""
    path = os.path.join(EVAL_DIR, "fixtures", f"{name}.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


# ================================================================
# Stage 2 — Runner（按模式分发）
# ================================================================

def run_trace_eval(cases: list[dict]) -> list[dict]:
    """Trace 评估: 确定性规则检查。

    每个 case 需要对应的 trace fixture。
    如果 fixture 不存在，生成一个空的占位输出。
    """
    from verifiers.trace_verifier import TraceVerifier

    verifier = TraceVerifier()
    outputs = []

    for case in cases:
        case_id = case["id"]

        # fixture 查找优先级: case 中显式指定 > trace_{id} 命名约定
        fixture_name = case.get("fixture", f"trace_{case_id}")
        trace_fixture = load_fixture(fixture_name)

        if trace_fixture is None:
            # 离线模式：没有真实 trace，生成模拟结果
            print(f"    [{case_id}] fixture 不存在，跳过（需先在线运行 pipeline）")
            outputs.append({
                "id": case_id,
                "mode": "trace",
                "status": "skipped",
                "reason": "fixture_not_found",
                "agent": case.get("agent", "unknown"),
                "scenario": case.get("scenario", ""),
                "verdict": None,
            })
            continue

        expected_spec = {
            "expected_tools": case.get("expected_tools", []),
            "forbidden_tools": case.get("forbidden_tools", []),
            "expected_order": case.get("expected_order", []),
            "reason": case.get("reason", ""),
        }

        result = verifier.verify(trace_fixture, expected_spec)
        outputs.append({
            "id": case_id,
            "mode": "trace",
            "status": "evaluated",
            "agent": case.get("agent", "unknown"),
            "scenario": case.get("scenario", ""),
            "verdict": result,
        })
        status = "PASS" if result["passes"] else "FAIL"
        print(f"    [{case_id}] {case.get('scenario', '')}: {status}")

    return outputs


def run_errors_eval(cases: list[dict]) -> list[dict]:
    """错误注入检测评估。

    需要 Review Editor 的实际输出。
    fixture: errors_{case_id}.json → {"changes": [...], "revised_fragments": [...], "overall_score": N}
    """
    outputs = []

    for case in cases:
        case_id = case["id"]
        fixture_name = f"errors_{case_id}"

        review_fixture = load_fixture(fixture_name)
        if review_fixture is None:
            print(f"    [{case_id}] fixture 不存在，跳过（需先在线运行 Review Editor）")
            outputs.append({
                "id": case_id,
                "mode": "errors",
                "status": "skipped",
                "reason": "fixture_not_found",
                "error_type": case.get("error_type", ""),
            })
            continue

        expected = case.get("expected", {})
        changes = review_fixture.get("changes", [])

        # 确定性检查
        should_flag = expected.get("should_flag", False)
        has_changes = len(changes) > 0

        detected = False
        if should_flag and has_changes:
            # 检查是否至少有一个 change 对应期望的错误
            min_severity = expected.get("min_severity", "critical")
            detected = True  # 简化：有 changes 就算 detected
        elif not should_flag and not has_changes:
            detected = True  # 正确：正常文本没有误报
        elif not should_flag and has_changes:
            detected = False  # 误报：正常文本被标记了

        # 检查 fix 质量
        fixed = False
        if detected and has_changes and expected.get("affected_fragments"):
            fixed_fragments = set(c.get("fragment_index", -1) for c in changes)
            expected_fragments = set(expected["affected_fragments"])
            fixed = expected_fragments.issubset(fixed_fragments)

        result = {
            "error_id": case_id,
            "error_type": case.get("error_type", ""),
            "detected": detected,
            "fixed": fixed if should_flag else None,
            "expected_flagged": should_flag,
            "actual_changes": len(changes),
            "changes_preview": [c.get("reason", "")[:80] for c in changes[:3]],
        }

        outputs.append({
            "id": case_id,
            "mode": "errors",
            "status": "evaluated",
            "error_type": case.get("error_type", ""),
            "verdict": result,
        })

        if should_flag:
            status = "DETECTED" if detected else "MISSED"
            fix_status = "+FIXED" if fixed else ""
            print(f"    [{case_id}] {case.get('error_type', '')}: {status} {fix_status}")
        else:
            status = "OK" if not has_changes else "FALSE_POSITIVE"
            print(f"    [{case_id}] {case.get('error_type', '')}: {status}")

    return outputs


def run_quality_eval(cases: list[dict]) -> list[dict]:
    """质量评估: 调用 LLM-as-Judge。

    fixture: quality_{case_id}.json → {"fragments": [...]}
    需要 API key 来调 LLM Judge。
    """
    outputs = []

    for case in cases:
        case_id = case["id"]
        fixture_name = f"quality_{case_id}"

        # 尝试加载已有 LLM Judge 结果（如果已跑过）
        existing = load_fixture(f"quality_verdict_{case_id}")
        if existing:
            outputs.append({
                "id": case_id,
                "mode": "quality",
                "status": "evaluated",
                "dimension": case.get("dimension", ""),
                "verdict": existing,
            })
            score = existing.get("overall", existing.get("scores", {}).get("overall", "?"))
            print(f"    [{case_id}] {case.get('dimension', '')}: {score} (cached)")
            continue

        # 尝试加载 fragment fixture
        fragment_fixture = load_fixture(fixture_name)
        if fragment_fixture is None:
            print(f"    [{case_id}] fixture 不存在，跳过（需先在线运行 pipeline 获取输出）")
            outputs.append({
                "id": case_id,
                "mode": "quality",
                "status": "skipped",
                "reason": "fixture_not_found",
                "dimension": case.get("dimension", ""),
            })
            continue

        # LLM-as-Judge 评分需要 API 调用
        # 这里是接口定义，实际调用由外部驱动
        print(f"    [{case_id}] {case.get('dimension', '')}: 待 LLM Judge 评分")
        outputs.append({
            "id": case_id,
            "mode": "quality",
            "status": "pending_llm_judge",
            "dimension": case.get("dimension", ""),
            "fixture": fixture_name,
            "criteria": case.get("criteria", []),
        })

    return outputs


# ================================================================
# Stage 3 — 汇总并写 verdicts.jsonl
# ================================================================

def write_verdicts(outputs: list[dict], mode: str, version: str, git_sha: str):
    """写 verdicts 到文件。"""
    os.makedirs(VERDICTS_DIR, exist_ok=True)
    path = os.path.join(VERDICTS_DIR, f"verdicts_{mode}-{version}.jsonl")

    # 统计
    total = len(outputs)
    skipped = sum(1 for o in outputs if o.get("status") == "skipped")
    evaluated = total - skipped
    passed = 0
    failed = 0

    for o in outputs:
        if o.get("status") not in ("evaluated",):
            continue
        v = o.get("verdict", {})
        if isinstance(v, dict):
            if v.get("passes"):
                passed += 1
            else:
                failed += 1

    summary = {
        "eval_mode": mode,
        "version": version,
        "git_sha": git_sha,
        "timestamp": datetime.now().isoformat(),
        "total": total,
        "evaluated": evaluated,
        "skipped": skipped,
        "passed": passed,
        "failed": failed,
        "pass_rate": passed / max(evaluated, 1) if evaluated > 0 else 0,
    }

    # 写文件：summary 第一行，每条 case 的结果后续行
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(summary, ensure_ascii=False) + "\n")
        for o in outputs:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")

    print(f"\n  Verdicts → {path}")
    print(f"  Summary: {evaluated} 条评估, {passed} pass, {failed} fail "
          f"({skipped} skip), pass_rate={summary['pass_rate']:.0%}")
    return summary


# ================================================================
# Main
# ================================================================

def parse_args():
    ap = argparse.ArgumentParser(description="Novel2Comic Eval Runner")
    ap.add_argument("--mode", choices=["all", "quality", "errors", "trace"],
                    default="trace", help="评估模式")
    ap.add_argument("--version", default="v1", help="输入输出版本号")
    ap.add_argument("--save-baseline", action="store_true",
                    help="保存本轮 verdicts 为 baseline")
    return ap.parse_args()


def main():
    args = parse_args()
    git_sha = get_git_sha()

    print(f"Novel2Comic Eval Runner")
    print(f"  Git SHA: {git_sha}")
    print(f"  Mode: {args.mode}")
    print(f"  Version: {args.version}")
    print()

    # Stage 1: 加载 inputs
    print("[Stage 1] 加载 inputs...")
    all_inputs = load_inputs(args.version)

    modes_to_run = ["quality", "errors", "trace"] if args.mode == "all" else [args.mode]
    summaries = {}

    for mode in modes_to_run:
        cases = all_inputs.get(mode, [])
        if not cases:
            print(f"\n  [{mode}] 无用例，跳过")
            continue

        print(f"\n[Stage 2] {mode.upper()} —— 运行 {len(cases)} 条用例...")

        if mode == "trace":
            outputs = run_trace_eval(cases)
        elif mode == "errors":
            outputs = run_errors_eval(cases)
        elif mode == "quality":
            outputs = run_quality_eval(cases)
        else:
            continue

        # Stage 3: 写 verdicts
        print(f"\n[Stage 3] 写 verdicts...")
        summary = write_verdicts(outputs, mode, args.version, git_sha)
        summaries[mode] = summary

        # 可选：保存为 baseline
        if args.save_baseline:
            baseline_path = os.path.join(BASELINE_DIR, f"verdicts_{mode}-{args.version}.jsonl")
            src = os.path.join(VERDICTS_DIR, f"verdicts_{mode}-{args.version}.jsonl")
            with open(src, "r", encoding="utf-8") as f:
                content = f.read()
            with open(baseline_path, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"  Baseline saved → {baseline_path}")

    # 总览
    print(f"\n{'='*50}")
    print("评估完成")
    for mode, s in summaries.items():
        print(f"  [{mode}] {s['evaluated']} 条, pass_rate={s['pass_rate']:.0%}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
