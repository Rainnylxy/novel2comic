# -*- coding: utf-8 -*-
"""Agent 集成测试——使用 Mock LLM 验证 Agent Tool 端到端流程。"""

import os
import sys
import json
import tempfile
from unittest.mock import MagicMock, patch

# 添加 novel2comic/ 到路径（使 from src.xxx 可用）
_n2c_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _n2c_dir not in sys.path:
    sys.path.insert(0, _n2c_dir)
# 添加父目录（使 import novel2comic 可用），agentflow 通过 PYTHONPATH 提供
_parent_dir = os.path.dirname(_n2c_dir)
if _parent_dir not in sys.path:
    sys.path.insert(0, _parent_dir)

from novel2comic.src.models import ChapterData, AnalysisResult, CharacterAppearance, CharacterSheet
from novel2comic.src.img_adapter import ImageGenAdapter
from novel2comic.src.styles import detect_style


SAMPLE_TEXT = """
夜幕降临，长安城华灯初上。苏墨站在朱雀大街的尽头，手握一柄锈迹斑斑的铁剑。

"三年了，我终于回来了。"他低声自语，目光穿过熙攘的人潮，锁定在那座金碧辉煌的将军府上。

一个卖糖葫芦的老者经过，苏墨叫住了他："老人家，将军府近日可有什么动静？"

老者打量了他一眼，压低声音道："小兄弟，将军府三日前贴出告示，要招纳天下剑客，缉拿大盗'夜枭'。赏金一千两黄金。"

"一千两黄金..."苏墨嘴角微扬，眼中闪过一丝复杂的神色。

他绕过朱雀大街，钻进一条暗巷。一只黑猫从墙头跃下，落在他肩上。苏墨从怀中取出一张泛黄的羊皮纸，上面画着将军府的内部地形图。

"夜枭...呵，他们连我的真名都不知道了。"他收起羊皮纸，身形一闪，消失在夜色中。
"""


# ============================================================
# Mock: 模拟 OpenAI 同步客户端
# ============================================================

class MockCompletion:
    def __init__(self, content):
        self.choices = [MagicMock()]
        self.choices[0].message.content = content


class MockChat:
    def __init__(self, responses: list[dict]):
        self.responses = responses
        self.call_count = 0

    def create(self, **kwargs):
        idx = self.call_count
        self.call_count += 1
        if idx < len(self.responses):
            return MockCompletion(json.dumps(self.responses[idx], ensure_ascii=False))
        return MockCompletion("{}")


class MockOpenAI:
    def __init__(self, responses: list[dict]):
        self.chat = MagicMock()
        self.chat.completions = MockChat(responses)


def test_style_detection():
    """测试风格自动判断。"""
    s = detect_style(["武侠", "悬疑"], "慢热")
    assert s.name == "gufeng", f"Expected gufeng, got {s.name}"

    s = detect_style(["校园", "恋爱"])
    assert s.name == "manga", f"Expected manga, got {s.name}"

    s = detect_style(["都市"])
    assert s.name == "webtoon", f"Expected webtoon, got {s.name}"

    print("  [PASS] test_style_detection passed")


