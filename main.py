# -*- coding: utf-8 -*-
"""Novel2Comic 新入口 —— 多 Agent 架构。

用法:
    python main.py comic --text "小说片段" --title 第一章
    python main.py comic --novel novels/poyun.txt --chapter 3
    python main.py continue --novel novels/poyun.txt --from-chapter 50
    python main.py roleplay --novel novels/poyun.txt --character 苏墨
"""

import asyncio
import os
import sys

# Windows 修复
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from novel2comic.src.cli.cli import main

if __name__ == "__main__":
    asyncio.run(main())
