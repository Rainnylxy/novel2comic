# -*- coding: utf-8 -*-
"""Web 服务器 —— Tornado 应用（仅续写 API）。"""

import tornado.web

from .write_handlers import (
    WriteStartHandler,
    WriteInjectHandler,
    WriteStateHandler,
)


class HealthHandler(tornado.web.RequestHandler):
    def get(self):
        self.write({"status": "ok"})


def create_app(ctx, services, llm) -> tornado.web.Application:
    settings = {
        "global_context": ctx,
        "services": services,
        "llm": llm,
        "debug": True,
    }
    app = tornado.web.Application(
        [
            (r"/api/health", HealthHandler),
            (r"/api/write/start", WriteStartHandler),
            (r"/api/write/inject", WriteInjectHandler),
            (r"/api/write/state", WriteStateHandler),
        ],
        **settings,
    )
    return app


def start_server(ctx, services, llm, port: int = 8000):
    import tornado.ioloop
    app = create_app(ctx, services, llm)
    app.listen(port)
    print(f"\n{'='*50}")
    print(f"  📖 续写引擎 Web 服务")
    print(f"  地址: http://localhost:{port}")
    print(f"  ✍️  /api/write/start  — 启动续写")
    print(f"  💬  /api/write/inject — 注入指令")
    print(f"  📊  /api/write/state  — 查询状态")
    print(f"  按 Ctrl+C 停止服务")
    print(f"{'='*50}\n")
    tornado.ioloop.IOLoop.current().start()
