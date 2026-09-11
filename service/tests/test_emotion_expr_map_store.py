"""旧 ``emotion_expr_map.json`` 迁移读点契约（写路径与端点已删除）。"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def map_file(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "emotion_expr_map.json"
        monkeypatch.setattr(
            "deskbot_server.emotion_expr_map_store.resolve_json_path",
            lambda _default: str(path),
        )
        yield path


def test_missing_legacy_file_reads_empty(map_file):
    from deskbot_server.emotion_expr_map_store import load_legacy_emotion_expr_map

    assert load_legacy_emotion_expr_map() == {}


def test_legacy_file_roundtrips_string_mappings(map_file):
    from deskbot_server.emotion_expr_map_store import load_legacy_emotion_expr_map

    map_file.write_text(
        json.dumps({"happy": "smile", "sad": "sad"}), encoding="utf-8"
    )
    assert load_legacy_emotion_expr_map() == {"happy": "smile", "sad": "sad"}


def test_legacy_file_with_non_string_scene_is_rejected(map_file):
    from deskbot_server.emotion_expr_map_store import load_legacy_emotion_expr_map

    map_file.write_text(json.dumps({"happy": 123}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_legacy_emotion_expr_map()


def test_store_module_is_read_only_migration_surface():
    """收缩契约：不再暴露 save/load 写分支，仅剩迁移读点。"""

    from deskbot_server import emotion_expr_map_store as store

    assert not hasattr(store, "save_emotion_expr_map")
    assert not hasattr(store, "load_emotion_expr_map")
    assert callable(store.load_legacy_emotion_expr_map)
