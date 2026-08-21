from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.fixture()
def llm_config_env(tmp_path: Path, monkeypatch):
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
    monkeypatch.setattr(dotenv_module, "_last_signature", None)
    monkeypatch.setattr(dotenv_module, "_file_managed_values", {})
    managed_names = (*config_state.LLM_WRITABLE_ENV_KEYS, "ARK_API_KEY")
    original_env = {key: os.environ.get(key) for key in managed_names}
    for key in managed_names:
        monkeypatch.delenv(key, raising=False)
    try:
        yield env_file, state_file
    finally:
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _write_initial_env(path: Path) -> None:
    path.write_text(
        "\n".join(
            (
                "ARK_API_KEY=secret-original",
                "LLM_PROTOCOL=openai",
                "LLM_MODEL=gpt-original",
                "LLM_BASE_URL=https://llm.example.test/custom/",
                "UNRELATED=value",
                "",
            )
        ),
        encoding="utf-8",
    )


def test_partial_update_preserves_missing_and_masked_fields(llm_config_env):
    from deskbot_server.env import load_dotenv
    from deskbot_server.llm.config_state import get_llm_config_status
    from deskbot_server.llm.env_store import read_llm_env, save_llm_env

    env_file, state_file = llm_config_env
    _write_initial_env(env_file)
    load_dotenv(force_reload=True)

    status = save_llm_env(
        {
            "api_key": "••••••••",
            "model_name": "gpt-updated",
        }
    )

    values = read_llm_env()
    assert values["LLM_API_KEY"] == ""
    assert values["ARK_API_KEY"] == "secret-original"
    assert values["LLM_PROTOCOL"] == "openai"
    assert values["LLM_BASE_URL"] == "https://llm.example.test/custom/"
    assert values["LLM_MODEL"] == "gpt-updated"
    assert status["revision"] == 1
    assert status["applied_revision"] == 0
    assert status["pending"] is True
    assert get_llm_config_status()["applied"] is False

    persisted = state_file.read_text(encoding="utf-8")
    assert "secret-original" not in persisted
    assert json.loads(persisted)["digest"].startswith("sha256:")


def test_new_key_is_written_to_generic_name_without_migrating_legacy_key(
    llm_config_env,
):
    from deskbot_server.env import load_dotenv
    from deskbot_server.llm.env_store import read_llm_env, save_llm_env

    env_file, _state_file = llm_config_env
    _write_initial_env(env_file)
    load_dotenv(force_reload=True)

    save_llm_env({"api_key": "secret-new"})

    values = read_llm_env()
    assert values["LLM_API_KEY"] == "secret-new"
    assert values["ARK_API_KEY"] == "secret-original"
    persisted = env_file.read_text(encoding="utf-8")
    assert "LLM_API_KEY=secret-new" in persisted.splitlines()
    assert "ARK_API_KEY=secret-original" in persisted.splitlines()


def test_realtime_applies_generic_key_while_preserving_different_legacy_key(
    llm_config_env,
):
    from deskbot_server.env import load_dotenv
    from deskbot_server.llm.config_state import apply_pending_llm_config
    from deskbot_server.llm.env_store import read_llm_env, save_llm_env

    env_file, _state_file = llm_config_env
    _write_initial_env(env_file)
    load_dotenv(force_reload=True)
    pending = save_llm_env({"api_key": "secret-new"})

    status = apply_pending_llm_config()

    assert status["revision"] == pending["revision"]
    assert status["applied_revision"] == pending["revision"]
    assert status["applied"] is True
    assert status["apply_error"] == ""
    values = read_llm_env()
    assert values["LLM_API_KEY"] == "secret-new"
    assert values["ARK_API_KEY"] == "secret-original"


def test_only_realtime_apply_advances_applied_revision(llm_config_env, monkeypatch):
    from deskbot_server.env import load_dotenv
    from deskbot_server.llm import config_state
    from deskbot_server.llm.env_store import save_llm_env

    env_file, _state_file = llm_config_env
    _write_initial_env(env_file)
    load_dotenv(force_reload=True)

    first = save_llm_env({"model_name": "gpt-revision-1"})
    assert first["revision"] == 1
    assert first["applied_revision"] == 0

    applied = config_state.apply_pending_llm_config()
    assert applied["revision"] == 1
    assert applied["applied_revision"] == 1
    assert applied["applied"] is True

    second = save_llm_env({"protocol": "ark", "base_url": ""})
    assert second["revision"] == 2
    assert second["applied_revision"] == 1
    assert second["status"] == "pending"
    assert "LLM_BASE_URL=" in env_file.read_text(encoding="utf-8").splitlines()

    def fail_reload(*, force_reload: bool = False):
        assert force_reload is True
        raise RuntimeError("reload failed")

    monkeypatch.setattr(config_state, "load_dotenv", fail_reload)
    with pytest.raises(RuntimeError, match="reload failed"):
        config_state.apply_pending_llm_config()
    still_pending = config_state.get_llm_config_status()
    assert still_pending["revision"] == 2
    assert still_pending["applied_revision"] == 1
    assert still_pending["pending"] is True
    assert "reload failed" in still_pending["apply_error"]


