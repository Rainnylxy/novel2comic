# -*- coding: utf-8 -*-
"""Tornado Request Handlers —— REST API + SSE + 静态文件。

端点:
- POST /api/play/start   — 初始化游戏
- POST /api/play/message — 发送用户消息
- POST /api/play/choice  — 提交抉择
- GET  /api/play/stream  — SSE 事件流
- GET  /api/play/state   — 查询游戏状态
- GET  /                  — 前端页面
"""

import json
import asyncio
import os

import tornado.web
import tornado.ioloop

from .session_manager import SessionManager


# 全局 SessionManager（由 app 初始化时注入）
_session_manager: SessionManager = None


def set_session_manager(mgr: SessionManager):
    global _session_manager
    _session_manager = mgr


def get_session_manager() -> SessionManager:
    return _session_manager


class BaseHandler(tornado.web.RequestHandler):
    """基类：设置 CORS 和 JSON 工具方法。"""

    def set_default_headers(self):
        self.set_header("Access-Control-Allow-Origin", "*")
        self.set_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.set_header("Access-Control-Allow-Headers", "Content-Type")

    def options(self, *_args):
        self.set_status(204)
        self.finish()

    def write_json(self, data: dict, status: int = 200):
        self.set_header("Content-Type", "application/json")
        self.set_status(status)
        self.write(json.dumps(data, ensure_ascii=False))

    def read_json(self) -> dict:
        try:
            return json.loads(self.request.body)
        except (json.JSONDecodeError, Exception):
            return {}


class StartHandler(BaseHandler):
    """POST /api/play/start — 初始化游戏。"""

    def post(self):
        body = self.read_json()

        novel_path = body.get("novel_path", "")
        chapter = int(body.get("chapter", 1))
        user_name = body.get("user_name", "林默")
        user_identity = body.get("user_identity", "新调来的刑警")
        npc_names = body.get("npcs", [])

        if not novel_path:
            self.write_json({"error": "novel_path is required"}, 400)
            return

        # 需要从全局上下文获取 ctx, services, llm
        app = self.application
        ctx = app.settings.get("global_context")
        services = app.settings.get("services")
        llm = app.settings.get("llm")

        if not ctx or not services or not llm:
            self.write_json({"error": "Server not initialized"}, 500)
            return

        # 如果 ctx.novel 未加载，先加载
        if not ctx.novel or not ctx.novel.story_graph:
            try:
                from ..cli.cli import _load_novel
                _load_novel(novel_path, services, ctx)
            except Exception as e:
                self.write_json({"error": f"Failed to load novel: {e}"}, 500)
                return

        # 自动检测 NPC
        if not npc_names:
            graph = ctx.novel.story_graph
            ch = chapter
            detected = set()
            if graph:
                for edge in graph.appears_in_edges:
                    if edge.chapter == ch:
                        detected.add(edge.person)
                persons = graph.person_nodes
                imp_map = {p.name: p.importance for p in persons}
                npc_names = sorted(detected, key=lambda n: -imp_map.get(n, 0))[:5]
            if not npc_names and graph:
                top = sorted(graph.person_nodes, key=lambda p: -p.importance)[:3]
                npc_names = [p.name for p in top]

        mgr = get_session_manager()
        session = mgr.create(
            ctx=ctx,
            services=services,
            llm=llm,
            novel_path=novel_path,
            chapter=chapter,
            user_name=user_name,
            user_identity=user_identity,
            npc_names=npc_names,
        )

        # 在后台线程启动游戏
        import threading
        t = threading.Thread(target=session.start, daemon=True)
        t.start()

        self.write_json({
            "session_id": session.session_id,
            "state": session.state.to_dict(),
        })


class MessageHandler(BaseHandler):
    """POST /api/play/message — 发送用户消息。"""

    def post(self):
        body = self.read_json()
        session_id = body.get("session_id", "")
        text = body.get("text", "")

        if not session_id or not text:
            self.write_json({"error": "session_id and text required"}, 400)
            return

        session = get_session_manager().get(session_id)
        if not session:
            self.write_json({"error": "Session not found"}, 404)
            return

        import threading
        t = threading.Thread(target=session.send_message, args=(text,), daemon=True)
        t.start()

        self.write_json({"status": "ok"})


class ChoiceHandler(BaseHandler):
    """POST /api/play/choice — 提交抉择。"""

    def post(self):
        body = self.read_json()
        session_id = body.get("session_id", "")
        choice_index = int(body.get("choice_index", 0))

        if not session_id:
            self.write_json({"error": "session_id required"}, 400)
            return

        session = get_session_manager().get(session_id)
        if not session:
            self.write_json({"error": "Session not found"}, 404)
            return

        import threading
        t = threading.Thread(target=session.apply_choice, args=(choice_index,), daemon=True)
        t.start()

        self.write_json({"status": "ok"})


class StateHandler(BaseHandler):
    """GET /api/play/state — 查询游戏状态。"""

    def get(self):
        session_id = self.get_argument("session_id", "")
        if not session_id:
            self.write_json({"error": "session_id required"}, 400)
            return

        session = get_session_manager().get(session_id)
        if not session:
            self.write_json({"error": "Session not found"}, 404)
            return

        self.write_json(session.state.to_dict())


class StreamHandler(BaseHandler):
    """GET /api/play/stream — SSE 事件流。"""

    async def get(self):
        session_id = self.get_argument("session_id", "")
        if not session_id:
            self.write_json({"error": "session_id required"}, 400)
            return

        session = get_session_manager().get(session_id)
        if not session:
            self.write_json({"error": "Session not found"}, 404)
            return

        # SSE headers
        self.set_header("Content-Type", "text/event-stream")
        self.set_header("Cache-Control", "no-cache")
        self.set_header("Connection", "keep-alive")
        self.set_header("X-Accel-Buffering", "no")

        # 循环推送事件
        loop = tornado.ioloop.IOLoop.current()
        while True:
            try:
                # 非阻塞获取事件
                event = await loop.run_in_executor(None, session.get_event, 5.0)
                if event is None:
                    # 发送心跳
                    self.write(": heartbeat\n\n")
                    await self.flush()
                    continue

                event_type = event.pop("type", "message")
                data = json.dumps(event, ensure_ascii=False)
                self.write(f"event: {event_type}\ndata: {data}\n\n")
                await self.flush()

            except tornado.iostream.StreamClosedError:
                break
            except Exception:
                break


class MainHandler(BaseHandler):
    """GET / — 提供前端页面。"""

    def get(self):
        static_dir = os.path.join(os.path.dirname(__file__), "static")
        index_path = os.path.join(static_dir, "index.html")
        if os.path.exists(index_path):
            with open(index_path, "r", encoding="utf-8") as f:
                self.set_header("Content-Type", "text/html; charset=utf-8")
                self.write(f.read())
        else:
            self.write_json({"error": "index.html not found"}, 404)
