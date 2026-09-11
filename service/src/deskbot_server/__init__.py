"""deskbot_server"""

# 本机管线（LiveKit ws、内部 HTTP）绝不能走系统代理：livekit 的 Rust 信令
# 客户端读 HTTP(S)_PROXY 却不认 NO_PROXY，Clash 类工具开着时连
# ws://127.0.0.1:7880 都会被塞进代理，握手直接失败（HandshakeIncomplete）。
# Rust 运行时在 import 阶段就初始化，剥离必须发生在任何 livekit import 之前
# ——所以放在包入口，而不是 main()。对外 API（豆包/火山）均为国内直连，
# 历来无需代理；确需保留可设 DESKBOT_KEEP_PROXY=1。
import os as _os

if (_os.environ.get("DESKBOT_KEEP_PROXY") or "").strip() not in {"1", "true"}:
    for _name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        _os.environ.pop(_name, None)
for _name in ("NO_PROXY", "no_proxy"):
    _parts = {
        p.strip()
        for p in (_os.environ.get(_name) or "").split(",")
        if p.strip()
    }
    _os.environ[_name] = ",".join(sorted(_parts | {"localhost", "127.0.0.1", "::1"}))
del _os
