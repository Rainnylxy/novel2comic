# -*- coding: utf-8 -*-
"""知识图谱可视化服务 —— 生成交互式 HTML 文件。

用法:
    from novel2comic.src.services.graph_viz import KnowledgeGraphVisualizer
    viz = KnowledgeGraphVisualizer()
    viz.render(graph, "output/kg_view.html")
    # 然后用浏览器打开 kg_view.html
"""

import json
import os
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from novel2comic.src.models import StoryGraph

# ============================================================
# 节点 / 边的视觉配置
# ============================================================

NODE_CONFIG = {
    "person": {
        "color": {"background": "#4A90D9", "border": "#2E6BB5", "highlight": {"background": "#6BB5FF"}},
        "shape": "dot", "size": 25,
        "label_cn": "人物",
    },
    "event": {
        "color": {"background": "#E8645A", "border": "#C0392B", "highlight": {"background": "#FF7B6E"}},
        "shape": "diamond", "size": 20,
        "label_cn": "事件",
    },
    "location": {
        "color": {"background": "#5BBF5B", "border": "#3A8A3A", "highlight": {"background": "#78D478"}},
        "shape": "triangle", "size": 18,
        "label_cn": "地点",
    },
    "org": {
        "color": {"background": "#F0A050", "border": "#C07830", "highlight": {"background": "#FFBB66"}},
        "shape": "square", "size": 22,
        "label_cn": "组织",
    },
    "item": {
        "color": {"background": "#9B59B6", "border": "#7D3C98", "highlight": {"background": "#BB77D6"}},
        "shape": "star", "size": 16,
        "label_cn": "物品",
    },
    "chapter": {
        "color": {"background": "#95A5A6", "border": "#7F8C8D", "highlight": {"background": "#B0BEC5"}},
        "shape": "box", "size": 14,
        "label_cn": "章节",
    },
}

EDGE_CONFIG = {
    "relationship":  {"color": "#4A90D9", "dashes": False, "width": 2, "label_cn": "人物关系"},
    "appears_in":    {"color": "#95A5A6", "dashes": True,  "width": 1, "label_cn": "出场"},
    "participates":  {"color": "#E8645A", "dashes": False, "width": 1.5, "label_cn": "参与事件"},
    "occurs_in":     {"color": "#E8645A", "dashes": True,  "width": 1, "label_cn": "发生于章节"},
    "located_at":    {"color": "#5BBF5B", "dashes": True,  "width": 1, "label_cn": "发生于地点"},
    "belongs_to":    {"color": "#F0A050", "dashes": False, "width": 1.5, "label_cn": "归属关系"},
    "owns":          {"color": "#9B59B6", "dashes": False, "width": 1, "label_cn": "拥有物品"},
    "event_relation":{"color": "#E8645A", "dashes": True,  "width": 1, "label_cn": "事件关联"},
    "location_hierarchy":{"color": "#5BBF5B", "dashes": True,  "width": 1, "label_cn": "地点层级"},
}

# ============================================================
# HTML 模板
# ============================================================

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Novel2Comic 知识图谱</title>
<script src="https://unpkg.com/vis-network@9.1.6/standalone/umd/vis-network.min.js"></script>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family: "Microsoft YaHei", "PingFang SC", sans-serif; background:#1a1a2e; color:#eee; }
#header { padding:12px 20px; background:#16213e; display:flex; align-items:center; gap:20px; flex-wrap:wrap; }
#header h1 { font-size:20px; color:#e94560; }
#header .stats { font-size:13px; color:#8899aa; }
#main { display:flex; height:calc(100vh - 56px); }
#sidebar { width:320px; background:#16213e; overflow-y:auto; padding:16px; border-right:1px solid #2a2a4a; }
#sidebar h2 { font-size:16px; margin-bottom:12px; color:#e94560; }
#legend { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:16px; }
#legend .tag { padding:4px 10px; border-radius:12px; font-size:12px; cursor:pointer; opacity:0.7; transition:opacity .2s; border:2px solid transparent; }
#legend .tag.active { opacity:1; border-color:#fff; }
#node-detail { font-size:13px; line-height:1.6; }
#node-detail h3 { font-size:18px; margin-bottom:8px; }
#node-detail .field { margin:4px 0; }
#node-detail .field-key { color:#8899aa; }
#controls { margin-bottom:12px; }
#controls button { padding:6px 14px; margin-right:6px; margin-bottom:6px; background:#2a2a4a; color:#ccc; border:1px solid #444; border-radius:4px; cursor:pointer; font-size:12px; }
#controls button:hover { background:#3a3a5a; }
#mynetwork { flex:1; }
</style>
</head>
<body>
<div id="header">
  <h1>Novel2Comic 知识图谱</h1>
  <span class="stats" id="stats"></span>
