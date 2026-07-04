# -*- coding: utf-8 -*-
"""SessionManager —— 管理多个 GameSession。

每个浏览器连接对应一个 session_id → GameSession。
"""

import uuid
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .game_session import GameSession


class SessionManager:
    """全局 GameSession 管理。

    用法:
        mgr = SessionManager()
        session = mgr.create(ctx, services, llm, novel_path, chapter, ...)
        session = mgr.get(session_id)
    """

    def __init__(self):
        self._sessions: dict[str, "GameSession"] = {}

    def create(
        self,
        ctx,
        services,
        llm,
        novel_path: str,
        chapter: int,
        user_name: str,
        user_identity: str,
        npc_names: list[str],
    ) -> "GameSession":
        from .game_session import GameSession

        session_id = uuid.uuid4().hex[:12]
        session = GameSession(
            session_id=session_id,
            ctx=ctx,
            services=services,
            llm=llm,
            novel_path=novel_path,
            chapter=chapter,
            user_name=user_name,
            user_identity=user_identity,
            npc_names=npc_names,
        )
        self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> Optional["GameSession"]:
        return self._sessions.get(session_id)

    def remove(self, session_id: str):
        self._sessions.pop(session_id, None)

    @property
    def active_count(self) -> int:
        return len(self._sessions)
