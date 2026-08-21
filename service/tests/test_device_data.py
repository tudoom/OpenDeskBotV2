from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor


def _isolate(tmp_path, monkeypatch):
    from deskbot_server import device_data as store

    data_dir = tmp_path / "data"
    global_dir = data_dir / "global"
    global_dir.mkdir(parents=True)
    monkeypatch.setattr(store, "DATA_DIR", data_dir)
    monkeypatch.setattr(store, "LOCAL_DATA_ROOT", data_dir / "local")
    return store, data_dir, global_dir


def test_local_data_initialization_copies_missing_global_seeds(tmp_path, monkeypatch):
    store, data_dir, global_dir = _isolate(tmp_path, monkeypatch)
    servo = {"xMin": 0, "xMax": 180, "yMin": 0, "yMax": 180}
    (global_dir / "servo.json").write_text(json.dumps(servo), encoding="utf-8")
    (global_dir / "device_volume.json").write_text(
        '{"default": 80}\n', encoding="utf-8"
    )

    assert store.ensure_local_data_initialized() is True
    assert json.loads(
        (data_dir / "local" / "servo.json").read_text(encoding="utf-8")
    ) == servo
    assert (data_dir / "local" / "device_volume.json").is_file()
    assert store.ensure_local_data_initialized() is False
    assert not (data_dir / ".local-profile-initialized").exists()


def test_local_initialization_never_imports_legacy_device_profiles(
    tmp_path, monkeypatch
):
    store, data_dir, global_dir = _isolate(tmp_path, monkeypatch)
    legacy = data_dir / "device" / "old-robot"
    legacy.mkdir(parents=True)
    (legacy / "servo.json").write_text('{"xMax": 1}', encoding="utf-8")
    (global_dir / "servo.json").write_text('{"xMax": 180}', encoding="utf-8")

    store.ensure_local_data_initialized()

    assert json.loads(
        (data_dir / "local" / "servo.json").read_text(encoding="utf-8")
    ) == {"xMax": 180}


def test_concurrent_local_initialization_keeps_valid_seed_files(
    tmp_path, monkeypatch
):
    store, data_dir, global_dir = _isolate(tmp_path, monkeypatch)
    (global_dir / "servo.json").write_text('{"xMax": 180}\n', encoding="utf-8")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _index: store.ensure_local_data_initialized(), range(20)))

    assert json.loads(
        (data_dir / "local" / "servo.json").read_text(encoding="utf-8")
    ) == {"xMax": 180}


def test_resolve_json_path_always_targets_local_profile(tmp_path, monkeypatch):
    store, data_dir, _global_dir = _isolate(tmp_path, monkeypatch)

    assert store.local_data_dir() == data_dir / "local"
    assert store.resolve_json_path("C:/seed/memories.json") == str(
        data_dir / "local" / "memories.json"
    )


def test_load_and_save_llm_system_prompt(tmp_path, monkeypatch):
    store, data_dir, global_dir = _isolate(tmp_path, monkeypatch)
    (global_dir / "llm_system.txt").write_text("全局 prompt\n", encoding="utf-8")

    assert store.load_llm_system_prompt() == "全局 prompt"
    saved = store.save_llm_system_prompt("更新后的 prompt")

    assert saved == data_dir / "local" / "llm_system.txt"
    assert store.load_llm_system_prompt() == "更新后的 prompt"
    assert (global_dir / "llm_system.txt").read_text(encoding="utf-8") == (
        "全局 prompt\n"
    )
