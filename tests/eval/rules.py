# -*- coding: utf-8 -*-
"""规则类评估指标 —— 纯函数，不依赖 LLM 调用。

在 Judge 评分之前运行，产出客观证据注入 Judge prompt。
"""

import re
from typing import Optional


# ================================================================
# 1. 角色死活 check
# ================================================================

def check_character_life(fragments: list, dead_chars: set,
                         missing_chars: set) -> list[dict]:
    """检查续写中是否出现了已死亡/失踪角色的直接出场。

    已死亡角色只能在回忆/闪回中出现，不能有 dialogue/action 类型片段。
    下落不明角色不能直接出场。

    Returns:
        [{"fragment_index": 0, "character": "张三", "type": "dead_character_appeared",
          "detail": "已死亡角色以 dialogue 出场: ..."}]
    """
    violations = []
    for i, f in enumerate(fragments):
        char = f.get("character", "")
        ftype = f.get("type", "")
        text = f.get("text", "")

        if char in dead_chars and ftype in ("dialogue", "action"):
            violations.append({
                "fragment_index": i,
                "character": char,
                "type": "dead_character_appeared",
                "detail": f"已死亡角色 '{char}' 以 {ftype} 出场: {text[:60]}",
            })

        if char in missing_chars and ftype in ("dialogue", "action"):
            violations.append({
                "fragment_index": i,
                "character": char,
                "type": "missing_character_appeared",
                "detail": f"下落不明角色 '{char}' 以 {ftype} 出场: {text[:60]}",
            })

    return violations


# ================================================================
# 2. 禁用词 check
# ================================================================

def check_taboo_words(fragments: list,
                      taboo_map: dict[str, list[str]]) -> list[dict]:
    """检查角色是否使用了禁用词/禁用句式。

    Args:
        taboo_map: {角色名: [禁用词列表]}
    """
    violations = []
    for i, f in enumerate(fragments):
        char = f.get("character", "")
        text = f.get("text", "")
        if not char or char not in taboo_map:
            continue

        for word in taboo_map[char]:
            if word in text:
                violations.append({
                    "fragment_index": i,
                    "character": char,
                    "type": "taboo_word",
                    "detail": f"'{char}' 使用了禁用词 '{word}': {text[:60]}",
                })

    return violations


# ================================================================
# 3. 句法统计
# ================================================================

def calc_syntax_stats(text: str) -> dict:
    """计算续写文本的句法统计，用于和蒸馏档案对比。

    Returns:
        {"avg_sentence_length": float, "total_chars": int, "sentence_count": int}
    """
    # 按中文标点断句
    sentences = re.split(r'[。！？；!?;\n]', text)
    sentences = [s.strip() for s in sentences if len(s.strip()) >= 2]
    total = sum(len(s) for s in sentences)

    return {
        "avg_sentence_length": round(total / len(sentences), 1) if sentences else 0,
        "total_chars": len(text),
        "sentence_count": len(sentences),
    }


# ================================================================
# 4. 片段类型分布
# ================================================================

def calc_fragment_distribution(fragments: list) -> dict:
    """统计 fragment 类型分布。

    Returns:
        {"dialogue": 0.35, "narration": 0.30, "action": 0.20,
         "inner_thought": 0.10, "divider": 0.05}
    """
    counts = {}
    for f in fragments:
        t = f.get("type", "narration")
        counts[t] = counts.get(t, 0) + 1

    total = len(fragments) or 1
    return {k: round(v / total, 2) for k, v in counts.items()}


# ================================================================
# 5. 目标片段数 deviation
# ================================================================

def calc_target_deviation(actual: int, target: int) -> float:
    """计算实际片段数与目标的偏差率。

    Returns:
        0 = 正好, 正值 = 超了, 负值 = 不足
    """
    return round((actual - target) / max(target, 1), 2) if target else 0


# ================================================================
# 汇总
# ================================================================

# ================================================================
# 6. 工具调用效率 check
# ================================================================

