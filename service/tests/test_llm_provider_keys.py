"""大模型 Key 按提供方分别保存、切换自动带出（2026-09-14 用户要求：切了 DeepSeek 却沿用方舟 Key → 401）。"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def env_dir(tmp_path: Path, monkeypatch):
    from deskbot_server import device_data
    from deskbot_server import env as dotenv_module
    from deskbot_server.llm import config_state, env_store

    env_file = tmp_path / ".env"
    state_file = tmp_path / "llm-config-state.json"
    monkeypatch.setattr(dotenv_module, "ENV_FILE", env_file)
    monkeypatch.setattr(device_data, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(device_data, "LOCAL_DATA_ROOT", tmp_path / "data" / "local")
    monkeypatch.setattr(config_state, "ENV_FILE", env_file)
    monkeypatch.setattr(config_state, "LLM_CONFIG_STATE_FILE", state_file)
    monkeypatch.setattr(env_store, "LLM_CONFIG_STATE_FILE", state_file)
    monkeypatch.setattr(env_store, "validate_provider_http_url", lambda url: None)
    monkeypatch.setattr(dotenv_module, "_last_signature", None)
    monkeypatch.setattr(dotenv_module, "_file_managed_values", {})
    for key in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "LLM_PROTOCOL", "LLM_API_KEY_DEEPSEEK", "LLM_API_KEY_DOUBAO", "LLM_API_KEY_HOST_GW_EXAMPLE_COM"):
        monkeypatch.delenv(key, raising=False)
    env_file.write_text("LLM_API_KEY=ark-old\nLLM_MODEL=doubao-seed-2-0-lite-260428\nLLM_BASE_URL=https://ark.cn-beijing.volces.com/api/v3\n", encoding="utf-8")
    return env_file


def _env(path: Path) -> dict[str, str]:
    from deskbot_server.env import read_env_file

    return read_env_file() if path else {}


def test_provider_ids_follow_base_url_host():
    from deskbot_server.llm.provider_keys import (
        key_env_name,
        provider_id,
        provider_label,
        same_provider,
    )

    assert provider_id("https://api.deepseek.com") == "DEEPSEEK"
    assert provider_id("https://ark.cn-beijing.volces.com/api/v3") == "DOUBAO"
    assert provider_id("https://api.xiaomimimo.com/v1") == "MIMO"
    assert provider_id("https://gw.example.com/v1") == "HOST_GW_EXAMPLE_COM"
    assert provider_id("") == "" and key_env_name("") == ""
    assert key_env_name("https://api.deepseek.com") == "LLM_API_KEY_DEEPSEEK"
    assert provider_label("https://api.deepseek.com") == "DeepSeek"
    assert same_provider("https://api.deepseek.com", "https://api.deepseek.com/") is True
    assert same_provider("https://api.deepseek.com", "https://ark.cn-beijing.volces.com/api/v3") is False


def test_switching_provider_without_its_key_is_refused_then_keys_are_kept_per_provider(env_dir):
    from deskbot_server.llm.env_store import save_llm_env

    ark = "https://ark.cn-beijing.volces.com/api/v3"
    ds = "https://api.deepseek.com"
    # 只换提供方、不填 Key：拒绝，且 .env 原样（不会把方舟 Key 拿去调 DeepSeek）
    with pytest.raises(ValueError, match="DeepSeek"):
        save_llm_env({"base_url": ds, "model_name": "deepseek-flash"}, current_base_url=ark)
    assert _env(env_dir)["LLM_API_KEY"] == "ark-old" and "LLM_API_KEY_DEEPSEEK" not in _env(env_dir)
    # 填了 DeepSeek 的 Key：生效并各自记一份；原来的方舟 Key 记到豆包名下
    save_llm_env({"api_key": "sk-ds", "base_url": ds, "model_name": "deepseek-flash"}, current_base_url=ark)
    env = _env(env_dir)
    assert env["LLM_API_KEY"] == "sk-ds" and env["LLM_API_KEY_DEEPSEEK"] == "sk-ds" and env["LLM_API_KEY_DOUBAO"] == "ark-old"
    # 切回豆包不填 Key：自动带出方舟 Key
    save_llm_env({"base_url": ark, "model_name": "doubao-seed-2-0-lite-260428"}, current_base_url=ds)
    env = _env(env_dir)
    assert env["LLM_API_KEY"] == "ark-old" and env["LLM_API_KEY_DEEPSEEK"] == "sk-ds"
    # 同一家只换模型：Key 不动
    save_llm_env({"model_name": "doubao-seed-2-0-pro"}, current_base_url=ark)
    assert _env(env_dir)["LLM_API_KEY"] == "ark-old"
    # 自定义接入点按主机名存
    save_llm_env({"api_key": "gw-key", "base_url": "https://gw.example.com/v1", "model_name": "m"}, current_base_url=ark)
    env = _env(env_dir)
    assert env["LLM_API_KEY"] == "gw-key" and env["LLM_API_KEY_HOST_GW_EXAMPLE_COM"] == "gw-key"
    # 再切回 DeepSeek：带出 sk-ds
    save_llm_env({"base_url": ds, "model_name": "deepseek-flash"}, current_base_url="https://gw.example.com/v1")
    assert _env(env_dir)["LLM_API_KEY"] == "sk-ds"


def test_setup_routes_expose_saved_providers_and_use_them_for_tests(env_dir, monkeypatch):
    from deskbot_server.web.app import create_app

    app = create_app()
    app.config.update(TESTING=True)
    client = app.test_client()
    before = client.get("/api/setup/llm").get_json()
    assert before["saved_key_providers"] == []  # 老安装：一把 Key 还没归到提供方名下
    refused = client.patch("/api/setup/llm", json={"base_url": "https://api.deepseek.com", "model_name": "deepseek-flash", "protocol": "openai"})
    assert refused.status_code == 400 and "DeepSeek" in refused.get_json()["error"]
    saved = client.patch("/api/setup/llm", json={"api_key": "sk-ds", "base_url": "https://api.deepseek.com", "model_name": "deepseek-flash", "protocol": "openai"})
    assert saved.status_code == 200
    assert saved.get_json()["saved_key_providers"] == ["DEEPSEEK", "DOUBAO"]
    # 页面上切回豆包但还没保存就点「检测可用模型」：用豆包保存过的 Key，而不是当前生效的 DeepSeek Key
    seen = {}

    def _fake_list(api_key, base_url):
        seen["api_key"] = api_key
        return ["m1"]

    monkeypatch.setattr("deskbot_server.web.blueprints.app2c_bp._list_provider_models", _fake_list)
    res = client.post("/api/setup/llm/models", json={"base_url": "https://ark.cn-beijing.volces.com/api/v3"})
    assert res.status_code == 200 and seen["api_key"] == "ark-old"


def test_page_hints_saved_providers():
    html = (Path(__file__).resolve().parents[1] / "src/deskbot_server/web/templates/app2c/advanced.html").read_text(encoding="utf-8")
    assert ":placeholder=\"setupKeyPlaceholder\"" in html and "saved_key_providers" in html
    assert "providerIdFor(baseUrl)" in html and "['volces.com','DOUBAO']" in html
    assert "已分别保存 Key 的提供方" in html
