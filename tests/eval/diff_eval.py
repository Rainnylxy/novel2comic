"""
Eval Diff —— 两轮 verdicts 对比，检测回归和新通过。

用法:
  python diff_eval.py --baseline v1 --current v2
  python diff_eval.py --baseline v1 --current v2 --mode trace

输出:
  - 整体 pass rate 变化
  - 回归列表 (was pass → now fail)
  - 新通过列表 (was fail → now pass)
  - Flappy cases 检测 (多次运行不一致)
"""

import json
import os
import sys
import argparse
from typing import Optional

EVAL_DIR = os.path.dirname(os.path.abspath(__file__))
VERDICTS_DIR = os.path.join(EVAL_DIR, "verdicts")
BASELINE_DIR = os.path.join(EVAL_DIR, "baseline")


def load_verdicts(version: str, mode: str, use_baseline: bool = False) -> dict:
    """加载一轮 verdicts 文件。

    Returns:
        {"summary": {...}, "cases": {"case-id": {...}}}
    """
    dir_path = BASELINE_DIR if use_baseline else VERDICTS_DIR
    path = os.path.join(dir_path, f"verdicts_{mode}-{version}.jsonl")

    if not os.path.exists(path):
        return {"summary": None, "cases": {}}

    with open(path, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]

    if not lines:
        return {"summary": None, "cases": {}}

    summary = json.loads(lines[0])
    cases = {}
    for line in lines[1:]:
        case = json.loads(line)
        cases[case["id"]] = case

    return {"summary": summary, "cases": cases}


def case_passed(case: dict) -> Optional[bool]:
    """判断单条 case 是否通过。

    Returns:
        True/False 或 None（无法判定，如 skipped）
    """
    if case.get("status") != "evaluated":
        return None

    verdict = case.get("verdict", {})
    if isinstance(verdict, dict):
        # trace mode
        if "passes" in verdict:
            return verdict["passes"]
        # errors mode
        if "detected" in verdict and "expected_flagged" in verdict:
            if verdict["expected_flagged"]:
                return verdict["detected"]
            else:
                return not verdict.get("false_positive", False)
        # 通用
        return verdict.get("passes", verdict.get("pass", None))

    return None


def diff(baseline_version: str, current_version: str, mode: str) -> dict:
    """对比两轮 verdicts。

    Returns:
        {
          "baseline_version": "v1",
          "current_version": "v2",
          "mode": "trace",
          "baseline_rate": 0.8,
          "current_rate": 0.85,
          "delta": +0.05,
          "regressions": ["t-01"],
          "new_passes": ["t-05"],
          "flappy": [],
          "verdict": "improved|regressed|stable"
        }
    """
    baseline = load_verdicts(baseline_version, mode, use_baseline=True)
    current = load_verdicts(current_version, mode, use_baseline=False)

    # 也尝试从当前 verdicts 目录加载 baseline
    if not baseline["cases"]:
        baseline = load_verdicts(baseline_version, mode, use_baseline=False)

    if not baseline["cases"]:
        print(f"  Baseline {baseline_version} 不存在，无法对比")
        return {
            "baseline_version": baseline_version,
            "current_version": current_version,
            "mode": mode,
            "error": "baseline_not_found",
        }

    if not current["cases"]:
        print(f"  当前版本 {current_version} 的 verdicts 不存在")
        return {
            "baseline_version": baseline_version,
            "current_version": current_version,
            "mode": mode,
            "error": "current_not_found",
        }

    # 逐 case 对比
    regressions = []
    new_passes = []
    flappy = []
    same_status = []

    all_ids = set(baseline["cases"].keys()) | set(current["cases"].keys())

    for case_id in sorted(all_ids):
        b_case = baseline["cases"].get(case_id, {})
        c_case = current["cases"].get(case_id, {})

        b_passed = case_passed(b_case)
        c_passed = case_passed(c_case)

        if b_passed is None or c_passed is None:
            flappy.append({"id": case_id, "baseline": b_passed, "current": c_passed,
                          "reason": "incomplete_data"})
        elif b_passed and not c_passed:
            regressions.append({
                "id": case_id,
                "scenario": b_case.get("scenario", b_case.get("dimension", "")),
                "baseline_status": "pass",
                "current_status": "fail",
            })
        elif not b_passed and c_passed:
            new_passes.append({
                "id": case_id,
                "scenario": c_case.get("scenario", c_case.get("dimension", "")),
                "baseline_status": "fail",
                "current_status": "pass",
            })
        else:
            same_status.append({"id": case_id, "status": "pass" if b_passed else "fail"})

    # 计算 pass rate
    b_evaluated = sum(1 for c in baseline["cases"].values()
                      if case_passed(c) is not None)
    c_evaluated = sum(1 for c in current["cases"].values()
                      if case_passed(c) is not None)
    b_passes = sum(1 for c in baseline["cases"].values()
                   if case_passed(c) is True)
    c_passes = sum(1 for c in current["cases"].values()
                   if case_passed(c) is True)

    baseline_rate = b_passes / max(b_evaluated, 1)
    current_rate = c_passes / max(c_evaluated, 1)
    delta = current_rate - baseline_rate

    # 判定
    if regressions:
        verdict = "regressed"
    elif new_passes:
        verdict = "improved"
    elif delta > 0.01:
        verdict = "improved"
    elif delta < -0.01:
        verdict = "regressed"
    else:
        verdict = "stable"

    result = {
        "baseline_version": baseline_version,
        "current_version": current_version,
        "mode": mode,
        "baseline_rate": round(baseline_rate, 3),
        "current_rate": round(current_rate, 3),
        "delta": round(delta, 3),
        "baseline_passes": f"{b_passes}/{b_evaluated}",
        "current_passes": f"{c_passes}/{c_evaluated}",
        "regressions": regressions,
        "new_passes": new_passes,
        "flappy": flappy,
        "verdict": verdict,
    }

    return result