def _extract_tool_calls(traces: list) -> list[dict]:
    """从 trace 列表中提取所有 tool_calls。

    trace 格式:
      {"agent": "plot_architect", "turns": [{"turn": 1, "tool_calls": [
        {"name": "lookup_roadmap", "args": {}, "output": "..."}]}]}
    """
    calls = []
    for trace in traces:
        agent = trace.get("agent", "?")
        for turn in trace.get("turns", []):
            for tc in turn.get("tool_calls", []):
                calls.append({
                    "agent": agent,
                    "turn": turn.get("turn", 0),
                    "tool": tc.get("name", "?"),
                    "args": tc.get("args", {}),
                    "output": (tc.get("output", "") or "")[:200],
                })
    return calls


def check_redundant_calls(traces: list) -> list[dict]:
    """检查同一 Agent 对同一角色重复调用 lookup_character。

    同一个角色在同一次 trace 中查询超过 1 次 → 冗余。
    """
    calls = _extract_tool_calls(traces)
    seen = {}  # {(agent, tool, char): [call_indices]}
    violations = []

    for i, call in enumerate(calls):
        if call["tool"] != "lookup_character":
            continue
        char = call["args"].get("name", "")
        if not char:
            continue
        key = (call["agent"], call["tool"], char)
        if key not in seen:
            seen[key] = []
        seen[key].append(i)

    for (agent, tool, char), indices in seen.items():
        if len(indices) > 1:
            violations.append({
                "type": "redundant_lookup",
                "detail": f"[{agent}] 重复查询 '{char}' {len(indices)} 次"
                          f" (turns: {[calls[i]['turn'] for i in indices]})",
            })

    return violations


def check_missed_lookup(traces: list, fragments: list,
                        preloaded_chars: set) -> list[dict]:
    """检查 Writer 是否写了未预加载且未 lookup 的角色。

    preloaded_chars: chapter_plan 中声明的角色，Writer 已知无需查。
    """
    calls = _extract_tool_calls(traces)

    # 收集实际查过的角色
    looked_up = set()
    for call in calls:
        if call["tool"] == "lookup_character":
            char = call["args"].get("name", "")
            if char:
                looked_up.add(char)

    # 收集续写中出场的角色
    appeared = set()
    for f in fragments:
        char = f.get("character", "")
        if char:
            appeared.add(char)

    # 出场但没预加载也没查 = 脑补
    known = preloaded_chars | looked_up
    violations = []
    for char in sorted(appeared - known):
        violations.append({
            "type": "missed_lookup",
            "detail": f"角色 '{char}' 出场但未预加载也未调用 lookup_character（可能脑补）",
        })

    return violations


def check_unnecessary_tools(traces: list, agent: str,
                             unnecessary: list) -> list[dict]:
    """检查是否调用了不必要的工具。

    Args:
        agent: 限定检查的 Agent 名（如 "chapter_writer"）
        unnecessary: 该 Agent 不应调用的工具名列表
    """
    calls = _extract_tool_calls(traces)
    violations = []
    for call in calls:
        if call["agent"] == agent and call["tool"] in unnecessary:
            violations.append({
                "type": "unnecessary_tool",
                "detail": f"[{agent}] 调用了不必要的工具 '{call['tool']}'"
                          f" @ turn {call['turn']}",
            })
    return violations


