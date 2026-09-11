from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from deskbot_server.face_expr_scenes_store import (
    load_face_expr_scenes_file,
    normalize_face_expr_scenes,
    save_face_expr_scenes_file,
)


def _minimal_design_doc() -> dict:
    return {"name": "test", "phonemes": [], "emotions": []}


@pytest.fixture()
def design_file(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        global_dir = root / "global"
        global_dir.mkdir()
        design_path = global_dir / "deskbot-face.json"
        design_path.write_text(
            json.dumps(_minimal_design_doc(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr("deskbot_server.device_data.DATA_DIR", root)
        monkeypatch.setattr("deskbot_server.device_data.LOCAL_DATA_ROOT", root / "local")
        from deskbot_server.face_design_store import clear_face_design_cache

        clear_face_design_cache()
        yield design_path


def _custom_default_scene() -> dict:
    return {
        "name": "default",
        "title": "我的默认眨眼",
        "frames": [
            {
                "ms": 999,
                "elements": {
                    "mouth": [],
                    "nose": [{"shape": "circle", "x": 1, "y": 2, "r": 3}],
                    "eye_l": [{"shape": "ellipse_fill", "x": 4, "y": 5, "rw": 6, "rh": 7}],
                    "eye_r": [{"shape": "ellipse_fill", "x": 8, "y": 9, "rw": 10, "rh": 11}],
                    "extra": [],
                },
            }
        ],
    }


def test_save_preserves_custom_default(design_file: Path):
    custom = _custom_default_scene()
    saved = save_face_expr_scenes_file([custom])
    assert saved[0]["frames"][0]["ms"] == 999

    reloaded = load_face_expr_scenes_file(seed_if_missing=False)
    assert reloaded is not None
    default_row = next(r for r in reloaded if r["name"] == "default")
    assert default_row["title"] == "我的默认眨眼"
    assert default_row["frames"][0]["ms"] == 999

    local_file = design_file.parent.parent / "local" / design_file.name
    on_disk = json.loads(local_file.read_text(encoding="utf-8"))
    assert on_disk["emotions"][0]["frames"][0]["ms"] == 999


def test_save_preserves_custom_scene_and_reload(design_file: Path):
    from deskbot_server.face_expr_scenes_store import builtin_emotion_scenes

    assert "wry_smile" not in {row["name"] for row in builtin_emotion_scenes()}

    scene = {
        "name": "my_test_scene",
        "title": "测试",
        "frames": [
            {
                "ms": 500,
                "elements": {
                    "mouth": [{"shape": "circle", "x": 10, "y": 20, "r": 3}],
                    "nose": [],
                    "eye_l": [],
                    "eye_r": [],
                    "extra": [],
                },
            }
        ],
    }
    save_face_expr_scenes_file([scene])
    rows = load_face_expr_scenes_file(seed_if_missing=False)
    assert len(rows) == 1 + len(builtin_emotion_scenes())
    mine = next(r for r in rows if r["name"] == "my_test_scene")
    assert mine["frames"][0]["elements"]["mouth"][0]["x"] == 10
    local_file = design_file.parent.parent / "local" / design_file.name
    on_disk = json.loads(local_file.read_text(encoding="utf-8"))
    assert [row["name"] for row in on_disk["emotions"]] == ["my_test_scene"]


def test_normalize_rejects_invalid_name():
    with pytest.raises(ValueError, match="invalid name"):
        normalize_face_expr_scenes([{"name": "Bad-Name", "frames": [{"ms": 500, "elements": {}}]}])


def test_normalize_preserves_frame_editor_parts_metadata():
    scene = _custom_default_scene()
    scene["frames"][0]["editor_parts"] = {
        "eye_l": {
            "visible": True,
            "shape": "heart_fill",
            "width": 32,
            "height": 32,
            "size": 100,
            "x": 86,
            "y": 96,
            "color": "#ff3366",
            "source": [{"shape": "circle", "x": 86, "y": 96, "r": 10}],
        }
    }

    normalized = normalize_face_expr_scenes([scene])

    assert normalized[0]["frames"][0]["editor_parts"]["eye_l"]["shape"] == "heart_fill"
    assert normalized[0]["frames"][0]["editor_parts"]["eye_l"]["source"][0]["r"] == 10


def test_normalize_sanitizes_untrusted_primitive_colors_and_numbers():
    from deskbot_server.face_expr_scenes_store import (
        normalize_design_scene,
        normalize_primitive_css_color,
    )

    scene = normalize_design_scene(
        {
            "name": "imported",
            "frames": [
                {
                    "ms": 200,
                    "elements": {
                        "mouth": [
                            {
                                "shape": "circle",
                                "x": 1,
                                "y": 2,
                                "r": 3,
                                "color": '"/><script>alert(1)</script>',
                            },
                            {"shape": "rect", "x": "4", "y": 5, "w": "<svg>", "h": 6, "color": "RED"},
                            {
                                "shape": "line",
                                "x1": 0,
                                "y1": 0,
                                "x2": 9,
                                "y2": 9,
                                "color": "rgb(255, 103, 0)",
                            },
                            {"shape": "circle", "x": 0, "y": 0, "r": 1, "color": "#ffd23fcc"},
                            {"shape": "circle", "x": 0, "y": 0, "r": 1, "c": "red"},
                        ],
                    },
                }
            ],
        }
    )

    rows = scene["frames"][0]["elements"]["mouth"]
    # 非法颜色串被丢弃：渲染端与固件回退该图层缺省色，注入面消失。
    assert "color" not in rows[0]
    assert rows[1]["color"] == "#ff0000"
    assert rows[1]["x"] == 4
    assert "w" not in rows[1]
    assert rows[2]["color"] == "#ff6700"
    assert rows[3]["color"] == "#ffd23f"
    # 配置侧 ``c: "red"`` 折算成 RGB565 整数（wire 语义）。
    assert rows[4]["c"] == 0xF800

    assert normalize_primitive_css_color("javascript:alert(1)") is None
    assert normalize_primitive_css_color("url(https://evil.example)") is None
    assert normalize_primitive_css_color("#0f0") == "#00ff00"


def test_save_persists_sanitized_colors(design_file: Path):
    scene = _custom_default_scene()
    scene["name"] = "imported_evil"
    scene["frames"][0]["elements"]["nose"][0]["color"] = '"><img src=x onerror=alert(1)>'

    save_face_expr_scenes_file([scene])

    local_file = design_file.parent.parent / "local" / design_file.name
    on_disk = local_file.read_text(encoding="utf-8")
    assert "onerror" not in on_disk
    assert "alert(1)" not in on_disk
