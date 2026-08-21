from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor

import yaml


def _expression(name: str) -> dict:
    return {
        "name": name,
        "title": name,
        "frames": [
            {
                "ms": 200,
                "elements": {
                    "mouth": [],
                    "nose": [],
                    "eye_l": [],
                    "eye_r": [],
                    "extra": [],
                },
            }
        ],
    }


def test_face_design_update_serializes_complete_rmw(tmp_path, monkeypatch):
    global_dir = tmp_path / "global"
    global_dir.mkdir()
    design_path = global_dir / "deskbot-face.json"
    design_path.write_text(
        json.dumps(
            {
                "name": "test",
                "phonemes": [],
                "emotions": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("deskbot_server.device_data.DATA_DIR", tmp_path)
    monkeypatch.setattr(
        "deskbot_server.device_data.LOCAL_DATA_ROOT",
        tmp_path / "local",
    )

    from deskbot_server.face_design_store import update_face_design_file

    def _add_phoneme(doc):
        time.sleep(0.02)
        doc["phonemes"] = [_expression("a")]
        return doc

    def _add_emotion(doc):
        time.sleep(0.02)
        doc["emotions"] = [_expression("happy")]
        return doc

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(
            pool.map(
                update_face_design_file,
                [_add_phoneme, _add_emotion],
            )
        )

    local_path = tmp_path / "local" / "deskbot-face.json"
    stored = json.loads(local_path.read_text(encoding="utf-8"))
    assert [row["name"] for row in stored["phonemes"]] == ["a"]
    assert [row["name"] for row in stored["emotions"]] == ["happy"]


def test_config_update_serializes_complete_rmw(tmp_path):
    from deskbot_server.config import update_config

    config_path = tmp_path / "config.yaml"
    config_path.write_text("server:\n  port: 9000\ndebug: {}\n", encoding="utf-8")

    def _write_field(index: int) -> None:
        def _update(cfg):
            time.sleep(0.005)
            cfg.setdefault("debug", {})[f"field_{index}"] = index

        update_config(_update, config_path)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_write_field, range(20)))

    stored = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert stored["server"]["port"] == 9000
    assert stored["debug"] == {f"field_{index}": index for index in range(20)}


def test_tts_partial_save_preserves_omitted_env_keys(tmp_path, monkeypatch):
    from deskbot_server.tts import env_store

    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "DOUBAO_TTS_API_KEY=secret-key",
                "DOUBAO_TTS_SPEAKER=old-speaker",
                "DOUBAO_TTS_VOICE_STATUS_URL=https://example.com/status",
                "",
            ]
        ),
        encoding="utf-8",
    )
    from deskbot_server import env as dotenv_module

    monkeypatch.setattr(dotenv_module, "ENV_FILE", env_path)
    monkeypatch.setattr(dotenv_module, "load_dotenv", lambda **_kwargs: None)
    monkeypatch.setattr(env_store, "load_dotenv", lambda **_kwargs: None)

    env_store.save_doubao_tts_env({"speaker": "new-speaker"})

    stored = env_store.read_env_file()
    assert stored["DOUBAO_TTS_API_KEY"] == "secret-key"
    assert stored["DOUBAO_TTS_SPEAKER"] == "new-speaker"
    assert stored["DOUBAO_TTS_VOICE_STATUS_URL"] == "https://example.com/status"