def test_realtime_does_not_ack_process_environment_override(
    llm_config_env,
    monkeypatch,
):
    from deskbot_server.env import load_dotenv
    from deskbot_server.llm import config_state
    from deskbot_server.llm.env_store import save_llm_env

    env_file, _state_file = llm_config_env
    _write_initial_env(env_file)
    load_dotenv(force_reload=True)
    pending = save_llm_env({"api_key": "console-key"})
    monkeypatch.setenv("LLM_API_KEY", "operator-key")

    status = config_state.apply_pending_llm_config()

    assert status["revision"] == pending["revision"]
    assert status["applied_revision"] == 0
    assert status["pending"] is True
    assert "LLM_API_KEY" in status["apply_error"]


def test_legacy_ark_process_override_does_not_block_web_llm_revision(
    llm_config_env,
    monkeypatch,
):
    from deskbot_server.env import load_dotenv
    from deskbot_server.llm import config_state
    from deskbot_server.llm.env_store import save_llm_env

    env_file, _state_file = llm_config_env
    _write_initial_env(env_file)
    load_dotenv(force_reload=True)
    pending = save_llm_env({"model_name": "gpt-web-managed"})
    monkeypatch.setenv("ARK_API_KEY", "operator-ark-key")

    status = config_state.apply_pending_llm_config()

    assert status["revision"] == pending["revision"]
    assert status["applied_revision"] == pending["revision"]
    assert status["applied"] is True
    assert status["apply_error"] == ""


def test_legacy_ark_file_change_does_not_advance_web_llm_revision(llm_config_env):
    from deskbot_server.llm import config_state

    env_file, _state_file = llm_config_env
    _write_initial_env(env_file)
    first = config_state.get_llm_config_status()
    env_file.write_text(
        env_file.read_text(encoding="utf-8").replace(
            "ARK_API_KEY=secret-original",
            "ARK_API_KEY=secret-rotated-outside-web",
        ),
        encoding="utf-8",
    )

    second = config_state.get_llm_config_status()

    assert second["revision"] == first["revision"]


def test_setup_get_patch_round_trip_is_lossless_and_pending(
    llm_config_env,
    tmp_path: Path,
    monkeypatch,
):
    from deskbot_server.db import init_database
    from deskbot_server.db.engine import init_engine, reset_engine
    from deskbot_server.env import load_dotenv

    env_file, _state_file = llm_config_env
    _write_initial_env(env_file)
    load_dotenv(force_reload=True)

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DESKBOT_DB_PATH", str(db_path))
    reset_engine()
    init_engine(db_path)
    init_database()
    try:
        from deskbot_server.web.app import create_app

        client = create_app().test_client()

        before = client.get("/api/setup/llm")
        assert before.status_code == 200
        before_payload = before.get_json()
        assert before_payload["config"]["protocol"] == "openai"
        assert (
            before_payload["config"]["base_url"]
            == "https://llm.example.test/custom/"
        )
        assert before_payload["status"] == "pending"
        original_file = env_file.read_bytes()
        original_revision = before_payload["revision"]

        round_trip = client.patch(
            "/api/setup/llm",
            json=before_payload["config"],
        )
        assert round_trip.status_code == 200
        round_trip_payload = round_trip.get_json()
        assert round_trip_payload["config"]["protocol"] == "openai"
        assert (
            round_trip_payload["config"]["base_url"]
            == "https://llm.example.test/custom/"
        )
        assert round_trip_payload["revision"] == original_revision
        assert round_trip_payload["applied_revision"] == 0
        assert round_trip_payload["pending"] is True
        assert env_file.read_bytes() == original_file

        from deskbot_server.llm.config_state import apply_pending_llm_config

        applied = apply_pending_llm_config()
        assert applied["applied"] is True
        after_apply = client.get("/api/setup/llm").get_json()
        assert after_apply["revision"] == original_revision
        assert after_apply["applied_revision"] == original_revision
        assert after_apply["status"] == "applied"
        assert after_apply["pending"] is False

        partial = client.patch(
            "/api/setup/llm",
            json={
                "api_key": "********",
                "model_name": "gpt-partial-patch",
            },
        )
        assert partial.status_code == 200
        partial_payload = partial.get_json()
        assert partial_payload["config"]["protocol"] == "openai"
        assert (
            partial_payload["config"]["base_url"]
            == "https://llm.example.test/custom/"
        )
        assert partial_payload["revision"] == original_revision + 1
        assert partial_payload["applied_revision"] == original_revision
        assert partial_payload["status"] == "pending"
        from deskbot_server.llm.env_store import read_llm_env

        values = read_llm_env()
        assert values["ARK_API_KEY"] == "secret-original"
        assert values["LLM_PROTOCOL"] == "openai"
        assert values["LLM_BASE_URL"] == "https://llm.example.test/custom/"
    finally:
        reset_engine()


