"""模型配置页「联网检索」：Key 写入/清除、状态、测试接口、页面入口。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.test_app2c_pages import local_client  # noqa: F401 - pytest fixture

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def env_file(tmp_path, monkeypatch):
    from deskbot_server import env as dotenv_module

    for key in ("ARK_WEB_SEARCH_API_KEY", "ARK_WEB_SEARCH_MODEL"):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / ".env"
    monkeypatch.setattr(dotenv_module, "ENV_FILE", path)
    monkeypatch.setattr(dotenv_module, "_last_signature", None)
    monkeypatch.setattr(dotenv_module, "_file_managed_values", {})
    return path


def _llm(monkeypatch, base, key="sk-llm"):
    from deskbot_server.llm.runtime import ResolvedLlmConfig

    cfg = ResolvedLlmConfig(model="m", api_key=key, api_base=base, protocol="openai", source="test", display_name="t")
    monkeypatch.setattr("deskbot_server.llm.runtime.resolve_llm_config", lambda: cfg)


def test_save_and_clear_dedicated_key(monkeypatch, env_file):
    from deskbot_server.websearch_env_store import save_websearch_env, websearch_config_status

    _llm(monkeypatch, "https://api.deepseek.com")
    assert websearch_config_status()["api_key_source"] == "none"
    status = save_websearch_env({"api_key": "sk-search"})
    assert status["api_key_source"] == "own" and status["api_key_set"] is True
    text = env_file.read_text(encoding="utf-8")
    assert "# 联网检索（火山方舟）" in text and "ARK_WEB_SEARCH_API_KEY=sk-search" in text
    assert os.environ["ARK_WEB_SEARCH_API_KEY"] == "sk-search"
    # 掩码 / 空值不覆盖
    assert save_websearch_env({"api_key": "••••"})["api_key_source"] == "own"
    assert "ARK_WEB_SEARCH_API_KEY=sk-search" in env_file.read_text(encoding="utf-8")
    status = save_websearch_env({"clear_api_key": True})
    assert status["api_key_source"] == "none"
    assert "ARK_WEB_SEARCH_API_KEY=\n" in env_file.read_text(encoding="utf-8")


def test_reuses_llm_key_when_llm_is_ark(monkeypatch, env_file):
    from deskbot_server.websearch_env_store import websearch_config_status

    _llm(monkeypatch, "https://ark.cn-beijing.volces.com/api/v3", key="sk-ark")
    status = websearch_config_status()
    assert status["api_key_source"] == "llm" and status["llm_is_ark"] and status["api_key_set"]
    assert "sk-ark" not in str(status)


def test_live_test_reports_missing_key_and_ark_result(monkeypatch, env_file):
    from deskbot_server import websearch_env_store as store

    _llm(monkeypatch, "https://api.deepseek.com")
    missing = store.test_websearch()
    assert not missing["ok"] and "火山方舟" in missing["error"]

    monkeypatch.setenv("ARK_WEB_SEARCH_API_KEY", "sk-search")
    seen = {}

    def fake_ark(q, *, limit):
        seen["q"] = q
        return {"ok": True, "provider": "ark_web_search", "abstract": "北京今天晴，最高 28℃。", "results": [{"url": "u"}]}

    monkeypatch.setattr(store, "_ark_web_search", fake_ark)
    ok = store.test_websearch()
    assert ok["ok"] and ok["reply"].startswith("北京今天晴") and ok["model"] == "doubao-seed-2-0-lite-260428"
    assert seen["q"] == "今天北京的天气怎么样"
    monkeypatch.setattr(store, "_ark_web_search", lambda q, *, limit: {"ok": False, "error": "HTTP 401: bad key"})
    bad = store.test_websearch()
    assert not bad["ok"] and "HTTP 401" in bad["error"]


def test_routes_and_page(local_client, monkeypatch, env_file):
    from deskbot_server import websearch_env_store as store

    client, _app = local_client
    _llm(monkeypatch, "https://api.deepseek.com")
    r = client.get("/api/setup/websearch")
    assert r.status_code == 200 and r.get_json()["api_key_source"] == "none"
    r = client.post("/api/setup/websearch/test", json={})
    assert r.status_code == 400 and "火山方舟" in r.get_json()["error"]
    r = client.patch("/api/setup/websearch", json={"api_key": "sk-search"})
    assert r.status_code == 200 and r.get_json()["api_key_source"] == "own"
    assert "sk-search" not in r.get_data(as_text=True)
    monkeypatch.setattr(store, "_ark_web_search", lambda q, *, limit: {"ok": True, "abstract": "晴", "results": []})
    r = client.post("/api/setup/websearch/test", json={})
    assert r.status_code == 200 and r.get_json()["reply"] == "晴"

    html = client.get("/advanced").get_data(as_text=True)
    assert "<h2>联网检索</h2>" in html and "/api/setup/websearch" in html
    assert "已复用大模型的火山方舟 Key" in html and 'v-model="websearch.api_key"' in html
    assert "03 · 必需配置" in html and "04 · 声音能力" in html  # 顺序：大模型 → 联网检索 → ASR → TTS


def test_env_keys_registered_and_documented():
    from deskbot_server import config_registry as cr

    assert cr.ENV_KEYS["ARK_WEB_SEARCH_API_KEY"][1] is True
    assert "ARK_WEB_SEARCH_MODEL" in cr.ENV_KEYS
    example = (ROOT / "service/.env.example").read_text(encoding="utf-8")
    assert "# ARK_WEB_SEARCH_API_KEY=" in example
