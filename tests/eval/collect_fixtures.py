"""
Fixture 自动采集脚本 —— 从真实 Pipeline 运行输出中提取 eval 所需数据。

每次跑完 Pipeline 后运行此脚本，自动生成所有 fixture 文件。
无需人工构造任何内容。

用法:
    # 从项目目录采集
    python collect_fixtures.py --project-dir src/projects/poyun_20260706_102428

    # 从独立 log 文件采集
    python collect_fixtures.py --trace-log agent_trace.log --chapter-dir src/projects/xxx

输出:
    tests/eval/fixtures/
    ├── trace_{case-id}.json       # 从 agent_trace.log 提取的工具调用序列
    ├── errors_{case-id}.json      # 从 Pipeline review 提取的审校结果
    └── quality_{case-id}.json     # 从 Pipeline complete 提取的片段输出
"""

import json
import os
import re
import sys
import argparse
from typing import Optional

EVAL_DIR = os.path.dirname(os.path.abspath(__file__))
FIXTURES_DIR = os.path.join(EVAL_DIR, "fixtures")


def parse_agent_trace_log(log_path: str) -> list[dict]:
    """解析 agent_trace.log，提取每个 Agent 每次调用的工具序列。

    agent_trace.log 格式: 每个 entry 以 "HH:MM:SS [LEVEL] {" 开头，
    JSON body 跨多行，以独立行 "}" 结束。
    JSON body 包含: agent_id, total_turns, turns: [{tool_calls: [{tool, input, output}]}]

    Returns:
        [{"agent": "plot_architect", "turns": [{"turn": 1, "tool_calls": [...]}]}]
    """
    if not os.path.exists(log_path):
        print(f"  [WARN] agent_trace.log 不存在: {log_path}")
        return []

    with open(log_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    # 收集每个 JSON block：遇到时间戳行开始，收集直到遇到下个时间戳或 EOF
    traces = []
    current_block_lines = []
    in_block = False

    timestamp_re = re.compile(r'^\d{2}:\d{2}:\d{2}\s+\[(?:INFO|DEBUG|WARN|ERROR)\]\s+\{')

    for line in lines:
        if timestamp_re.match(line):
            # 开始新 block，先处理上一个 block
            if in_block and current_block_lines:
                _try_parse_trace_block(current_block_lines, traces)
            # 取出 JSON 开头部分（去掉时间戳前缀）
            m = re.match(r'^[\d:.]+ \[(?:INFO|DEBUG|WARN|ERROR)\]\s+', line)
            json_line = line[m.end():] if m else line
            current_block_lines = [json_line]
            in_block = True
        elif in_block:
            current_block_lines.append(line)

    # 处理最后一个 block
    if in_block and current_block_lines:
        _try_parse_trace_block(current_block_lines, traces)

    print(f"  [trace] 从 {log_path} 提取 {len(traces)} 条 Agent 调用记录")
    return traces


def _try_parse_trace_block(lines: list[str], traces: list):
    """尝试将多行文本解析为 trace JSON。"""
    text = "".join(lines)
    # 找 JSON 结束位置：可能是独立的 "}" 行
    try:
        entry = json.loads(text)
    except json.JSONDecodeError:
        # 尝试找最后一个完整的 JSON 对象
        # 去掉尾部非 JSON 内容
        idx = text.rfind('}')
        if idx >= 0:
            try:
                entry = json.loads(text[:idx + 1])
            except json.JSONDecodeError:
                return
        else:
            return

    if "agent_id" not in entry or "turns" not in entry:
        return

    agent_name = entry.get("agent_id", "unknown")
    turns = entry.get("turns", [])

    simplified_turns = []
    for t in turns:
        tool_calls_raw = t.get("tool_calls", [])
        tool_calls = []
        for tc in tool_calls_raw:
            tool_name = tc.get("tool", tc.get("name", "?"))
            tool_args = tc.get("input", tc.get("args", {}))
            tool_calls.append({
                "name": tool_name,
                "args": tool_args,
            })
        simplified_turns.append({
            "turn": t.get("turn", len(simplified_turns) + 1),
            "thought": (t.get("thinking", t.get("reasoning", "")) or "")[:200],
            "tool_calls": tool_calls,
        })

    traces.append({
        "agent": agent_name,
        "turns": simplified_turns,
    })


def extract_trace_fixtures(traces: list[dict]) -> dict[str, dict]:
    """从 trace 列表中提取每个可评估的调用序列。

    按场景分类:
      - "首次规划-无路线图" → 第一次调用时无 roadmap 的情况
      - "有路线图-直接规划" → 有 roadmap 时的标准规划流程
      - "写角色前先查档案" → Writer 调用 lookup_character 的情况
      - "写高潮节前查伏笔" → Writer 调用 recall_foreshadowing 的情况

    Returns:
        {"t-01": {...}, "t-02": {...}, ...}
    """
    fixtures = {}

    arch_traces = [t for t in traces if "architect" in t["agent"].lower()]
    writer_traces = [t for t in traces if "writer" in t["agent"].lower()]

    # Architect traces
    for i, trace in enumerate(arch_traces):
        tool_names = []
        for turn in trace["turns"]:
            for tc in turn["tool_calls"]:
                tool_names.append(tc["name"])

        # 判断场景
        has_lookup_roadmap = "lookup_roadmap" in tool_names
        has_update_roadmap = "update_roadmap" in tool_names
        has_verify = "verify_character" in tool_names

        if has_lookup_roadmap and has_update_roadmap:
            fixture_id = f"t_arch_{i+1:02d}"
            fixtures[fixture_id] = trace
            print(f"    {fixture_id}: 首次规划-创建路线图 ({len(tool_names)} tool calls)")
        elif has_lookup_roadmap and has_verify:
            fixture_id = f"t_arch_{i+1:02d}"
            fixtures[fixture_id] = trace
            print(f"    {fixture_id}: 有路线图-验证角色 ({len(tool_names)} tool calls)")

    # Writer traces
    for i, trace in enumerate(writer_traces):
        tool_names = []
        for turn in trace["turns"]:
            for tc in turn["tool_calls"]:
                tool_names.append(tc["name"])

        has_lookup_char = "lookup_character" in tool_names
        has_recall = "recall_foreshadowing" in tool_names

        if has_lookup_char:
            fixture_id = f"t_writer_{i+1:02d}"
            fixtures[fixture_id] = trace
            extras = "+ foreshadowing" if has_recall else ""
            print(f"    {fixture_id}: Writer查角色档案{extras} ({len(tool_names)} tool calls)")

    return fixtures


def extract_review_fixtures(project_dir: str,
                            chapter_numbers: list[int] = None) -> dict[str, dict]:
    """从 Pipeline 输出的 chapter JSON 中提取 Review Editor 的审校结果。

    每个 chapter_{num}.json 包含 review 阶段的 changes 和 revised_fragments。

    Returns:
        {"e_001": {"changes": [...], "revised_fragments": [...], "overall_score": N}}
    """
    if not os.path.isdir(project_dir):
        print(f"  [WARN] 项目目录不存在: {project_dir}")
        return {}

    fixtures = {}
    # 找到所有 chapter 文件
    ch_files = sorted([
        f for f in os.listdir(project_dir)
        if f.startswith("chapter_") and f.endswith(".json")
    ])

    if chapter_numbers is None and ch_files:
        # 取最新的 3 章
        ch_files = ch_files[-3:]

    for ch_file in ch_files:
        path = os.path.join(project_dir, ch_file)
        try:
            with open(path, "r", encoding="utf-8") as f:
                chapter_data = json.load(f)
        except Exception:
            continue

        # 提取审校相关数据
        review_data = chapter_data.get("review", {})
        if not review_data:
            continue

        ch_num = chapter_data.get("chapter_number", "?")
        fixture_id = f"e_ch{ch_num}"
        fixtures[fixture_id] = review_data
        changes_count = len(review_data.get("changes", []))
        print(f"    {fixture_id}: {changes_count} 处修改, "
              f"评分={review_data.get('overall_score', '?')}")

    return fixtures


def extract_quality_fixtures(project_dir: str,
                             chapter_numbers: list[int] = None) -> dict[str, dict]:
    """从 Pipeline 输出中提取续写片段，供质量评分使用。

    Returns:
        {"q_001": {"fragments": [...], "chapter_plan": {...}, "context": {...}}}
    """
    if not os.path.isdir(project_dir):
        return {}

    fixtures = {}
    ch_files = sorted([
        f for f in os.listdir(project_dir)
        if f.startswith("chapter_") and f.endswith(".json")
    ])

    if chapter_numbers is None and ch_files:
        ch_files = ch_files[-3:]

    for ch_file in ch_files:
        path = os.path.join(project_dir, ch_file)
        try:
            with open(path, "r", encoding="utf-8") as f:
                chapter_data = json.load(f)
        except Exception:
            continue

        ch_num = chapter_data.get("chapter_number", "?")

        # 提取片段（_save_chapter_full 写入 fragments 字段）
        fragments = chapter_data.get("fragments",
                                     chapter_data.get("revised_fragments", []))
        # 规划字段在顶层（title/synopsis/sections 等），提取为 chapter_plan
        chapter_plan = {
            k: v for k, v in chapter_data.items()
            if k not in ("fragments", "fragment_count", "review")
        }

        if not fragments:
            continue

        fixture_id = f"q_ch{ch_num}"
        fixtures[fixture_id] = {
            "chapter_number": ch_num,
            "title": chapter_plan.get("title", ""),
            "synopsis": chapter_plan.get("synopsis", ""),
            "fragments": fragments,
            "fragment_count": len(fragments),
        }
        print(f"    {fixture_id}: {len(fragments)} 个片段, "
              f"「{chapter_plan.get('title', '?')}」")

    return fixtures


def extract_full_context(project_dir: str) -> Optional[dict]:
    """提取完整上下文：角色档案、文风、路线图。

    这些是 quality eval 中 LLM Judge 需要的背景信息。
    """
    context = {}

    # 角色档案
    char_path = os.path.join(project_dir, "character_profiles.json")
    if os.path.exists(char_path):
        with open(char_path, "r", encoding="utf-8") as f:
            context["character_profiles"] = json.load(f)
    else:
        context["character_profiles"] = {}

    # 文风
    style_path = os.path.join(project_dir, "author_style_profile.json")
    if os.path.exists(style_path):
        with open(style_path, "r", encoding="utf-8") as f:
            context["style_profile"] = json.load(f)

    # 路线图
    roadmap_path = os.path.join(project_dir, "roadmap.json")
    if os.path.exists(roadmap_path):
        with open(roadmap_path, "r", encoding="utf-8") as f:
            context["roadmap"] = json.load(f)

    # 状态修正
    fixes_path = os.path.join(project_dir, "status_fixes.json")
    if os.path.exists(fixes_path):
        with open(fixes_path, "r", encoding="utf-8") as f:
            context["status_fixes"] = json.load(f)

    return context if context else None


def main():
    ap = argparse.ArgumentParser(
        description="Fixture 自动采集 —— 从 Pipeline 运行输出中提取 eval 数据"
    )
    ap.add_argument("--project-dir", default="",
                    help="项目目录 (如 src/projects/poyun_20260706_102428)")
    ap.add_argument("--trace-log", default="agent_trace.log",
                    help="agent_trace.log 路径")
    ap.add_argument("--no-trace", action="store_true",
                    help="跳过 trace 日志解析")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印，不写文件")
    args = ap.parse_args()

    project_dir = args.project_dir
    if project_dir and not os.path.isabs(project_dir):
        # 相对于项目根目录
        project_dir = os.path.abspath(
            os.path.join(os.path.dirname(EVAL_DIR), "..", project_dir)
        )

    os.makedirs(FIXTURES_DIR, exist_ok=True)

    print("Fixture 自动采集")
    print(f"  Project dir: {project_dir or '(未指定)'}")
    print(f"  Trace log: {args.trace_log}")
    print()

    # 1. 解析 agent_trace.log → trace fixtures
    if not args.no_trace:
        print("[1/3] 解析 Agent Trace...")
        traces = parse_agent_trace_log(args.trace_log)
        if traces:
            trace_fixtures = extract_trace_fixtures(traces)
            for fid, data in trace_fixtures.items():
                if not args.dry_run:
                    _save_fixture(f"trace_{fid}", data)
        else:
            print("  无可用 trace 数据（agent_trace.log 为空或不存在）")
            print("  提示: 运行 pipeline 后 AgentFlow 自动生成 agent_trace.log")

    # 2. 提取审校结果 → error fixtures
    if project_dir:
        print("\n[2/3] 提取审校结果...")
        review_fixtures = extract_review_fixtures(project_dir)
        for fid, data in review_fixtures.items():
            if not args.dry_run:
                _save_fixture(f"errors_{fid}", data)

        # 3. 提取续写片段 → quality fixtures
        print("\n[3/3] 提取续写片段...")
        quality_fixtures = extract_quality_fixtures(project_dir)
        for fid, data in quality_fixtures.items():
            if not args.dry_run:
                _save_fixture(f"quality_{fid}", data)

        # 4. 提取完整上下文
        context = extract_full_context(project_dir)
        if context and not args.dry_run:
            _save_fixture("context", context)
            print(f"\n  完整上下文已保存 ("
                  f"{len(context.get('character_profiles', {}))} 角色, "
                  f"{len(context.get('roadmap', {}).get('milestones', []))} 里程碑)")

    else:
        print("\n[2/3] 跳过（未指定 --project-dir）")
        print("[3/3] 跳过（未指定 --project-dir）")

    print(f"\n完成 → {FIXTURES_DIR}")
    _list_fixtures()


def _save_fixture(name: str, data: dict):
    """保存 fixture 到文件。"""
    path = os.path.join(FIXTURES_DIR, f"{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _list_fixtures():
    """列出当前所有 fixture 文件。"""
    if not os.path.isdir(FIXTURES_DIR):
        return
    files = sorted(os.listdir(FIXTURES_DIR))
    if files:
        print("当前 fixtures:")
        for fn in files:
            size = os.path.getsize(os.path.join(FIXTURES_DIR, fn))
            print(f"  {fn} ({size:,} bytes)")


if __name__ == "__main__":
    main()