def run_rule_checks(
    fragments: list,
    character_profiles: Optional[dict] = None,
    dead_chars: Optional[set] = None,
    missing_chars: Optional[set] = None,
    target_paragraphs: int = 0,
    traces: Optional[list] = None,
    preloaded_chars: Optional[set] = None,
) -> dict:
    """运行所有规则检查，返回结构化结果。

    Args:
        fragments: 续写生成的 fragment 列表
        character_profiles: {name: CharacterProfile} 蒸馏档案
        dead_chars: 已知已死亡的角色名集合
        missing_chars: 已知下落不明的角色名集合
        target_paragraphs: 目标片段数
        traces: 已解析的 Agent trace 列表（用于工具效率检查）
        preloaded_chars: 已预加载的角色名集合（来自 chapter_plan）

    Returns:
        {
            "violations": [...],
            "tool_efficiency": {...},
            "stats": {"syntax": {...}, "distribution": {...}, ...},
            "evidence_text": "格式化文本，可注入 Judge prompt"
        }
    """
    violations = []

    # 提取 taboo_map
    taboo_map = {}
    if character_profiles:
        for name, profile in character_profiles.items():
            v = getattr(profile, 'voice', None)
            if v and v.taboo_words:
                taboo_map[name] = v.taboo_words

    # 角色死活
    dead = dead_chars or set()
    missing = missing_chars or set()
    life_violations = check_character_life(fragments, dead, missing)
    violations.extend(life_violations)

    # 禁用词
    taboo_violations = check_taboo_words(fragments, taboo_map)
    violations.extend(taboo_violations)

    # 工具调用效率
    tool_efficiency = {}
    if traces:
        redundant = check_redundant_calls(traces)
        violations.extend(redundant)

        missed = check_missed_lookup(
            traces, fragments,
            preloaded_chars or set(),
        )
        violations.extend(missed)

        # Writer 不应调用 Architect 的专属工具
        unnecessary = check_unnecessary_tools(
            traces, "chapter_writer",
            ["lookup_roadmap", "update_roadmap", "verify_character",
             "gather_active_conflicts"],
        )
        violations.extend(unnecessary)

        total_calls = len(_extract_tool_calls(traces))
        tool_efficiency = {
            "total_tool_calls": total_calls,
            "redundant_count": len(redundant),
            "missed_lookup_count": len(missed),
            "unnecessary_count": len(unnecessary),
        }

    # 统计
    text = _fragments_to_plain_text(fragments)
    syntax = calc_syntax_stats(text)
    distribution = calc_fragment_distribution(fragments)

    stats = {
        "syntax": syntax,
        "distribution": distribution,
        "fragment_count": len(fragments),
        "target_deviation": calc_target_deviation(len(fragments), target_paragraphs),
    }
    if tool_efficiency:
        stats["tool_efficiency"] = tool_efficiency

    # 构建 evidence text
    evidence = _build_evidence_text(violations, stats)

    return {
        "violations": violations,
        "stats": stats,
        "evidence_text": evidence,
    }


def _fragments_to_plain_text(fragments: list) -> str:
    """fragment 列表 → 纯文本（用于句法统计）。"""
    parts = []
    for f in fragments:
        text = f.get("text", "")
        if text:
            parts.append(text)
    return "\n".join(parts)


def _build_evidence_text(violations: list, stats: dict) -> str:
    """构建可注入 Judge prompt 的证据文本。"""
    parts = ["## 规则检测结果（客观参考）"]

    v_count = len(violations)
    if v_count == 0:
        parts.append("- 无规则违规项")
    else:
        parts.append(f"- 共 {v_count} 项违规:")
        for v in violations:
            parts.append(f"  * [{v['type']}] {v['detail']}")

    s = stats
    parts.append(f"\n### 续写统计")
    parts.append(f"- 总片段数: {s.get('fragment_count', 0)}")
    parts.append(f"- 平均句长: {s['syntax']['avg_sentence_length']}字")
    parts.append(f"- 总句数: {s['syntax']['sentence_count']}")
    parts.append(f"- 片段类型分布: {s['distribution']}")

    te = s.get("tool_efficiency")
    if te:
        parts.append(f"\n### 工具调用效率")
        parts.append(f"- 总调用次数: {te.get('total_tool_calls', 0)}")
        parts.append(f"- 冗余调用: {te.get('redundant_count', 0)}")
        parts.append(f"- 漏查角色: {te.get('missed_lookup_count', 0)}")
        parts.append(f"- 不必要调用: {te.get('unnecessary_count', 0)}")

    return "\n".join(parts)
