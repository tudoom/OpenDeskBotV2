from __future__ import annotations

import yaml


def test_checked_in_prompt_has_one_source_and_disables_legacy_display_plans():
    from pathlib import Path

    service_root = Path(__file__).resolve().parents[1]
    config = yaml.safe_load((service_root / "config.yaml").read_text(encoding="utf-8"))
    prompt = (service_root / "data" / "global" / "llm_system.txt").read_text(
        encoding="utf-8"
    )

    assert config["llm"]["system_prompt_file"] == "data/global/llm_system.txt"
    assert "system_prompt" not in config["llm"]
    assert "anims 始终写 []" in prompt
    assert '"anims": []' in prompt
    assert "每轮都要" not in prompt
    assert "必须有合适的 anims" not in prompt


def test_external_system_prompt_is_not_duplicated_into_yaml(tmp_path):
    from deskbot_server.config import load_config, save_config, update_config

    prompt_path = tmp_path / "prompt.txt"
    prompt_path.write_text("authoritative prompt", encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    save_config(
        {
            "llm": {
                "system_prompt_file": "prompt.txt",
                "system_prompt": 'stale prompt with "volume": 80',
                "model_name": "example",
            }
        },
        config_path,
    )

    stored = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert stored["llm"]["system_prompt_file"] == "prompt.txt"
    assert "system_prompt" not in stored["llm"]
    assert load_config(str(config_path))["llm"]["system_prompt"] == (
        "authoritative prompt"
    )

    def _inject_runtime_value(cfg):
        cfg["llm"]["system_prompt"] = "must not persist"

    update_config(_inject_runtime_value, config_path)
    stored = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert "system_prompt" not in stored["llm"]


def test_debug_prefs_migrate_once_from_config_then_local_file_wins(
    tmp_path, monkeypatch
):
    import json

    from deskbot_server import debug_prefs_store, device_data
    from deskbot_server.auto_reply import (
        get_asr_voice_auto_reply_enabled,
        set_asr_voice_auto_reply_enabled,
    )

    monkeypatch.setattr(device_data, "LOCAL_DATA_ROOT", tmp_path / "local")
    prefs_path = tmp_path / "local" / "debug_prefs.json"
    try:
        # No local file yet: the config.yaml key is the one-time migration
        # source and must be copied into data/local/debug_prefs.json.
        set_asr_voice_auto_reply_enabled(True)
        debug_prefs_store.apply_debug_prefs_from_config(
            {"debug": {"asr_auto_reply": False}}
        )
        assert get_asr_voice_auto_reply_enabled() is False
        assert json.loads(prefs_path.read_text(encoding="utf-8")) == {
            "asr_auto_reply": False
        }

        # Once the local file exists it wins over any config.yaml value.
        debug_prefs_store.persist_asr_auto_reply(True)
        debug_prefs_store.apply_debug_prefs_from_config(
            {"debug": {"asr_auto_reply": False}}
        )
        assert get_asr_voice_auto_reply_enabled() is True
    finally:
        set_asr_voice_auto_reply_enabled(True)


def test_persist_asr_auto_reply_never_writes_config_yaml(tmp_path, monkeypatch):
    import json

    from deskbot_server import debug_prefs_store, device_data
    from deskbot_server.auto_reply import set_asr_voice_auto_reply_enabled

    monkeypatch.setattr(device_data, "LOCAL_DATA_ROOT", tmp_path / "local")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("debug:\n  asr_auto_reply: true\n", encoding="utf-8")
    before = config_path.read_text(encoding="utf-8")
    try:
        debug_prefs_store.persist_asr_auto_reply(False)

        assert config_path.read_text(encoding="utf-8") == before
        stored = json.loads(
            (tmp_path / "local" / "debug_prefs.json").read_text(encoding="utf-8")
        )
        assert stored == {"asr_auto_reply": False}
    finally:
        set_asr_voice_auto_reply_enabled(True)