</div>
<div id="main">
  <div id="sidebar">
    <div id="controls">
      <button onclick="resetView()">重置视图</button>
      <button onclick="togglePhysics()">物理引擎</button>
    </div>
    <h2>图例</h2>
    <div id="legend"></div>
    <h2>节点详情</h2>
    <div id="node-detail"><em>点击节点查看详情</em></div>
  </div>
  <div id="mynetwork"></div>
</div>

<script>
// 数据
const graphData = __GRAPH_DATA__;

// 节点配置
const nodeConfig = __NODE_CONFIG__;
const edgeConfig = __EDGE_CONFIG__;

// 构建 legend
const legendEl = document.getElementById('legend');
const visibleTypes = new Set();
for (const [type, cfg] of Object.entries(nodeConfig)) {{
  visibleTypes.add(type);
  const tag = document.createElement('span');
  tag.className = 'tag active';
  tag.id = 'legend-' + type;
  tag.style.background = cfg.color.background;
  tag.style.color = '#fff';
  tag.textContent = cfg.label_cn || type;
  tag.onclick = () => toggleType(type);
  legendEl.appendChild(tag);
}}

// 应用节点/边样式
graphData.nodes.forEach(n => {{
  const cfg = nodeConfig[n.nodeType] || nodeConfig['person'];
  n.color = cfg.color;
  n.shape = cfg.shape;
  n.size = cfg.size;
  n.font = {{ color:'#ddd', size:12, face:'Microsoft YaHei' }};
}});
graphData.edges.forEach(e => {{
  const cfg = edgeConfig[e.edgeType] || edgeConfig['relationship'];
  e.color = {{ color:cfg.color, highlight:cfg.color, hover:cfg.color }};
  e.dashes = cfg.dashes;
  e.width = cfg.width;
  e.label = cfg.label_cn || '';
  e.font = {{ color:'#8899aa', size:10, face:'Microsoft YaHei', background:'#1a1a2e' }};
  e.arrows = 'to';
}});

// 容器
const container = document.getElementById('mynetwork');

// 选项
const options = {{
  nodes: {{ borderWidth:2, shadow:{{ enabled:true, size:6 }} }},
  edges: {{ smooth:{{ type:'continuous' }}, arrows:{{ to:{{ scaleFactor:0.5 }} }} }},
  physics: {{
    solver:'forceAtlas2Based',
    forceAtlas2Based:{{ gravitationalConstant:-40, centralGravity:0.005, springLength:150, springConstant:0.08 }},
    stabilization:{{ iterations:200 }},
  }},
  interaction: {{ hover:true, tooltipDelay:100, zoomView:true, dragView:true }},
  layout: {{ improvedLayout:true }},
}};

const network = new vis.Network(container, graphData, options);

// 统计
document.getElementById('stats').textContent =
  `节点: ${{graphData.nodes.length}} | 边: ${{graphData.edges.length}}`;

// 点击节点显示详情
network.on('click', function(params) {{
  const detailEl = document.getElementById('node-detail');
  if (params.nodes.length > 0) {{
    const nodeId = params.nodes[0];
    const node = graphData.nodes.find(n => n.id === nodeId);
    if (node && node.detail) {{
      let html = `<h3>${{node.label}}</h3>`;
      html += `<div class="field"><span class="field-key">类型:</span> ${{nodeConfig[node.nodeType]?.label_cn || node.nodeType}}</div>`;
      for (const [k, v] of Object.entries(node.detail)) {{
        if (v !== null && v !== '' && k !== 'name') {{
          html += `<div class="field"><span class="field-key">${{k}}:</span> ${{typeof v === 'object' ? JSON.stringify(v) : v}}</div>`;
        }}
      }}
      detailEl.innerHTML = html;
    }}
  }} else {{
    detailEl.innerHTML = '<em>点击节点查看详情</em>';
  }}
}});

