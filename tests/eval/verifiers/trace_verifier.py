"""
Trace Verifier —— 确定性规则检查。

检查 Agent tool call 序列是否符合预期。
不依赖 LLM，纯代码执行。

用法:
    from trace_verifier import TraceVerifier
    v = TraceVerifier()
    result = v.verify(trace_data, expected_spec)
"""

import json
from typing import Any


class TraceVerifier:
    """确定性 Agent trace 校验器。

    检查:
    1. 必需工具是否被调用
    2. 禁止工具是否被调用
    3. 调用顺序是否符合预期
    4. 工具调用参数的合理性
    """

    def __init__(self):
        self.results: list[dict] = []

    def verify(self, trace_data: dict, expected_spec: dict) -> dict:
        """校验单条 trace。

        Args:
            trace_data: agent_trace 的 JSON 输出
                {
                  "agent": "PlotArchitect",
                  "turns": [
                    {"turn": 1, "tool_calls": [{"name": "lookup_roadmap", "args": {...}}], ...},
                    ...
                  ]
                }
            expected_spec: 期望的工具调用规范
                {
                  "expected_tools": ["lookup_roadmap", "update_roadmap"],
                  "forbidden_tools": ["gather_active_conflicts"],
                  "expected_order": ["lookup_roadmap", "update_roadmap"],
                  "reason": "为什么期望这些调用"
                }

        Returns:
            {
              "passes": bool,
              "failures": [{"rule": str, "detail": str, "severity": "error|warn"}]
            }
        """
        failures = []

        # 提取所有被调用的工具名
        called_tools = self._extract_tool_names(trace_data)

        # Check 1: 必需工具是否被调用
        for tool in expected_spec.get("expected_tools", []):
            if tool not in called_tools:
                failures.append({
                    "rule": "required_tool_missing",
                    "tool": tool,
                    "detail": f"期望调用 {tool} 但未调用",
                    "severity": "error",
                })

        # Check 2: 禁止工具是否被调用
        for tool in expected_spec.get("forbidden_tools", []):
            if tool in called_tools:
                failures.append({
                    "rule": "forbidden_tool_called",
                    "tool": tool,
                    "detail": f"不应调用 {tool} 但实际调用了",
                    "severity": "error",
                })

        # Check 3: 调用顺序
        expected_order = expected_spec.get("expected_order", [])
        if expected_order and len(expected_order) > 1:
            order_ok = self._check_order(called_tools, expected_order)
            if not order_ok:
                failures.append({
                    "rule": "tool_order_violation",
                    "detail": f"工具调用顺序不符合预期。期望: {expected_order}，实际: {called_tools}",
                    "severity": "warn",
                })

        # Check 4: 重复调用检测（如 lookup_roadmap 被调用多次）
        duplicates = self._check_duplicates(trace_data)
        for dup in duplicates:
            failures.append({
                "rule": "duplicate_tool_call",
                "tool": dup,
                "detail": f"{dup} 被重复调用超过必要次数",
                "severity": "warn",
            })

        return {
            "passes": len([f for f in failures if f["severity"] == "error"]) == 0,
            "called_tools": called_tools,
            "expected_tools": expected_spec.get("expected_tools", []),
            "failures": failures,
            "reason": expected_spec.get("reason", ""),
        }

    def verify_batch(self, traces: list[dict], specs: list[dict]) -> dict:
        """批量校验。

        Returns:
            {
              "results": [...],
              "summary": {"total": N, "passed": N, "failed": N, "pass_rate": 0.XX},
              "verdict": "pass|fail"
            }
        """
        self.results = []
        for trace, spec in zip(traces, specs):
            result = self.verify(trace, spec)
            self.results.append(result)

        passed = sum(1 for r in self.results if r["passes"])
        total = len(self.results)
        return {
            "results": self.results,
            "summary": {
                "total": total,
                "passed": passed,
                "failed": total - passed,
                "pass_rate": passed / max(total, 1),
            },
            "verdict": "pass" if passed == total else "fail",
        }

    @staticmethod
    def _extract_tool_names(trace_data: dict) -> list[str]:
        """从 trace 中提取所有被调用的工具名（按调用顺序）。"""
        names = []
        turns = trace_data.get("turns", [])
        for turn in turns:
            tool_calls = turn.get("tool_calls", [])
            for tc in tool_calls:
                name = tc.get("name", "")
                if name:
                    names.append(name)
        return names

    @staticmethod
    def _check_order(called: list[str], expected: list[str]) -> bool:
        """检查调用顺序是否符合预期。

        expected 中的 None 表示"任意工具"。
        例如 expected=["lookup_roadmap", None, "verify_character"]
        表示验证角色前必须先查路线图，中间可以有任意其他调用。
        """
        called_idx = 0
        for exp in expected:
            if exp is None:
                called_idx += 1
                continue
            # 找下一个匹配
            found = False
            while called_idx < len(called):
                if called[called_idx] == exp:
                    found = True
                    called_idx += 1
                    break
                called_idx += 1
            if not found:
                return False
        return True

    @staticmethod
    def _check_duplicates(trace_data: dict) -> list[str]:
        """检测不必要的重复工具调用。

        规则: lookup_roadmap 在同一 turn 中不应被调用多次。
        """
        duplicates = []
        turns = trace_data.get("turns", [])
        for turn in turns:
            tool_calls = turn.get("tool_calls", [])
            names = [tc.get("name", "") for tc in tool_calls]
            seen = set()
            for name in names:
                if name in seen and name in ("lookup_roadmap", "lookup_character"):
                    duplicates.append(name)
                seen.add(name)
        return duplicates


# ================================================================
# CLI
# ================================================================

def main():
    """从命令行运行 trace 校验。

    用法:
        python trace_verifier.py trace.json spec.json
        python trace_verifier.py --batch traces.jsonl specs.jsonl
    """
    import sys
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("trace_file", help="Trace JSON 文件")
    ap.add_argument("spec_file", help="期望规范 JSON 文件")
    ap.add_argument("--batch", action="store_true", help="批量模式")
    args = ap.parse_args()

    verifier = TraceVerifier()

    if args.batch:
        with open(args.trace_file, "r", encoding="utf-8") as f:
            traces = [json.loads(line) for line in f if line.strip()]
        with open(args.spec_file, "r", encoding="utf-8") as f:
            specs = [json.loads(line) for line in f if line.strip()]
        result = verifier.verify_batch(traces, specs)
    else:
        with open(args.trace_file, "r", encoding="utf-8") as f:
            trace = json.load(f)
        with open(args.spec_file, "r", encoding="utf-8") as f:
            spec = json.load(f)
        result = verifier.verify(trace, spec)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("verdict", "fail") in ("pass",) else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
