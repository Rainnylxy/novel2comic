# -*- coding: utf-8 -*-
"""续写 API Handlers —— REST + SSE。

端点:
  POST /api/write/start   — 启动续写，返回 SSE 流
  POST /api/write/inject  — 注入用户指令
  GET  /api/write/state   — 查询续写状态
"""

import asyncio
import json
import os
import threading
import queue

import tornado.web

from ..pipeline.pipeline import ContinuationPipeline


# 全局 pipeline（单例，同一时间只有一个续写会话）
_active_pipeline: ContinuationPipeline = None
_pipeline_lock = threading.Lock()


def _get_or_create_pipeline(ctx, services, llm, novel_path: str) -> ContinuationPipeline:
    """获取或创建活跃的 pipeline 实例。

    同一时间只允许一个续写会话运行。新的 start 会替换旧的 pipeline。
    """
    global _active_pipeline
    with _pipeline_lock:
        if _active_pipeline is not None and _active_pipeline.phase == "idle":
            # Check novel_path matches
            if getattr(_active_pipeline, '_loaded_novel_path', '') != novel_path:
                _active_pipeline = ContinuationPipeline(ctx, services, llm)
        else:
            _active_pipeline = ContinuationPipeline(ctx, services, llm)

        if _active_pipeline._chapter == 0:
            _active_pipeline.load_novel(novel_path)
            _active_pipeline._loaded_novel_path = novel_path

    return _active_pipeline


class WriteStartHandler(tornado.web.RequestHandler):
    """POST /api/write/start — 启动续写，返回 SSE 事件流。"""

    def set_default_headers(self):
        self.set_header("Access-Control-Allow-Origin", "*")
        self.set_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.set_header("Access-Control-Allow-Headers", "Content-Type")

    def options(self):
        self.set_status(204)
        self.finish()

    async def post(self):
        body = json.loads(self.request.body or "{}")
        novel_path = body.get("novel_path", "")
        instruction = body.get("instruction", "")

        if not novel_path:
            self.set_status(400)
            self.write({"error": "novel_path is required"})
            return

        app = self.application
        ctx = app.settings.get("global_context")
        services = app.settings.get("services")
        llm = app.settings.get("llm")

        if not ctx or not services or not llm:
            self.set_status(500)
            self.write({"error": "Server not initialized"})
            return

        # SSE 响应头
        self.set_header("Content-Type", "text/event-stream")
        self.set_header("Cache-Control", "no-cache")
        self.set_header("Connection", "keep-alive")
        self.set_header("X-Accel-Buffering", "no")  # 禁用 nginx 缓冲

        try:
            pipeline = _get_or_create_pipeline(ctx, services, llm, novel_path)

            async for event in pipeline.run(instruction):
                sse_text = event.to_sse()
                self.write(sse_text)
                await self.flush()

        except Exception as e:
            error_event = {
                "event": "error",
                "data": json.dumps({"message": str(e)}, ensure_ascii=False),
            }
            self.write(f"event: error\ndata: {json.dumps({'message': str(e)}, ensure_ascii=False)}\n\n")
            await self.flush()
        finally:
            self.finish()


class WriteInjectHandler(tornado.web.RequestHandler):
    """POST /api/write/inject — 注入用户指令。"""

    def set_default_headers(self):
        self.set_header("Access-Control-Allow-Origin", "*")
        self.set_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.set_header("Access-Control-Allow-Headers", "Content-Type")

    def options(self):
        self.set_status(204)
        self.finish()

    async def post(self):
        body = json.loads(self.request.body or "{}")
        instruction = body.get("instruction", "").strip()

        if not instruction:
            self.set_status(400)
            self.write({"error": "instruction is required"})
            return

        if _active_pipeline is None:
            self.set_status(400)
            self.write({"error": "No active writing session. Start one first."})
            return

        # 异步注入指令
        try:
            await _active_pipeline.inject(instruction)
            self.write({"status": "ok", "message": f"指令已注入: {instruction[:50]}"})
        except Exception as e:
            self.set_status(500)
            self.write({"error": str(e)})


class WriteStateHandler(tornado.web.RequestHandler):
    """GET /api/write/state — 查询当前续写状态。"""

    def set_default_headers(self):
        self.set_header("Access-Control-Allow-Origin", "*")
        self.set_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.set_header("Access-Control-Allow-Headers", "Content-Type")

    def options(self):
        self.set_status(204)
        self.finish()

    def get(self):
        if _active_pipeline is None:
            self.write({"phase": "idle", "message": "No active session"})
            return

        self.write({
            "phase": _active_pipeline.phase,
            "chapter": _active_pipeline.chapter,
            "fragment_count": _active_pipeline.fragment_count,
        })