// 类型过滤
function toggleType(type) {{
  const tag = document.getElementById('legend-' + type);
  if (visibleTypes.has(type)) {{
    visibleTypes.delete(type);
    tag.classList.remove('active');
  }} else {{
    visibleTypes.add(type);
    tag.classList.add('active');
  }}
  const nodes = graphData.nodes.map(n => ({{
    ...n,
    hidden: !visibleTypes.has(n.nodeType),
  }}));
  network.setData({{ nodes, edges:graphData.edges }});
}}

// 其他控制
function resetView() {{
  network.fit({{ animation:true }});
  visibleTypes.clear();
  document.querySelectorAll('#legend .tag').forEach(t => {{
    t.classList.add('active');
    visibleTypes.add(t.id.replace('legend-', ''));
  }});
  const nodes = graphData.nodes.map(n => ({{...n, hidden:false}}));
  network.setData({{ nodes, edges:graphData.edges }});
}}

let physicsOn = true;
function togglePhysics() {{
  physicsOn = !physicsOn;
  network.setOptions({{ physics: physicsOn }});
}}
</script>
</body>
</html>
"""


# ============================================================
# KnowledgeGraphVisualizer
# ============================================================

class KnowledgeGraphVisualizer:
    """知识图谱可视化器。

    将 StoryGraph 转换为 vis-network 的 JSON 格式，生成交互式 HTML。
    """

    def __init__(self):
        pass

    def to_vis_data(self, graph: "StoryGraph") -> dict:
        """将 StoryGraph 转换为 vis-network 兼容的 {nodes, edges}。

        Returns:
            {"nodes": [...], "edges": [...]}
        """
        nodes = []
        edges = []

        # ── 人物节点 ──
        for p in graph.person_nodes:
            node_id = f"person:{p.name}"
            nodes.append({
                "id": node_id, "label": p.name, "nodeType": "person",
                "detail": {
                    "名称": p.name, "角色": p.role_type, "派系": p.faction,
                    "重要度": p.importance, "状态": p.status,
                    "首次出场": f"第{p.first_appearance_chapter}章",
                    "描述": p.description,
                },
            })

        # ── 事件节点 ──
        for e in graph.event_nodes:
            node_id = f"event:{e.name}"
            nodes.append({
                "id": node_id, "label": e.name[:20], "nodeType": "event",
                "detail": {
                    "名称": e.name, "类型": e.event_type,
                    "章节": f"第{e.chapter_start}-{e.chapter_end or e.chapter_start}章",
                    "地点": e.location, "重要性": e.importance,
                    "原因": e.cause, "结果": e.effect,
                    "摘要": e.summary,
                    "参与者": json.dumps(e.participants, ensure_ascii=False) if e.participants else "",
                },
            })

        # ── 地点节点 ──
        for loc in graph.location_nodes:
            node_id = f"location:{loc.name}"
            nodes.append({
                "id": node_id, "label": loc.name, "nodeType": "location",
                "detail": {
                    "名称": loc.name, "类型": loc.location_type,
                    "父级": loc.parent, "首次出现": f"第{loc.first_appear_chapter}章",
                    "势力": ", ".join(loc.factions) if loc.factions else "",
                    "描述": loc.description, "已毁": loc.is_destroyed,
                },
            })

        # ── 组织节点 ──
        for org in graph.organization_nodes:
            node_id = f"org:{org.name}"
            nodes.append({
                "id": node_id, "label": org.name, "nodeType": "org",
                "detail": {
                    "名称": org.name, "类型": org.org_type,
                    "首领": ", ".join(org.leader) if org.leader else "",
                    "基地": org.base, "状态": org.status,
                    "描述": org.description,
                },
            })

        # ── 物品节点 ──
        for item in graph.item_nodes:
            node_id = f"item:{item.name}"
            nodes.append({
                "id": node_id, "label": item.name, "nodeType": "item",
                "detail": {
                    "名称": item.name, "类型": item.item_type,
                    "品阶": item.grade, "来源": item.source,
                    "能力": ", ".join(item.abilities) if item.abilities else "",
                    "描述": item.description,
                },
            })

        # ── 章节节点 ──
        for ch in graph.chapter_nodes:
            node_id = f"chapter:{ch.index}"
            nodes.append({
                "id": node_id, "label": f"第{ch.index}章", "nodeType": "chapter",
                "detail": {
                    "标题": ch.title, "章节编号": ch.index,
                    "字数": ch.word_count,
                    "关键事件": ", ".join(ch.key_events) if ch.key_events else "",
                    "出场角色": ", ".join(ch.appearing_characters) if ch.appearing_characters else "",
                    "摘要": ch.summary,
                },
            })

        # ── 边 ──
        # Relationship
        for e in graph.relationship_edges:
            edges.append({
                "from": f"person:{e.from_char}", "to": f"person:{e.to_char}",
                "edgeType": "relationship",
                "title": f"{e.relation_type}" +
                         (f" 亲密度:{e.intimacy:+d}" if e.intimacy else "") +
                         (f" ({e.current_tension})" if e.current_tension else "") +
                         (f" 权力:{e.power_dynamic}" if e.power_dynamic else ""),
            })

        # AppearsIn
        for e in graph.appears_in_edges:
            edges.append({
                "from": f"person:{e.person}", "to": f"chapter:{e.chapter}",
                "edgeType": "appears_in", "title": "出场" if e.role == "出场" else "提及",
            })

        # Participates
        for e in graph.participates_edges:
            edges.append({
                "from": f"person:{e.person}", "to": f"event:{e.event}",
                "edgeType": "participates",
                "title": f"{e.role} | {e.outcome or ''}",
            })

        # OccursIn
        for e in graph.occurs_in_edges:
            edges.append({
                "from": f"event:{e.event}",
                "to": f"chapter:{e.chapter_start}",
                "edgeType": "occurs_in",
                "title": f"第{e.chapter_start}-{e.chapter_end or e.chapter_start}章",
            })

        # LocatedAt
        for e in graph.located_at_edges:
            edges.append({
                "from": f"event:{e.event}", "to": f"location:{e.location}",
                "edgeType": "located_at", "title": e.location,
            })

        # BelongsTo
        for e in graph.belongs_to_edges:
            edges.append({
                "from": f"person:{e.person}", "to": f"org:{e.organization}",
                "edgeType": "belongs_to", "title": e.role,
            })

        # Owns
        for e in graph.owns_edges:
            end = f"第{e.chapter_end}章" if e.chapter_end else "仍持有"
            edges.append({
                "from": f"person:{e.person}", "to": f"item:{e.item}",
                "edgeType": "owns", "title": f"第{e.chapter_start}章 - {end}",
            })

        # EventRelation
        for e in graph.event_relation_edges:
            edges.append({
                "from": f"event:{e.from_event.split(':',1)[-1] if ':' in e.from_event else e.from_event}",
                "to": f"event:{e.to_event.split(':',1)[-1] if ':' in e.to_event else e.to_event}",
                "edgeType": "event_relation", "title": e.relation_type,
            })

        # LocationHierarchy
        for e in graph.location_hierarchy_edges:
            edges.append({
                "from": f"location:{e.child}", "to": f"location:{e.parent}",
                "edgeType": "location_hierarchy", "title": f"{e.child} ⊂ {e.parent}",
            })

        return {"nodes": nodes, "edges": edges}

    def render(self, graph: "StoryGraph", output_path: str):
        """生成交互式 HTML 文件。

        Args:
            graph: StoryGraph 实例
            output_path: 输出文件路径（.html）
        """
        vis_data = self.to_vis_data(graph)

        html = HTML_TEMPLATE.replace(
            "__GRAPH_DATA__", json.dumps(vis_data, ensure_ascii=False),
        ).replace(
            "__NODE_CONFIG__", json.dumps(NODE_CONFIG, ensure_ascii=False),
        ).replace(
            "__EDGE_CONFIG__", json.dumps(EDGE_CONFIG, ensure_ascii=False),
        )

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html)

        print(f"[Viz] 知识图谱已生成: {output_path}")
        print(f"      节点: {len(vis_data['nodes'])}, 边: {len(vis_data['edges'])}")
        return output_path

    def render_to_string(self, graph: "StoryGraph") -> str:
        """生成 HTML 字符串（不写文件）。"""
        vis_data = self.to_vis_data(graph)
        return HTML_TEMPLATE.replace(
            "__GRAPH_DATA__", json.dumps(vis_data, ensure_ascii=False),
        ).replace(
            "__NODE_CONFIG__", json.dumps(NODE_CONFIG, ensure_ascii=False),
        ).replace(
            "__EDGE_CONFIG__", json.dumps(EDGE_CONFIG, ensure_ascii=False),
        )