def format_report(diff_result: dict) -> str:
    """格式化对比报告。"""
    if "error" in diff_result:
        return f"  ERROR: {diff_result['error']}"

    lines = [
        f"",
        f"  Mode: {diff_result['mode']}",
        f"  Baseline ({diff_result['baseline_version']}): "
        f"{diff_result['baseline_rate']:.0%} ({diff_result['baseline_passes']})",
        f"  Current ({diff_result['current_version']}): "
        f"{diff_result['current_rate']:.0%} ({diff_result['current_passes']})",
        f"  Delta: {diff_result['delta']:+.1%}",
        f"  Verdict: {diff_result['verdict'].upper()}",
    ]

    if diff_result["regressions"]:
        lines.append(f"\n  REGRESSIONS ({len(diff_result['regressions'])}):")
        for r in diff_result["regressions"]:
            lines.append(f"    {r['id']}: {r.get('scenario', '?')}")
            lines.append(f"      was PASS → now FAIL")

    if diff_result["new_passes"]:
        lines.append(f"\n  NEW PASSES ({len(diff_result['new_passes'])}):")
        for n in diff_result["new_passes"]:
            lines.append(f"    {n['id']}: {n.get('scenario', '?')}")
            lines.append(f"      was FAIL → now PASS")

    if diff_result["flappy"]:
        lines.append(f"\n  FLAPPY ({len(diff_result['flappy'])}):")
        for f in diff_result["flappy"]:
            lines.append(f"    {f['id']}: incomplete data")

    if not diff_result["regressions"] and not diff_result["new_passes"]:
        lines.append(f"\n  No changes detected. All cases stable.")

    return "\n".join(lines)


def parse_args():
    ap = argparse.ArgumentParser(description="Novel2Comic Eval Diff")
    ap.add_argument("--baseline", required=True, help="Baseline 版本号")
    ap.add_argument("--current", required=True, help="当前版本号")
    ap.add_argument("--mode", choices=["all", "quality", "errors", "trace"],
                    default="all", help="对比模式")
    return ap.parse_args()


def main():
    args = parse_args()
    modes = ["quality", "errors", "trace"] if args.mode == "all" else [args.mode]

    print(f"Eval Diff: {args.baseline} → {args.current}")
    print(f"{'='*50}")

    all_results = {}
    for mode in modes:
        result = diff(args.baseline, args.current, mode)
        all_results[mode] = result
        print(format_report(result))

    # 汇总
    print(f"\n{'='*50}")
    print("Overall:")
    for mode, r in all_results.items():
        if "error" not in r:
            arrow = "↑" if r["delta"] > 0 else "↓" if r["delta"] < 0 else "→"
            print(f"  [{mode}] {r['baseline_rate']:.0%} → {r['current_rate']:.0%} "
                  f"({arrow}{abs(r['delta']):.1%})  [{r['verdict']}]")

    # 保存 diff 结果
    diff_path = os.path.join(EVAL_DIR, "verdicts",
                             f"diff_{args.baseline}_to_{args.current}.json")
    with open(diff_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f"\nDiff saved → {diff_path}")


if __name__ == "__main__":
    main()