def test_setup_connection_test_uses_current_saved_configuration(
    llm_config_env,
    tmp_path: Path,
    monkeypatch,
):
    from deskbot_server.db import init_database
    from deskbot_server.db.engine import init_engine, reset_engine
    from deskbot_server.env import load_dotenv

    env_file, _state_file = llm_config_env
    _write_initial_env(env_file)
    load_dotenv(force_reload=True)

    db_path = tmp_path / "connection-test.db"
    monkeypatch.setenv("DESKBOT_DB_PATH", str(db_path))
    reset_engine()
    init_engine(db_path)
    init_database()
    try:
        from deskbot_server.web.app import create_app

        seen = {}

        def fake_chat_completion(_messages, *, config, **_kwargs):
            seen["config"] = config
            return "ok", {"model": config.model, "display_name": config.display_name}

        monkeypatch.setattr(
            "deskbot_server.web.blueprints.app2c_bp.chat_completion",
            fake_chat_completion,
        )
        client = create_app().test_client()

        response = client.post("/api/setup/llm/test", json={})

        assert response.status_code == 200
        assert response.get_json()["ok"] is True
        assert seen["config"].model == "gpt-original"
        assert seen["config"].protocol == "openai"
        assert seen["config"].api_base == "https://llm.example.test/custom/"
    finally:
        reset_engine()


def test_legacy_post_writes_generic_key_and_returns_revision_status(
    llm_config_env,
    tmp_path: Path,
    monkeypatch,
):
    from deskbot_server.db import init_database
    from deskbot_server.db.engine import init_engine, reset_engine
    from deskbot_server.env import load_dotenv
    from deskbot_server.llm.env_store import read_llm_env

    env_file, _state_file = llm_config_env
    _write_initial_env(env_file)
    load_dotenv(force_reload=True)

    db_path = tmp_path / "legacy.db"
    monkeypatch.setenv("DESKBOT_DB_PATH", str(db_path))
    reset_engine()
    init_engine(db_path)
    init_database()
    try:
        from deskbot_server.web.app import create_app

        client = create_app().test_client()

        response = client.post(
            "/api/setup/llm",
            json={
                "api_key": "secret-new",
                "model_name": "gpt-legacy-client",
            },
        )
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["revision"] == 1
        assert payload["applied_revision"] == 0
        assert payload["status"] == "pending"
        values = read_llm_env()
        assert values["LLM_API_KEY"] == "secret-new"
        assert values["ARK_API_KEY"] == "secret-original"
        assert values["LLM_MODEL"] == "gpt-legacy-client"
        assert values["LLM_PROTOCOL"] == "openai"
        assert values["LLM_BASE_URL"] == "https://llm.example.test/custom/"
    finally:
        reset_engine()


def test_independent_ark_key_saves_without_touching_llm_revision(llm_config_env):
    from deskbot_server.llm.config_state import get_llm_config_status
    from deskbot_server.llm.env_store import read_llm_env, save_llm_env

    env_file, _state_file = llm_config_env
    _write_initial_env(env_file)
    baseline = get_llm_config_status()

    save_llm_env({"ark_api_key": "ark-new"})
    values = read_llm_env()
    assert values["ARK_API_KEY"] == "ark-new"
    # ARK_API_KEY 不纳入配置 revision：实时服务不依赖它。
    assert get_llm_config_status()["revision"] == baseline["revision"]

    # 掩码/留空不覆盖已有值。
    save_llm_env({"ark_api_key": "ark-***"})
    assert read_llm_env()["ARK_API_KEY"] == "ark-new"
    save_llm_env({"ark_api_key": ""})
    assert read_llm_env()["ARK_API_KEY"] == "ark-new"
