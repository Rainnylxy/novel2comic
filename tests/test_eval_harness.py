"""Eval Harness 单元测试。

验证 TraceVerifier 确定性检查的正确性。
"""

import json
import os
import sys
import pytest

# 确保 eval 目录可导入
EVAL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval")
if EVAL_DIR not in sys.path:
    sys.path.insert(0, EVAL_DIR)

from verifiers.trace_verifier import TraceVerifier


class TestTraceVerifier:
    """TraceVerifier 确定性规则检查。"""

    def setup_method(self):
        self.verifier = TraceVerifier()

    def test_all_required_tools_called(self):
        """所有必需工具都被调用 → pass。"""
        trace = {
            "agent": "PlotArchitect",
            "turns": [
                {"turn": 1, "tool_calls": [
                    {"name": "lookup_roadmap", "args": {}}
                ]},
                {"turn": 2, "tool_calls": [
                    {"name": "update_roadmap", "args": {"roadmap_json": "{}"}}
                ]},
            ],
        }
        spec = {
            "expected_tools": ["lookup_roadmap", "update_roadmap"],
            "expected_order": ["lookup_roadmap", "update_roadmap"],
        }
        result = self.verifier.verify(trace, spec)
        assert result["passes"] is True
        assert len(result["failures"]) == 0

    def test_missing_required_tool(self):
        """缺少必需工具 → fail。"""
        trace = {
            "agent": "PlotArchitect",
            "turns": [
                {"turn": 1, "tool_calls": [
                    {"name": "update_roadmap", "args": {}}
                ]},
            ],
        }
        spec = {
            "expected_tools": ["lookup_roadmap", "update_roadmap"],
        }
        result = self.verifier.verify(trace, spec)
        assert result["passes"] is False
        assert any(f["rule"] == "required_tool_missing" for f in result["failures"])

    def test_forbidden_tool_called(self):
        """调用了禁止工具 → fail。"""
        trace = {
            "agent": "PlotArchitect",
            "turns": [
                {"turn": 1, "tool_calls": [
                    {"name": "lookup_roadmap", "args": {}},
                    {"name": "update_roadmap", "args": {}},
                ]},
            ],
        }
        spec = {
            "expected_tools": ["lookup_roadmap"],
            "forbidden_tools": ["update_roadmap"],
        }
        result = self.verifier.verify(trace, spec)
        assert result["passes"] is False
        assert any(f["rule"] == "forbidden_tool_called" for f in result["failures"])

    def test_order_violation(self):
        """调用顺序错误 → warn（不影响 passes）。"""
        trace = {
            "agent": "PlotArchitect",
            "turns": [
                {"turn": 1, "tool_calls": [
                    {"name": "update_roadmap", "args": {}}
                ]},
                {"turn": 2, "tool_calls": [
                    {"name": "lookup_roadmap", "args": {}}
                ]},
            ],
        }
        spec = {
            "expected_tools": ["lookup_roadmap", "update_roadmap"],
            "expected_order": ["lookup_roadmap", "update_roadmap"],
        }
        result = self.verifier.verify(trace, spec)
        # 顺序违规是 warn，不阻断 passes
        assert any(f["rule"] == "tool_order_violation" for f in result["failures"])

    def test_duplicate_detection(self):
        """重复调用 lookup_roadmap → warn。"""
        trace = {
            "agent": "PlotArchitect",
            "turns": [
                {"turn": 1, "tool_calls": [
                    {"name": "lookup_roadmap", "args": {}},
                    {"name": "lookup_roadmap", "args": {}},
                ]},
            ],
        }
        spec = {
            "expected_tools": ["lookup_roadmap"],
        }
        result = self.verifier.verify(trace, spec)
        assert any(f["rule"] == "duplicate_tool_call" for f in result["failures"])

    def test_order_with_none_wildcard(self):
        """expected_order 中的 None 表示任意工具。"""
        trace = {
            "agent": "PlotArchitect",
            "turns": [
                {"turn": 1, "tool_calls": [
                    {"name": "lookup_roadmap", "args": {}}
                ]},
                {"turn": 2, "tool_calls": [
                    {"name": "gather_active_conflicts", "args": {}}
                ]},
                {"turn": 3, "tool_calls": [
                    {"name": "verify_character", "args": {"name": "江停"}}
                ]},
            ],
        }
        spec = {
            "expected_tools": ["lookup_roadmap", "verify_character"],
            "expected_order": ["lookup_roadmap", None, "verify_character"],
        }
        result = self.verifier.verify(trace, spec)
        # 顺序合法：lookup_roadmap → [任意] → verify_character
        order_violations = [f for f in result["failures"]
                           if f["rule"] == "tool_order_violation"]
        assert len(order_violations) == 0

    def test_batch_verify(self):
        """批量校验。"""
        traces = [
            {"agent": "A", "turns": [
                {"turn": 1, "tool_calls": [{"name": "lookup_roadmap"}]}
            ]},
            {"agent": "B", "turns": [
                {"turn": 1, "tool_calls": []}
            ]},
        ]
        specs = [
            {"expected_tools": ["lookup_roadmap"]},
            {"expected_tools": ["lookup_roadmap"]},
        ]
        result = self.verifier.verify_batch(traces, specs)
        assert result["summary"]["total"] == 2
        assert result["summary"]["passed"] == 1
        assert result["summary"]["failed"] == 1
        assert result["verdict"] == "fail"

    def test_extract_tool_names_empty(self):
        """空 trace → 空工具列表。"""
        trace = {"agent": "Test", "turns": []}
        names = self.verifier._extract_tool_names(trace)
        assert names == []

    def test_verify_with_reason(self):
        """验证输出包含 reason 字段。"""
        trace = {"agent": "Test", "turns": [
            {"turn": 1, "tool_calls": [{"name": "lookup_roadmap"}]}
        ]}
        spec = {
            "expected_tools": ["lookup_roadmap"],
            "reason": "这是测试原因",
        }
        result = self.verifier.verify(trace, spec)
        assert result["reason"] == "这是测试原因"
