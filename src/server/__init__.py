# -*- coding: utf-8 -*-
"""Web 服务器 —— Tornado 应用。

用法:
    from src.server import create_app
    app = create_app(ctx, services, llm)
    app.listen(8000)
    tornado.ioloop.IOLoop.current().start()
"""

import os
import tornado.web

from .session_manager import SessionManager
from .handlers import (
    set_session_manager,
    StartHandler,
    MessageHandler,
    ChoiceHandler,
    StateHandler,
    StreamHandler,
    MainHandler,
)


def create_app(ctx, services, llm) -> tornado.web.Application:
    """创建 Tornado 应用。

    Args:
        ctx: GlobalContext
        services: ServiceRegistry
        llm: UnifiedLLM

    Returns:
        tornado.web.Application
    """

    # 全局 SessionManager
    session_mgr = SessionManager()
    set_session_manager(session_mgr)

    static_dir = os.path.join(os.path.dirname(__file__), "static")

    settings = {
        "global_context": ctx,
        "services": services,
        "llm": llm,
        "debug": True,
    }

    app = tornado.web.Application(
        [
            (r"/", MainHandler),
            (r"/api/play/start", StartHandler),
            (r"/api/play/message", MessageHandler),
            (r"/api/play/choice", ChoiceHandler),
            (r"/api/play/state", StateHandler),
            (r"/api/play/stream", StreamHandler),
            (r"/static/(.*)", tornado.web.StaticFileHandler, {"path": static_dir}),
        ],
        **settings,
    )

    return app


def start_server(ctx, services, llm, port: int = 8000):
    """启动 Web 服务器。

    Args:
        ctx: GlobalContext
        services: ServiceRegistry
        llm: UnifiedLLM
        port: 监听端口
    """
    import tornado.ioloop

    app = create_app(ctx, services, llm)
    app.listen(port)
    print(f"\n{'='*50}")
    print(f"  📖 互动小说引擎 Web 服务")
    print(f"  地址: http://localhost:{port}")
    print(f"  按 Ctrl+C 停止服务")
    print(f"{'='*50}\n")
    tornado.ioloop.IOLoop.current().start()
