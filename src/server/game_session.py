# -*- coding: utf-8 -*-
"""GameSession —— 单个游戏会话。

持有 DirectorAgent + StoryState + NPCManager，
提供 run_message() 和 apply_choice() 方法供 HTTP handlers 调用。
"""

import asyncio
import json
import os
import queue
import threading
from typing import Optional

from ..interactive.story_state import StoryState, UserCharacter
from ..interactive.choice_engine import ChoiceEngine
from ..interactive.npc_manager import NPCManager
from ..interactive.director_agent import DirectorAgent


class GameSession:
    """单个游戏会话。

    每个浏览器窗口对应一个 GameSession。
    运行在独立线程中（因为 Director 内部有同步 LLM 调用）。
    """

    def __init__(
        self,
        session_id: str,
        ctx,
        services,
        llm,
        novel_path: str,
        chapter: int,
        user_name: str,
        user_identity: str,
        npc_names: list[str],
    ):
        self.session_id = session_id
        self._ctx = ctx
        self._services = services
        self._llm = llm

        # 事件队列（SSE 推送用）
        self._event_queue: queue.Queue = queue.Queue()
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # 初始化 StoryState
        total = len(ctx.novel.chapters) if ctx.novel else 0
        user_char = UserCharacter(
            name=user_name,
            identity=user_identity,
            backstory=f"第{chapter}章开始出现的{user_identity}",
            first_appearance_chapter=chapter,
        )
        self.state = StoryState(
            user_character=user_char,
            chapter=chapter,
            total_chapters=total,
        )
        for name in npc_names:
            self.state.intimacy[name] = 0

        # 初始化引擎
        self.choice_engine = ChoiceEngine(llm)
        self.npc_manager = NPCManager(ctx, services, llm, npc_names, chapter)
        self.npc_manager.set_user_context(user_name, self.state)

        self.director = DirectorAgent(
            ctx, services, llm, self.state, self.npc_manager, self.choice_engine,
        )

        self._novel_title = ctx.novel.title if ctx.novel else "未知"

    # ================================================================
    # 事件推送
    # ================================================================

    def emit(self, event_type: str, data: dict):
        """向事件队列推送一条 SSE 事件。"""
        self._event_queue.put({"type": event_type, **data})

    def get_event(self, timeout: float = 30.0) -> Optional[dict]:
        """从事件队列取一条事件（阻塞）。"""
        try:
            return self._event_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    # ================================================================
    # 游戏操作
    # ================================================================

    def start(self):
        """启动游戏——生成初始场景。在后台线程中运行。"""
        novel_name = self._novel_title
        scene = self._get_scene_info()
        first_task = (
            f"用户角色 {self.state.user_character.name} ({self.state.user_character.identity}) "
            f"已进入《{novel_name}》的世界。当前是第 {self.state.chapter} 章。"
            f"请用 narrate 设置初始场景，描述环境和在场人物，然后等待用户说话。"
        )

        self.emit("narrate", {
            "text": f"📖 《{novel_name}》\n第{self.state.chapter}章 | {scene}"
        })

        # 同步运行 Director
        self._run_director_sync(first_task)

    def send_message(self, text: str):
        """用户发送消息。"""
        self.state.total_turns += 1
        self.emit("user_message", {"text": text})
        self._run_director_sync(text)

    def apply_choice(self, choice_index: int):
        """用户选择了一个抉择选项。"""
        self.state.apply_choice(
            self.director._last_choice_result or {"choices": []},
            choice_index,
        )
        # 刷新 NPC 态度
        user_name = self.state.user_character.name
        self.npc_manager.set_user_context(user_name, self.state)
        self.director.refresh_identity()

        # 推举状态更新
        self.emit("state_update", {
            "intimacy": self.state.intimacy,
            "plot_flags": self.state.plot_flags,
            "active_ending": self.state.active_ending,
        })

        # 通知 Director 继续
        choice_msg = f"CHOICE:{choice_index}"
        self._run_director_sync(choice_msg)

    def _run_director_sync(self, task: str):
        """同步运行 Director，解析输出中的事件并推送到队列。"""
        try:
            result = asyncio.run(self.director.run(task))
        except Exception as e:
            self.emit("error", {"text": f"引擎错误: {e}"})
            return

        if not result:
            return

        # AgentResult 对象，取 output 字段（纯文本），
        # 避免把 tool_calls / steps 等元数据也序列化进去
        result_text = getattr(result, 'output', '') or str(result)

        # 检测抉择标记
        if "<!--CHOICE-->" in result_text:
            parts = result_text.split("<!--CHOICE-->", 1)
            normal = parts[0].strip()
            if "<!--ENDCHOICE-->" in parts[1]:
                choice_part = parts[1].split("<!--ENDCHOICE-->")[0].strip()
                after = parts[1].split("<!--ENDCHOICE-->")[-1].strip()
            else:
                choice_part = parts[1].strip()
                after = ""

            if normal:
                self._emit_parsed_text(normal)
            if choice_part:
                self._emit_choice_event(choice_part)
            if after:
                self._emit_parsed_text(after)
        else:
            self._emit_parsed_text(result_text)

    def _emit_parsed_text(self, text: str):
        """解析 Director 输出文本，按角色对话和旁白分类推送。"""
        for line in text.strip().split("\n"):
            line = line.strip()
            if not line:
                continue

            # 旁白: [旁白] xxx
            if line.startswith("[旁白]"):
                self.emit("narrate", {"text": line[len("[旁白]"):].strip()})
            # 角色对话: Name: xxx 或 Name：
            elif ":" in line and not line.startswith("[") and not line.startswith("http"):
                # 找第一个冒号位置
                idx = line.index(":")
                name = line[:idx].strip()
                content = line[idx + 1:].strip()
                if name and content and len(name) <= 10:
                    self.emit("dialogue", {"character": name, "text": content})
                    continue
                self.emit("narrate", {"text": line})
            else:
                self.emit("narrate", {"text": line})

    def _emit_choice_event(self, choice_text: str):
        """从抉择文本中提取结构化抉择数据。"""
        # 简单处理: 提取 moment + choices
        lines = choice_text.strip().split("\n")
        moment = ""
        choices = []

        for line in lines:
            line = line.strip()
            if "⚡" in line:
                moment = line.replace("⚡", "").strip()
            elif line.startswith("A)") or line.startswith("B)") or line.startswith("C)"):
                text = line[3:].strip() if len(line) > 3 else line[2:].strip()
                choices.append({"text": text, "letter": line[0]})

        if choices:
            self.emit("choice", {
                "moment": moment or "抉择时刻",
                "choices": choices,
            })

    def _get_scene_info(self) -> str:
        """获取场景信息字符串。"""
        return (
            f"第{self.state.chapter}/{self.state.total_chapters}章 | "
            f"👥 {self.npc_manager.get_present_characters_str()}"
        )
