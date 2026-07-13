# -*- coding: utf-8 -*-
"""Trace 日志查看工具。

用法:
    python scripts/view_trace.py                    # 概览
    python scripts/view_trace.py --detail N         # 第 N 个 trace 的详情
    python scripts/view_trace.py --turn N           # 只看第 N 轮
    python scripts/view_trace.py --grep "keyword"   # 搜索
"""

import argparse
import json
import re
import os


TRACE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "agent_trace.log")


def extract_traces(content: str) -> list[dict]:
    """从日志文本中提取所有 JSON trace 块。"""
    traces = []
    # 匹配 INFO] { ... 直到匹配的 }
    pattern = re.compile(r'INFO\] (\{.*)', re.DOTALL)
    pos = 0
    while pos < len(content):
        m = pattern.search(content, pos)
        if not m:
            break
        json_start = m.start(1)
        # 括号匹配
        depth = 0
        end = json_start
        for i in range(json_start, min(json_start + 100000, len(content))):
            ch = content[i]
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        try:
            traces.append(json.loads(content[json_start:end]))
        except json.JSONDecodeError:
            pass
        pos = end
    return traces


def print_overview(traces: list[dict]):
    """打印概览。"""
    print(f"共 {len(traces)} 个 trace 条目\n")
    for i, t in enumerate(traces):
        aid = t.get("agent_id", "?")
        turns = t.get("total_turns", 0)
        calls = t.get("total_tool_calls", 0)
        tok = t.get("total_tokens", {})
        total_tok = tok.get("total_tokens", "?")
        success = "✓" if t.get("success") else "✗"
        print(f"[{i}] {success} {aid}: {turns} turns, {calls} tool calls, {total_tok} tokens")
        for turn in t.get("turns", []):
            tn = turn.get("turn", "?")
            tcs = [tc["tool"] for tc in turn.get("tool_calls", [])]
            thinking = (turn.get("thinking", "") or "")[:60]
            final = (turn.get("final_answer", "") or "")[:60]
            msgs = turn.get("messages_snapshot", [])
            sys_count = sum(1 for m in msgs if m["role"] == "system")
            print(f"  T{tn}: {sys_count} sys msgs, tools={tcs}")
            if thinking:
                print(f"       think: {thinking}...")
            if final:
                print(f"       final: {final}...")
        print()


def print_detail(traces: list[dict], idx: int):
    """打印某个 trace 的完整内容。"""
    t = traces[idx]
    print(f"=== [{idx}] {t.get('agent_id')}: {t.get('total_turns')} turns ===\n")

    # 摘要
    tok = t.get("total_tokens", {})
    print(f"Tokens: prompt={tok.get('prompt_tokens','?')}, "
          f"completion={tok.get('completion_tokens','?')}, "
          f"total={tok.get('total_tokens','?')}")
    print(f"Duration: {t.get('total_duration_ms','?')}ms")
    print(f"Success: {t.get('success')}")

    for turn in t.get("turns", []):
        tn = turn.get("turn", "?")
        print(f"\n{'─'*60}")
        print(f"TURN {tn}")
        print(f"{'─'*60}")

        # Messages snapshot: 展示每条 message 的角色和前 200 字符
        msgs = turn.get("messages_snapshot", [])
        print(f"\n[Messages Snapshot: {len(msgs)} messages]")
        for mi, msg in enumerate(msgs):
            role = msg.get("role", "?")
            content = msg.get("content", "")
            preview = content[:200].replace("\n", "\\n")
            extra = ""
            if msg.get("tool_calls"):
                names = [tc.get("function", {}).get("name", "?") for tc in msg["tool_calls"]]
                extra = f" [calls: {', '.join(names)}]"
            if msg.get("tool_call_id"):
                extra = f" [tool_id: {msg['tool_call_id']}]"
            print(f"  [{mi}] {role}{extra}: {preview}...")

        # Thinking
        thinking = turn.get("thinking", "") or ""
        if thinking:
            print(f"\n[Thinking]\n{thinking[:500]}")

        # Tool calls
        tcs = turn.get("tool_calls", [])
        if tcs:
            print(f"\n[Tool Calls: {len(tcs)}]")
            for tc in tcs:
                print(f"  {tc['tool']}({json.dumps(tc.get('input',{}), ensure_ascii=False)})")
                out = tc.get("output", "")[:300]
                print(f"    → {out}")

        # Final answer
        final = turn.get("final_answer", "") or ""
        if final:
            print(f"\n[Final Answer]\n{final[:1000]}")

        print(f"\nTokens: {turn.get('tokens', {})}")
        print(f"Duration: {turn.get('duration_ms', '?')}ms")


def search_traces(traces: list[dict], keyword: str):
    """搜索包含关键词的 trace。"""
    for i, t in enumerate(traces):
        text = json.dumps(t, ensure_ascii=False)
        if keyword.lower() in text.lower():
            # 找到匹配位置并显示上下文
            idx = text.lower().find(keyword.lower())
            ctx = text[max(0, idx - 50):idx + len(keyword) + 100]
            print(f"[trace {i}] ...{ctx}...")


def main():
    parser = argparse.ArgumentParser(description="AgentFlow Trace 日志查看工具")
    parser.add_argument("--detail", type=int, metavar="N", help="查看第 N 个 trace 的详细信息")
    parser.add_argument("--turn", type=int, metavar="N", help="只查看指定 trace 的某一轮")
    parser.add_argument("--grep", type=str, metavar="KW", help="搜索关键词")
    parser.add_argument("--file", type=str, default=TRACE_FILE, help="trace 文件路径")
    parser.add_argument("--last", type=int, default=0, metavar="N", help="只显示最后 N 个 trace")
    args = parser.parse_args()

    if not os.path.exists(args.file):
        print(f"文件不存在: {args.file}")
        return

    with open(args.file, "r", encoding="utf-8") as f:
        content = f.read()

    traces = extract_traces(content)
    if not traces:
        print("未找到 trace 数据")
        return

    if args.last:
        traces = traces[-args.last:]

    if args.grep:
        search_traces(traces, args.grep)
    elif args.detail is not None:
        print_detail(traces, args.detail)
    elif args.turn is not None:
        # 显示所有 trace 的指定轮
        for i, t in enumerate(traces):
            turns = t.get("turns", [])
            if args.turn < len(turns):
                print(f"\n=== [trace {i}] turn {args.turn} ===")
                turn = turns[args.turn]
                msgs = turn.get("messages_snapshot", [])
                for mi, msg in enumerate(msgs):
                    role = msg.get("role", "?")
                    content = msg.get("content", "")[:300].replace("\n", "\\n")
                    print(f"  [{mi}] {role}: {content}...")
    else:
        print_overview(traces)


if __name__ == "__main__":
    main()
