"""python -m deskbot_server.web"""

from __future__ import annotations

import logging
import os

from deskbot_server.env import load_dotenv
from deskbot_server.logging_setup import setup_logging
from deskbot_server.web.app import app, web_debug_enabled

logger = logging.getLogger("deskbot-server")

# 控制台的每个慢请求（米家云同步、LLM 对话、TTS 试听、复刻上传）都会占住
# 一个 worker；线程数要覆盖"几个慢请求 + 设备轮询 + 页面切换"同时发生。
_WEB_THREADS = int(os.environ.get("DESKBOT_WEB_THREADS") or "16")


def main() -> None:
    load_dotenv()
    # 换 waitress 后不再有开发服务器的默认输出：不显式配置日志的话，
    # 蓝图里的 logger.info（如「web 对话 round=…」）会被丢弃，排障时
    # 只剩一个空的 web-*.log。
    setup_logging()
    host = (os.environ.get("DESKBOT_WEB_HOST") or "127.0.0.1").strip()
    port = int(os.environ.get("DESKBOT_WEB_PORT") or "5050")
    if web_debug_enabled():
        # 调试模式要 reloader 与交互式回溯，只有 Werkzeug 开发服务器提供
        app.run(host=host, port=port, debug=True, threaded=True)
        return
    try:
        from waitress import serve
    except ImportError:
        logger.warning("waitress 未安装，退回 Flask 开发服务器（仅应出现在开发环境）")
        app.run(host=host, port=port, debug=False, threaded=True)
        return
    logger.info("控制台 waitress 监听 %s:%d threads=%d", host, port, _WEB_THREADS)
    serve(app, host=host, port=port, threads=_WEB_THREADS, ident=None)


if __name__ == "__main__":
    main()
