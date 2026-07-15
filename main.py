# -*- coding: utf-8 -*-
"""Novel2Comic —— 续写引擎入口。

用法:
    python main.py write --novel novels/poyun.txt
    python main.py server --port 8000
    python main.py frontend --port 3000
"""

import asyncio
import os
import sys

# 自动加载 .env 文件
def _load_dotenv():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip().strip("\"'")
                if key and key not in os.environ:
                    os.environ[key] = val

_load_dotenv()

# Windows 修复
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from novel2comic.src.cli.cli import main

if __name__ == "__main__":
    main()