def test_agent_tools_end_to_end():
    """测试 Agent Tool 端到端流程（Mock LLM）。"""
    import novel2comic.agent as agent_module

    mock_responses = [
        # Stage 1: analyze_text 返回
        {
            "genre_tags": ["武侠", "悬疑"],
            "style": "gufeng",
            "tone": ["苍凉", "暗涌"],
            "era": "古代架空",
            "pace": "慢热",
            "characters_preview": [
                {"name": "苏墨", "role": "主角", "first_appearance_line": "苏墨站在朱雀大街的尽头"},
                {"name": "老者", "role": "配角", "first_appearance_line": "一个卖糖葫芦的老者"},
                {"name": "黑猫", "role": "伙伴", "first_appearance_line": "一只黑猫从墙头跃下"},
            ],
        },
        # Stage 2: design_characters 返回
        [
            {
                "id": "su_mo", "name": "苏墨", "role": "protagonist",
                "appearance": {"face": "清瘦", "hair": "长发", "build": "修长", "clothing": "灰袍", "accessories": "锈剑", "distinctive_features": "锐利眼神"},
                "sd_trigger_words": "su_mo, lean swordsman, sharp jawline, grey robes, rusty sword",
                "personality_notes": "冷峻内敛",
            },
            {
                "id": "old_man", "name": "老者", "role": "supporting",
                "appearance": {"face": "皱纹", "hair": "花白", "build": "佝偻", "clothing": "粗布衣", "accessories": "糖葫芦车", "distinctive_features": "精明小眼"},
                "sd_trigger_words": "old street vendor, weathered face, worn hat",
                "personality_notes": "市井精明",
            },
            {
                "id": "black_cat", "name": "黑猫", "role": "supporting",
                "appearance": {"face": "", "hair": "", "build": "", "clothing": "", "accessories": "", "distinctive_features": "纯黑毛色"},
                "sd_trigger_words": "black cat, sleek fur, glowing eyes",
                "personality_notes": "神秘伙伴",
            },
        ],
        # Stage 3: extract_scenes 返回
        [
            {"id": 1, "title": "朱雀大街·归来", "summary": "苏墨归来", "characters_in_scene": ["苏墨"], "emotion_arc": "苍凉→暗涌", "key_dialogue": "三年了"},
            {"id": 2, "title": "糖葫芦摊·情报", "summary": "打探消息", "characters_in_scene": ["苏墨", "老者"], "emotion_arc": "平静→暗讽", "key_dialogue": "一千两黄金"},
        ],
        # Stage 4: storyboard_scene scene 1
        [
            {"panel_number": 1, "visual_description": "远景长安", "character_action": "无", "dialogue": "", "camera_angle": "俯视大远景", "mood": "寂寥", "sd_prompt": "epic view of capital", "character_refs": []},
            {"panel_number": 2, "visual_description": "锈剑特写", "character_action": "手握紧", "dialogue": "", "camera_angle": "极近特写", "mood": "沉重", "sd_prompt": "close-up rusty sword", "character_refs": ["苏墨"]},
        ],
        # Stage 4: storyboard_scene scene 2
        [
            {"panel_number": 1, "visual_description": "街边对话", "character_action": "苏墨拦下老者", "dialogue": "将军府近日可有什么动静？", "camera_angle": "中景", "mood": "试探", "sd_prompt": "street conversation", "character_refs": ["苏墨", "老者"]},
            {"panel_number": 2, "visual_description": "老者密语", "character_action": "压低声音", "dialogue": "赏金一千两黄金", "camera_angle": "近景", "mood": "暗讽", "sd_prompt": "old man whispering", "character_refs": ["老者"]},
        ],
    ]

    mock_client = MockOpenAI(mock_responses)
    img_gen = ImageGenAdapter(use_placeholder=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        # 注入 Agent 上下文
        agent_module._ctx.chapter_data = ChapterData(
            title="月下归来",
            source_text=SAMPLE_TEXT,
            output_dir=tmpdir,
        )
        agent_module._ctx.openai_client = mock_client
        agent_module._ctx.llm_model = "mock"
        agent_module._ctx.img_gen = img_gen

        # === 逐个调用 Tool ===

        # Tool 1: analyze_text
        result1 = json.loads(agent_module.analyze_text.func(SAMPLE_TEXT))
        assert result1["status"] == "ok", f"analyze_text failed: {result1}"
        assert agent_module._ctx.data.analysis is not None
        assert agent_module._ctx.data.analysis.style == "gufeng"
        assert len(agent_module._ctx.data.analysis.characters_preview) == 3
        print("  [PASS] Tool 1: analyze_text")

        # Tool 2: design_characters
        result2 = json.loads(agent_module.design_characters.func())
        assert result2["status"] == "ok", f"design_characters failed: {result2}"
        assert len(agent_module._ctx.data.characters) == 3
        assert agent_module._ctx.data.characters[0].name == "苏墨"
        assert agent_module._ctx.data.characters[0].sd_trigger_words != ""
        print("  [PASS] Tool 2: design_characters")

        # Tool 3: extract_scenes
        result3 = json.loads(agent_module.extract_scenes.func())
        assert result3["status"] == "ok", f"extract_scenes failed: {result3}"
        assert len(agent_module._ctx.data.scenes) == 2
        assert agent_module._ctx.data.scenes[0].title == "朱雀大街·归来"
        print("  [PASS] Tool 3: extract_scenes")

        # Tool 4: storyboard_scene (scene 1)
        result4a = json.loads(agent_module.storyboard_scene.func(1))
        assert result4a["status"] == "ok", f"storyboard_scene(1) failed: {result4a}"
        assert len(agent_module._ctx.data.scenes[0].panels) == 2
        print("  [PASS] Tool 4a: storyboard_scene(scene_id=1)")

        # Tool 4: storyboard_scene (scene 2)
        result4b = json.loads(agent_module.storyboard_scene.func(2))
        assert result4b["status"] == "ok", f"storyboard_scene(2) failed: {result4b}"
        assert len(agent_module._ctx.data.scenes[1].panels) == 2
        print("  [PASS] Tool 4b: storyboard_scene(scene_id=2)")

        # Verify sd_prompt was enhanced with style base + character triggers + aspect ratio
        panel1_prompt = agent_module._ctx.data.scenes[1].panels[0].sd_prompt
        assert "webtoon" in panel1_prompt.lower() or "gufeng" in panel1_prompt.lower() or "manga" in panel1_prompt.lower(), \
            f"sd_prompt should contain style base: {panel1_prompt[:100]}"
        print("  [PASS] sd_prompt auto-enhancement verified")

        # Tool 5: generate_images
        result5 = json.loads(agent_module.generate_images.func(0))
        assert result5["status"] == "ok", f"generate_images failed: {result5}"
        assert result5["generated"] == 4  # 2 scenes x 2 panels each
        for scene in agent_module._ctx.data.scenes:
            for panel in scene.panels:
                assert panel.status == "generated"
                assert os.path.exists(panel.generated_image_path)
        print("  [PASS] Tool 5: generate_images")

        # Tool 6: compile_comic
        result6 = json.loads(agent_module.compile_comic.func())
        assert result6["status"] == "ok", f"compile_comic failed: {result6}"
        assert result6["page_count"] == 2
        for page in agent_module._ctx.data.pages:
            assert os.path.exists(page.image_path)
        print("  [PASS] Tool 6: compile_comic")

        # Tool 7: save_project
        result7 = json.loads(agent_module.save_project.func())
        assert result7["status"] == "ok", f"save_project failed: {result7}"
        saved_files = result7["saved_files"]
        assert len(saved_files) > 0
        for sf in saved_files:
            assert os.path.exists(sf), f"Saved file not found: {sf}"
        print("  [PASS] Tool 7: save_project")

        # Verify save/load roundtrip (chapter_data is second file)
        ch_file = [f for f in saved_files if "chapter_data" in f][0]
        loaded = ChapterData.load(ch_file)
        assert loaded.title == "月下归来"
        assert len(loaded.characters) == 3
        assert len(loaded.scenes) == 2
        print("  [PASS] Save/load roundtrip")

    print("  [PASS] test_agent_tools_end_to_end passed")


def test_data_serialization():
    """测试数据模型 JSON 序列化。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        data = ChapterData(title="测试", source_text="测试文本", output_dir=tmpdir)
        data.analysis = AnalysisResult(genre_tags=["武侠"], style="gufeng")
        data.characters = [
            CharacterSheet(
                id="test_char", name="测试角色", role="protagonist",
                appearance=CharacterAppearance(face="测试面孔"),
                sd_trigger_words="test character trigger words",
            )
        ]
        filepath = os.path.join(tmpdir, "test.json")
        data.save(filepath)
        loaded = ChapterData.load(filepath)
        assert loaded.title == "测试"
        assert loaded.analysis.style == "gufeng"
        assert len(loaded.characters) == 1
        assert loaded.characters[0].name == "测试角色"
        print("  [PASS] test_data_serialization passed")


# ============================================================
# Novel-level tests
# ============================================================

NOVEL_TEXT = """第一章 星落

夜幕降临，长安城华灯初上。苏墨站在朱雀大街的尽头，手握一柄锈迹斑斑的铁剑。

"三年了，我终于回来了。"他低声自语。

第二章 暗巷

他绕过朱雀大街，钻进一条暗巷。一只黑猫从墙头跃下，落在他肩上。

苏墨从怀中取出一张泛黄的羊皮纸，上面画着将军府的内部地形图。

第三章 相遇

清晨的阳光洒在青石板路上。一位白衣少女从巷口经过，目光与苏墨相遇。

"你是..."少女迟疑地看着他手中的剑。
"""


def test_chapter_parser():
    """测试章节解析器。"""
    from novel2comic.src.chapter_parser import parse_novel_chapters
    from novel2comic.src.models import ChapterInfo

    chapters = parse_novel_chapters(NOVEL_TEXT, "测试小说")
    assert len(chapters) == 3, f"Expected 3 chapters, got {len(chapters)}"

    assert chapters[0].index == 1
    assert chapters[0].title == "星落"
    assert "苏墨站在朱雀大街" in chapters[0].content
    assert "三年了" in chapters[0].content
    assert "暗巷" not in chapters[0].content  # 不应该包含下一章内容

    assert chapters[1].index == 2
    assert chapters[1].title == "暗巷"
    assert "黑猫从墙头跃下" in chapters[1].content

    assert chapters[2].index == 3
    assert chapters[2].title == "相遇"
    assert "白衣少女" in chapters[2].content

    print("  [PASS] test_chapter_parser passed")


def test_novel_model():
    """测试 Novel 数据模型。"""
    from novel2comic.src.models import Novel, ChapterInfo, CharacterSheet, CharacterAppearance

    novel = Novel(title="测试小说")
    novel.chapters = [
        ChapterInfo(index=1, title="第一章", content="测试内容", word_count=4),
        ChapterInfo(index=2, title="第二章", content="更多内容", word_count=4),
    ]

    # 添加角色到全书库
    char = CharacterSheet(
        id="test", name="测试角色", role="protagonist",
        appearance=CharacterAppearance(face="测试"),
        sd_trigger_words="test trigger",
    )
    novel.add_characters([char])

    assert novel.total_chapters == 2
    assert novel.has_character("测试角色")
    assert len(novel.characters) == 1

    # 重复添加同名角色 → 跳过
    novel.add_characters([char])
    assert len(novel.characters) == 1

    # 当前章节
    novel.current_chapter_index = 1
    assert novel.current_chapter is not None
    assert novel.current_chapter.title == "第一章"

    # JSON 序列化/反序列化
    d = novel.to_dict()
    loaded = Novel.from_dict(d)
    assert loaded.title == "测试小说"
    assert loaded.total_chapters == 2
    assert len(loaded.characters) == 1
    assert loaded.characters[0].name == "测试角色"

    print("  [PASS] test_novel_model passed")


def test_novel_agent_tools():
    """测试 Novel 级 Agent Tools（load_novel, list_chapters, select_chapter）。"""
    import novel2comic.agent as agent_module
    import tempfile

    # 写入临时小说文件
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as f:
        f.write(NOVEL_TEXT)
        novel_path = f.name

    try:
        # Tool: load_novel
        result1 = json.loads(agent_module.load_novel.func(novel_path))
        assert result1["status"] == "ok", f"load_novel failed: {result1}"
        assert result1["total_chapters"] == 3
        assert agent_module._ctx.novel is not None
        assert agent_module._ctx.novel.total_chapters == 3
        print("  [PASS] Novel tool: load_novel")

        # Tool: list_chapters
        result2 = json.loads(agent_module.list_chapters.func())
        assert result2["status"] == "ok"
        assert result2["total"] == 3
        assert len(result2["chapter_list"]) == 3
        print("  [PASS] Novel tool: list_chapters")

        # Tool: select_chapter(2)
        result3 = json.loads(agent_module.select_chapter.func(2))
        assert result3["status"] == "ok"
        assert result3["chapter_index"] == 2
        assert agent_module._ctx.novel.current_chapter_index == 2
        assert agent_module._ctx.chapter_data is not None
        assert "暗巷" in agent_module._ctx.chapter_data.title
        assert "黑猫从墙头跃下" in agent_module._ctx.chapter_data.source_text
        print("  [PASS] Novel tool: select_chapter(2)")

        # select_chapter 不存在的章
        result4 = json.loads(agent_module.select_chapter.func(99))
        assert "error" in result4
        print("  [PASS] Novel tool: select_chapter(99) returns error")

    finally:
        os.unlink(novel_path)

    print("  [PASS] test_novel_agent_tools passed")


def test_novel_registry():
    """测试小说注册表：注册、查找、缓存命中、列表。"""
    from novel2comic.src.novel_registry import (
        register_novel, find_novel, list_all_novels, save_registry, load_registry,
    )
    import tempfile

    # 清理注册表（避免干扰其他测试）
    reg_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "projects", "novel_registry.json",
    )
    if os.path.exists(reg_path):
        os.remove(reg_path)

    # 创建临时小说文件
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as f:
        f.write("第一章 测试\n这是测试内容。\n\n第二章 继续\n更多内容。")
        tmp_path = f.name

    try:
        # 1. 首次注册
        entry = register_novel(tmp_path, "测试小说", 2, "/tmp/test_project")
        assert entry.title == "测试小说"
        assert entry.total_chapters == 2
        print("  [PASS] Registry: register_novel")

        # 2. 查找（缓存命中 —— 文件未变）
        found = find_novel(tmp_path)
        assert found is not None
        assert found.title == "测试小说"
        assert found.total_chapters == 2
        print("  [PASS] Registry: find_novel (cache hit)")

        # 3. 列表
        all_novels = list_all_novels()
        assert len(all_novels) >= 1
        assert any(n.title == "测试小说" for n in all_novels)
        print("  [PASS] Registry: list_all_novels")

        # 4. 修改文件后缓存失效
        with open(tmp_path, "a", encoding="utf-8") as f:
            f.write("\n第三章 新增\n新内容。")
        found_after_change = find_novel(tmp_path)
        assert found_after_change is None  # 文件变了，缓存失效
        print("  [PASS] Registry: cache miss after file change")

        # 5. 不存在的文件
        not_found = find_novel("nonexistent.txt")
        assert not_found is None
        print("  [PASS] Registry: find_novel (not found)")

        # 6. 重新注册更新后的文件
        entry2 = register_novel(tmp_path, "测试小说v2", 3, "/tmp/test_project2")
        assert entry2.total_chapters == 3
        found2 = find_novel(tmp_path)
        assert found2 is not None
        assert found2.total_chapters == 3
        print("  [PASS] Registry: re-register after file change")

    finally:
        os.unlink(tmp_path)
        if os.path.exists(reg_path):
            os.remove(reg_path)

    print("  [PASS] test_novel_registry passed")


def test_load_novel_cache_hit():
    """测试 load_novel 的缓存命中流程。"""
    import novel2comic.agent as agent_module
    import tempfile

    # 准备测试小说
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as f:
        f.write(NOVEL_TEXT)
        novel_path = f.name

    # 清理注册表
    reg_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "projects", "novel_registry.json",
    )
    if os.path.exists(reg_path):
        os.remove(reg_path)

    try:
        # 第一次加载：缓存未命中
        result1 = json.loads(agent_module.load_novel.func(novel_path))
        assert result1["status"] == "ok"
        assert result1["cached"] == False, f"First load should miss cache: {result1}"
        assert result1["total_chapters"] == 3
        print("  [PASS] Load novel: first time (cache miss)")

        # 第二次加载同一文件：缓存命中
        result2 = json.loads(agent_module.load_novel.func(novel_path))
        assert result2["status"] == "ok"
        assert result2["cached"] == True, f"Second load should hit cache: {result2}"
        assert result2["total_chapters"] == 3
        assert agent_module._ctx.novel is not None
        assert agent_module._ctx.novel.total_chapters == 3
        print("  [PASS] Load novel: second time (cache hit)")

        # list_novels 应该能看到
        result3 = json.loads(agent_module.list_novels.func())
        assert result3["count"] >= 1
        print("  [PASS] list_novels shows cached novel")

        # resume_novel 恢复
        if agent_module._ctx.novel:
            agent_module._ctx.novel = None  # 先清空
        result4 = json.loads(agent_module.resume_novel.func(0))
        assert result4["status"] == "ok"
        assert agent_module._ctx.novel is not None
        assert agent_module._ctx.novel.total_chapters == 3
        print("  [PASS] resume_novel restores from cache")

        # resume_novel 错误索引
        result5 = json.loads(agent_module.resume_novel.func(99))
        assert "error" in result5
        print("  [PASS] resume_novel(99) returns error")

    finally:
        os.unlink(novel_path)
        if os.path.exists(reg_path):
            os.remove(reg_path)

    print("  [PASS] test_load_novel_cache_hit passed")


def test_graph_algorithms():
    """测试 NetworkX 图算法。"""
    from novel2comic.src.models import CharacterGraph, RelationshipEdge, CharacterNode

    graph = CharacterGraph()

    # 添加角色
    for name, faction, importance in [
        ("苏墨", "江湖", 10), ("将军", "将军府", 8),
        ("黑猫", "江湖", 6), ("白衣少女", "无", 7),
        ("老管家", "将军府", 4),
    ]:
        node = CharacterNode(id=f"n_{name}", name=name, faction=faction, importance=importance)
        graph._add_node(node)

    # 添加关系
    relations = [
        ("苏墨", "将军", "敌对", -8, "A主导"),
        ("苏墨", "黑猫", "主仆", 8, "平等"),
        ("苏墨", "白衣少女", "爱情", 5, "平等"),
        ("将军", "老管家", "主仆", 3, "A主导"),
        ("白衣少女", "将军", "血缘", -2, "B主导"),
    ]
    for a, b, rtype, intimacy, power in relations:
        graph.add_edge(RelationshipEdge(
            from_char=a, to_char=b, relation_type=rtype,
            intimacy=intimacy, power_dynamic=power,
        ))

    # 测试 1: 节点数
    assert graph.node_count == 5
    assert graph.edge_count == 5
    print("  [PASS] graph: node/edge count")

    # 测试 2: 最短路径
    path = graph.shortest_path("老管家", "黑猫")
    assert path is not None
    # 老管家 → 将军 → 苏墨 → 黑猫
    assert len(path) == 4
    print(f"  [PASS] graph: shortest_path(老管家, 黑猫) = {' → '.join(path)}")

    # 测试 3: 中心度排名
    centrality = graph.centrality_ranking()
    assert centrality[0][0] == "苏墨"  # 苏墨连接最多人
    print(f"  [PASS] graph: centrality top = {centrality[0]}")

    # 测试 4: 阵营分组
    factions = graph.faction_groups()
    assert "江湖" in factions
    assert "将军府" in factions
    print(f"  [PASS] graph: factions = {list(factions.keys())}")

    # 测试 5: 敌对关系
    enemies = graph.enemy_pairs()
    assert ("苏墨", "将军") in enemies or ("将军", "苏墨") in enemies
    print(f"  [PASS] graph: enemy_pairs = {enemies}")

    # 测试 6: 关系子图
    story = graph.story_path("苏墨", max_depth=1)
    assert "将军" in story
    assert "黑猫" in story
    assert "白衣少女" in story
    print(f"  [PASS] graph: story_path(苏墨) has {len(story)} direct connections")

    # 测试 7: 亲密度排名
    intimacy_rank = graph.intimacy_ranking()
    assert abs(intimacy_rank[0][2]) >= abs(intimacy_rank[-1][2])
    print(f"  [PASS] graph: intimacy top = {intimacy_rank[0]}")

    # 测试 8: JSON 序列化往返
    d = graph.to_dict()
    loaded = CharacterGraph.from_dict(d)
    assert loaded.node_count == 5
    assert loaded.edge_count == 5
    assert loaded.get_edge("苏墨", "将军").intimacy == -8
    print("  [PASS] graph: to_dict/from_dict roundtrip")

    # 测试 9: 分镜指导
    hint = graph.get_storyboard_hints("苏墨", "将军")
    assert "对峙" in hint
    print(f"  [PASS] graph: storyboard_hints(苏墨, 将军) = {hint}")

    print("  [PASS] test_graph_algorithms passed")


if __name__ == "__main__":
    test_style_detection()
    test_agent_tools_end_to_end()
    test_data_serialization()
    test_chapter_parser()
    test_novel_model()
    test_novel_agent_tools()
    test_novel_registry()
    test_load_novel_cache_hit()
    test_graph_algorithms()
    print("\n*** All tests passed! ***")
