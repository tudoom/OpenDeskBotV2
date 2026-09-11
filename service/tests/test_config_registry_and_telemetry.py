"""配置登记棘轮 + 遥测基本契约。"""

from __future__ import annotations

import re
from pathlib import Path

from deskbot_server import config_registry as cr
from deskbot_server import telemetry

SRC = Path(__file__).resolve().parents[1] / "src" / "deskbot_server"
_ENV_GET = re.compile(r'environ\.get\(\s*"([A-Z0-9_]+)"')


def test_every_environ_key_in_code_is_registered():
    """新出现的 os.environ.get("X") 必须先在 config_registry.ENV_KEYS 登记。"""
    unregistered: set[str] = set()
    for path in SRC.rglob("*.py"):
        if path.name == "config_registry.py":
            continue
        for key in _ENV_GET.findall(path.read_text(encoding="utf-8")):
            if key not in cr.ENV_KEYS:
                unregistered.add(key)
    assert not unregistered, f"未登记的环境变量键: {sorted(unregistered)}"


def test_effective_settings_masks_secrets(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "ark-1234567890")
    monkeypatch.setenv("LLM_MODEL", "doubao-x")
    rows = {r["key"]: r for r in cr.effective_settings(env_file_keys={"LLM_MODEL"})}
    assert rows["LLM_API_KEY"]["value"] == "ark…" and rows["LLM_API_KEY"]["source"] == "process"
    assert rows["LLM_MODEL"]["value"] == "doubao-x" and rows["LLM_MODEL"]["source"] == ".env"


def test_telemetry_emit_recent_and_scrub(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKBOT_SERVER_LOG_FILE", str(tmp_path / "core.log"))
    telemetry.reset_for_tests()
    telemetry._logger = None  # 让本测试用临时目录
    telemetry.emit("session.ready", device_id="d1", transport="wifi_tcp", api_key="secret")
    telemetry.emit("tool.exec", channel="web", tools="miot", ok=True, ms=12)
    rows = telemetry.recent(10)
    assert [r["event"] for r in rows] == ["tool.exec", "session.ready"]
    assert rows[1]["api_key"] == "***"
    assert telemetry.recent(10, event_prefix="tool.")[0]["ms"] == 12
    written = (tmp_path / "telemetry.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(written) == 2 and '"event": "session.ready"' in written[0]


def test_web_entrypoint_configures_logging():
    """换 waitress 后没有开发服务器兜底：不配日志，蓝图里的 info 全部丢弃。"""
    src = (SRC / "web" / "__main__.py").read_text(encoding="utf-8")
    assert "setup_logging()" in src
    assert "waitress" in src
