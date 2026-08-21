from __future__ import annotations

import json
from pathlib import Path

import pytest

from deskbot_server.face_design_store import (
    FaceDesignRevisionConflict,
    clear_face_design_cache,
    load_face_design_file,
)
from deskbot_server.face_expression_transactions import (
    FaceExpressionPermissionError,
    apply_face_expression_transaction,
    get_face_expression_state,
)


def _frame(marker: int = 1) -> dict:
    return {
        "ms": 400,
        "elements": {
            "eye_l": [{"shape": "pixel", "x": marker, "y": 2, "c": 65535}],
            "eye_r": [],
            "nose": [],
            "mouth": [],
            "extra": [],
        },
    }


@pytest.fixture()
def face_root(monkeypatch, tmp_path: Path) -> Path:
    global_dir = tmp_path / "global"
    local_dir = tmp_path / "local"
    global_dir.mkdir()
    (global_dir / "deskbot-face.json").write_text(
        json.dumps(
            {
                "name": "test-face",
                "phonemes": [],
                "emotions": [
                    {"name": "idle", "title": "待机", "frames": [_frame(1)]},
                    {"name": "happy", "title": "开心", "frames": [_frame(2)]},
                    {
                        # A shipped example whose historical prefix must not
                        # cause it to be treated as user-owned.
                        "name": "user_expression_example",
                        "title": "系统示例",
                        "frames": [_frame(3)],
                    },
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    local_dir.mkdir()
    (local_dir / "emotion_expr_map.json").write_text(
        json.dumps({"happy": "smile"}), encoding="utf-8"
    )
    monkeypatch.setattr("deskbot_server.device_data.DATA_DIR", tmp_path)
    monkeypatch.setattr("deskbot_server.device_data.LOCAL_DATA_ROOT", local_dir)
    clear_face_design_cache()
    yield tmp_path
    clear_face_design_cache()


def _create_payload(*, expected_revision: int = 0, title: str = "我的表情") -> dict:
    return {
        "expected_revision": expected_revision,
        "scenes": {
            "create": [
                {
                    "title": title,
                    "alias": ["mine"],
                    "frames": [_frame(8), {**_frame(9), "ms": 900}],
                }
            ]
        },
    }


@pytest.fixture()
def client(face_root: Path, monkeypatch):
    db_path = face_root / "test.db"
    monkeypatch.setenv("DESKBOT_DB_PATH", str(db_path))
    from deskbot_server.db import init_database
    from deskbot_server.db.engine import init_engine, reset_engine
    from deskbot_server.web.app import create_app

    reset_engine()
    init_engine(db_path)
    init_database()
    try:
        yield create_app().test_client()
    finally:
        reset_engine()


def test_legacy_doc_gets_explicit_origin_and_repairs_split_mapping(face_root: Path):
    state = get_face_expression_state()
    assert state["revision"] == 0
    assert state["map"] == {"happy": "smile"}
    examples = [row for row in state["config"] if row["name"] == "user_expression_example"]
    assert examples[0]["origin"] == "system"

    saved = apply_face_expression_transaction(_create_payload())
    assert saved["revision"] == 1
    assert saved["map"] == {"happy": "happy"}
    on_disk = json.loads(
        (face_root / "local" / "deskbot-face.json").read_text(encoding="utf-8")
    )
    assert on_disk["schema_version"] == 2
    assert all(row["origin"] == "system" for row in on_disk["emotions"][:-1])


def test_legacy_origin_migration_uses_global_membership_not_name_prefix(face_root: Path):
    global_file = face_root / "global" / "deskbot-face.json"
    legacy = json.loads(global_file.read_text(encoding="utf-8"))
    legacy["emotions"].append(
        {
            "name": "user_expression_real_saved",
            "title": "旧版用户保存",
            "frames": [_frame(6)],
        }
    )
    local_file = face_root / "local" / "deskbot-face.json"
    local_file.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    clear_face_design_cache()

    doc = load_face_design_file(seed_if_missing=False)
    assert doc is not None
    origins = {row["name"]: row["origin"] for row in doc["emotions"]}
    assert origins["user_expression_example"] == "system"
    assert origins["user_expression_real_saved"] == "user"


def test_create_update_delete_user_scene_without_losing_frames(face_root: Path):
    created_result = apply_face_expression_transaction(_create_payload())
    created = created_result["created"][0]
    assert created["name"].startswith("user_expression_")
    assert created["origin"] == "user"
    assert [frame["ms"] for frame in created["frames"]] == [400, 900]

    updated_result = apply_face_expression_transaction(
        {
            "expected_revision": 1,
            "scenes": {
                "update": [
                    {
                        **created,
                        "title": "改名后",
                        "frames": [created["frames"][0], {**created["frames"][1], "ms": 1200}],
                    }
                ]
            },
            "map_patch": {"idle": created["name"]},
        }
    )
    updated = next(row for row in updated_result["config"] if row["name"] == created["name"])
    assert updated["title"] == "改名后"
    assert [frame["ms"] for frame in updated["frames"]] == [400, 1200]
    assert updated_result["map"]["idle"] == created["name"]

    deleted_result = apply_face_expression_transaction(
        {
            "expected_revision": 2,
            "scenes": {"delete": [created["name"]]},
        }
    )
    assert all(row["name"] != created["name"] for row in deleted_result["config"])
    assert deleted_result["map"]["idle"] == "idle"


def test_create_and_map_new_scene_in_one_atomic_transaction(face_root: Path):
    result = apply_face_expression_transaction(
        {
            "expected_revision": 0,
            "scenes": {
                "create": [{"title": "设备默认", "frames": [_frame(7)]}]
            },
            "map_create_refs": {"idle": 0},
        }
    )

    created = result["created"][0]
    assert result["revision"] == 1
    assert result["map"]["idle"] == created["name"]
    assert next(
        row for row in result["config"] if row["name"] == created["name"]
    )["title"] == "设备默认"


def test_invalid_created_scene_mapping_is_atomic(face_root: Path):
    with pytest.raises(ValueError, match="out of range"):
        apply_face_expression_transaction(
            {
                "expected_revision": 0,
                "scenes": {
                    "create": [{"title": "不会落盘", "frames": [_frame(7)]}]
                },
                "map_create_refs": {"idle": 1},
            }
        )

    state = get_face_expression_state()
    assert state["revision"] == 0
    assert all(row.get("title") != "不会落盘" for row in state["config"])


def test_transaction_preserves_web_editor_metadata_without_sending_it_as_elements(
    face_root: Path,
):
    frame = _frame(8)
    frame["editor_parts"] = {
        "cheek_l": {
            "visible": False,
            "shape": "drop_fill",
            "width": 24,
            "height": 14,
            "size": 100,
            "x": 58,
            "y": 137,
            "color": "#fa6040",
            "source": [],
        }
    }
    created = apply_face_expression_transaction(
        {
            "expected_revision": 0,
            "scenes": {"create": [{"title": "带编辑状态", "frames": [frame]}]},
        }
    )["created"][0]

    assert created["frames"][0]["editor_parts"]["cheek_l"]["shape"] == "drop_fill"
    assert "editor_parts" not in created["frames"][0]["elements"]


def test_system_scene_requires_copy_and_prefix_does_not_grant_ownership(face_root: Path):
    for name in ("idle", "user_expression_example"):
        with pytest.raises(FaceExpressionPermissionError):
            apply_face_expression_transaction(
                {
                    "expected_revision": 0,
                    "scenes": {"update": [{"name": name, "frames": [_frame(7)]}]},
                }
            )
    copied = apply_face_expression_transaction(
        {
            "expected_revision": 0,
            "scenes": {
                "create": [{"title": "待机副本", "frames": [_frame(7)]}]
            },
        }
    )["created"][0]
    assert copied["name"] != "idle"
    assert copied["origin"] == "user"


def test_revision_conflict_and_invalid_map_are_atomic(face_root: Path):
    first = apply_face_expression_transaction(_create_payload())
    with pytest.raises(FaceDesignRevisionConflict):
        apply_face_expression_transaction(_create_payload(expected_revision=0))
    with pytest.raises(ValueError, match="unknown scene"):
        apply_face_expression_transaction(
            {"expected_revision": 1, "map_patch": {"idle": "missing_scene"}}
        )
    doc = load_face_design_file(seed_if_missing=False)
    assert doc is not None
    assert doc["revision"] == first["revision"] == 1
    assert len([row for row in doc["emotions"] if row["origin"] == "user"]) == 1


def test_transaction_requires_revision_and_does_not_write(face_root: Path):
    with pytest.raises(ValueError, match="expected_revision is required"):
        apply_face_expression_transaction(
            {
                "scenes": {
                    "create": [{"title": "missing revision", "frames": [_frame(7)]}]
                }
            }
        )
    state = get_face_expression_state()
    assert state["revision"] == 0
    assert not [row for row in state["config"] if row["origin"] == "user"]


@pytest.mark.parametrize(
    "primitive",
    [
        {"shape": "svg_path", "d": "M0 0L1 1"},
        {"shape": "future_unknown_shape", "x": 1, "y": 2},
    ],
)
def test_transaction_rejects_browser_only_shapes_before_device_black_screen(
    face_root: Path, primitive: dict
):
    with pytest.raises(ValueError, match="unsupported device expression shape"):
        apply_face_expression_transaction(
            {
                "expected_revision": 0,
                "scenes": {
                    "create": [
                        {
                            "title": "坏场景",
                            "frames": [
                                {
                                    "ms": 400,
                                    "elements": {"extra": [primitive]},
                                }
                            ],
                        }
                    ]
                },
            }
        )
    assert get_face_expression_state()["revision"] == 0


def test_transaction_rejects_expression_frame_over_primitive_limit(
    face_root: Path,
):
    primitives = [
        {"shape": "pixel", "x": index % 284, "y": index % 240, "c": 65535}
        for index in range(17)
    ]
    with pytest.raises(
        ValueError, match="frame 0 layer 'extra' exceeds 16 primitives"
    ):
        apply_face_expression_transaction(
            {
                "expected_revision": 0,
                "scenes": {
                    "create": [
                        {
                            "title": "too many primitives",
                            "frames": [
                                {"ms": 400, "elements": {"extra": primitives}}
                            ],
                        }
                    ]
                },
            }
        )
    assert get_face_expression_state()["revision"] == 0


def test_transaction_rejects_expression_that_cannot_fit_pb_wire(
    face_root: Path,
):
    primitives = [
        {
            "shape": "text",
            "x": index,
            "y": 20,
            "text": "device-bound-expression-" * 8,
            "size": 12,
        }
        for index in range(100)
    ]
    with pytest.raises(ValueError, match="exceeds 16 primitives"):
        apply_face_expression_transaction(
            {
                "expected_revision": 0,
                "scenes": {
                    "create": [
                        {
                            "title": "oversized wire payload",
                            "frames": [
                                {"ms": 400, "elements": {"extra": primitives}}
                            ],
                        }
                    ]
                },
            }
        )
    assert get_face_expression_state()["revision"] == 0


def test_transaction_api_snapshot_conflict_and_permission_status(client):
    snapshot = client.get("/api/face_expression_transaction")
    assert snapshot.status_code == 200
    assert snapshot.get_json()["revision"] == 0

    created = client.post("/api/face_expression_transaction", json=_create_payload())
    assert created.status_code == 200
    assert created.get_json()["created"][0]["origin"] == "user"

    conflict = client.post("/api/face_expression_transaction", json=_create_payload())
    assert conflict.status_code == 409
    assert conflict.get_json()["error"] == "revision_conflict"

    forbidden = client.post(
        "/api/face_expression_transaction",
        json={
            "expected_revision": 1,
            "scenes": {"update": [{"name": "idle", "frames": [_frame(4)]}]},
        },
    )
    assert forbidden.status_code == 403

    missing_revision = client.post(
        "/api/face_expression_transaction",
        json={"map_patch": {"idle": "idle"}},
    )
    assert missing_revision.status_code == 400
    assert missing_revision.get_json()["error"] == "expected_revision is required"
