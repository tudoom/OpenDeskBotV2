"""开发预览：在 5099 端口起 Web 控制台（不与已安装的 5050 服务冲突）。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, r"C:\tmp\odbfix\service\src")
os.environ["DESKBOT_WEB_PORT"] = os.environ.get("PORT") or "5123"

from deskbot_server.web.__main__ import main

if __name__ == "__main__":
    main()
