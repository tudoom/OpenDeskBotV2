from __future__ import annotations

import json


def _isolate(monkeypatch, tmp_path):
    from deskbot_server import device_data, local_face_data

    data = tmp_path / "data"
    monkeypatch.setattr(device_data, "DATA_DIR", data)
    monkeypatch.setattr(device_data, "LOCAL_DATA_ROOT", data / "local")
    return device_data, local_face_data, data


def test_persistent_paths_have_one_pc_local_profile(monkeypatch, tmp_path):
    device_data, _face, data = _isolate(monkeypatch, tmp_path)

    assert device_data.local_data_dir() == data / "local"
    assert device_data.resolve_json_path("C:/seed/memories.json") == str(
        data / "local" / "memories.json"
    )


def test_local_profile_is_seeded_only_from_global(monkeypatch, tmp_path):
    device_data, _face, data = _isolate(monkeypatch, tmp_path)
    legacy = data / "device" / "old"
    legacy.mkdir(parents=True)
    (legacy / "servo.json").write_text('{"source":"legacy"}', encoding="utf-8")
    template = data / "global" / "servo.json"
    template.parent.mkdir(parents=True)
    template.write_text('{"source":"global"}', encoding="utf-8")

    assert device_data.ensure_local_data_initialized() is True
    payload = json.loads((data / "local" / "servo.json").read_text(encoding="utf-8"))
    assert payload == {"source": "global"}
    assert device_data.ensure_local_data_initialized() is False


def test_face_library_is_pc_local_and_seeded_from_global(monkeypatch, tmp_path):
    _device_data, local_face_data, data = _isolate(monkeypatch, tmp_path)
    template = data / "global" / "deskbot-face.json"
    template.parent.mkdir(parents=True)
    template.write_text(
        '{"name":"seed","phonemes":[],"emotions":[]}', encoding="utf-8"
    )

    first = local_face_data.ensure_face_data_file()
    assert first == data / "local" / "deskbot-face.json"
    assert json.loads(first.read_text(encoding="utf-8"))["name"] == "seed"

    first.write_text(
        '{"name":"edited","phonemes":[],"emotions":[]}', encoding="utf-8"
    )
    assert local_face_data.ensure_face_data_file() == first
    assert json.loads(first.read_text(encoding="utf-8"))["name"] == "edited"


def test_memory_library_is_pc_local(monkeypatch, tmp_path):
    _device_data, _face, _data = _isolate(monkeypatch, tmp_path)
    from deskbot_server.memory_store import add_memory, list_memories

    saved = add_memory("shared local memory")
    visible = list_memories()

    assert [row["id"] for row in visible] == [saved["id"]]
