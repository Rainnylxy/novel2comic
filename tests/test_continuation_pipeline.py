# -*- coding: utf-8 -*-
"""续写系统集成测试 —— 使用 mock LLM 验证流水线逻辑。"""

import json
import asyncio
from unittest.mock import MagicMock, patch

import pytest

from src.continuation.fragment import StoryFragment, PipelineEvent
from src.continuation.pipeline import ContinuationPipeline
from src.context import GlobalContext, ServiceRegistry


@pytest.fixture
def mock_ctx_and_services():
    """构建 mock GlobalContext 和 ServiceRegistry。"""
    # Mock Novel with chapters
    mock_novel = MagicMock()
    mock_novel.title = "test_novel"
    mock_novel.chapters = [
        MagicMock(index=1, content="第一章内容。" * 100),
        MagicMock(index=2, content="第二章内容。" * 100),
    ]

    # Mock StoryGraph
    mock_graph = MagicMock()
    mock_graph.total_node_count = 10
    mock_graph.total_edge_count = 20
    mock_graph.person_nodes = []
    mock_graph.event_nodes = []
    mock_graph.event_relation_edges = []
    mock_graph.relationship_edges = []
    mock_novel.story_graph = mock_graph

    # Mock KG Service
    mock_kg = MagicMock()
    mock_kg.get_all_persons.return_value = []
    mock_kg.enemy_pairs.return_value = []

    # Mock Project Service
    mock_project = MagicMock()
    mock_project.create_project_dir.return_value = "/tmp/test_project"

    ctx = GlobalContext()
    ctx.novel = mock_novel

    services = ServiceRegistry(kg=mock_kg, project=mock_project)

    return ctx, services


class TestPipelineFlow:
    """测试流水线的事件流转。"""

    @pytest.mark.asyncio
    async def test_pipeline_emits_phase_events(self, mock_ctx_and_services):
        """验证流水线输出 phase 事件序列。"""
        ctx, services = mock_ctx_and_services
        mock_llm = MagicMock()

        # Mock KG extraction
        services.kg.extract_incremental = MagicMock(return_value=ctx.novel.story_graph)

        pipeline = ContinuationPipeline(ctx, services, mock_llm)

        # 跳过 load_novel（太复杂），直接手动设置内部状态
        pipeline._style_profile = MagicMock()
        pipeline._style_profile.summary.return_value = ""
        pipeline._style_profile.exemplars_text.return_value = ""
        pipeline._previous_chapter_ending = "test ending"
        pipeline._chapter = 2

        # Mock agents
        mock_architect = MagicMock()
        mock_architect.run = AsyncMock(return_value=json.dumps({
            "chapter_number": 3, "title": "测试", "synopsis": "测试大纲",
            "structure": {"opening": "", "rising": "", "climax": "", "hook": ""},
            "tone": "测试",
        }))
        mock_writer = MagicMock()
        mock_fragment = StoryFragment(type="narration", text="测试叙述。")
        mock_writer.stream = lambda outline: async_gen([mock_fragment])
        mock_reviewer = MagicMock()
        mock_reviewer.run = AsyncMock(return_value=json.dumps({
            "issues": [], "overall_score": 8.0,
        }))
        mock_editor = MagicMock()

        pipeline.architect = mock_architect
        pipeline.writer = mock_writer
        pipeline.reviewer = mock_reviewer
        pipeline.editor = mock_editor

        events = []
        async for event in pipeline.run("test instruction"):
            events.append(event)

        # 检查事件序列
        event_types = [e.event_type for e in events]
        assert "phase" in event_types
        assert "outline" in event_types
        assert "fragment" in event_types
        assert "review" in event_types
        assert "complete" in event_types
        assert "done" in event_types

    @pytest.mark.asyncio
    async def test_pipeline_inject_forwards_to_writer(self, mock_ctx_and_services):
        """验证 inject 转发到 Writer。"""
        ctx, services = mock_ctx_and_services
        mock_llm = MagicMock()

        pipeline = ContinuationPipeline(ctx, services, mock_llm)
        mock_writer = MagicMock()
        mock_writer.inject = AsyncMock()
        pipeline.writer = mock_writer
        pipeline._phase = "writing"

        await pipeline.inject("测试指令")

        mock_writer.inject.assert_called_once_with("测试指令")


class TestFragmentTypes:
    """验证所有 Fragment 类型正确渲染。"""

    def test_all_fragment_types(self):
        """确保 5 种 Fragment 类型都能正常序列化。"""
        fragments = [
            StoryFragment(type="narration", text="夜色如墨。"),
            StoryFragment(type="dialogue", text="你好。", character="江停"),
            StoryFragment(type="action", text="推开门", character="严峫"),
            StoryFragment(type="inner_thought", text="这件事不对。", character="江停"),
            StoryFragment(type="divider", text="", divider_label="三小时后"),
        ]

        for f in fragments:
            d = f.to_dict()
            assert "type" in d
            assert "text" in d
            sse = f.to_sse()
            assert "\n" not in sse  # SSE 单行约束
            restored = StoryFragment.from_json(sse)
            assert restored.type == f.type
            assert restored.text == f.text


# Helper: async generator from list
async def async_gen(items: list):
    for item in items:
        yield item


# Helper: async mock
class AsyncMock(MagicMock):
    async def __call__(self, *args, **kwargs):
        return super().__call__(*args, **kwargs)
