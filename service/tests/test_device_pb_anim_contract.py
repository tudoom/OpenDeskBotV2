from __future__ import annotations

from pathlib import Path


def test_preview_chain_normalizes_wire_colors_and_splits_long_animation():
    from deskbot_server.face_expr_scenes_store import design_frames_to_pb_chain

    frames = [
        {
            "ms": 5000,
            "elements": {
                "mouth": [
                    {"shape": "draw_line", "x1": 1, "y1": 2, "x2": 3, "y2": 4, "color": "#ff0000"}
                ]
            },
        }
        for _ in range(3)
    ]
    pairs = design_frames_to_pb_chain(frames, runtime_req="preview-test")
    assert [payload["chunk_ms"] for payload, _binary in pairs] == [10000, 5000]
    primitive = pairs[0][0]["anim"][0]["elements"]["mouth"][0]
    assert primitive["shape"] == "line"
    assert primitive["c"] == 0xF800
    assert "color" not in primitive


def test_http_preview_route_reuses_canonical_multi_chunk_builder():
    path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "deskbot_server"
        / "ws"
        / "http_api.py"
    )
    source = path.read_text(encoding="utf-8")
    block = source.split('if path_only == "/api/device_pb_anim":', 1)[1]
    block = block.split('if path_only == "/api/device_pb_expr_scene":', 1)[0]

    assert 'runtime_req="validation"' in block
    assert "design_frames_to_pb_chain(" in block
    assert 'payload["action"] = act' in block
    assert 'payload["level"] = pb_level' in block
    assert '"chunks": len(validation_pairs)' in block
    assert '"anim": anim_list' in block
    assert '"expression_name": expression_name or "adhoc_web"' in block
    assert '"expression_title": expression_title or expression_name or "Web 预览"' in block
    # Arbitration happens in the shared persisted-operation runner so every
    # Web expression endpoint reaches the same USB runtime.
    assert "play_web_expression_frames" in source
    assert "hold_ms=None" in source
    assert "direct PB fallback is disabled" in source
    assert "result.expression_result = arbitrated.as_tool_result()" in source


def test_servo_with_scene_never_appends_anim_to_the_servo_pb_chain():
    path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "deskbot_server"
        / "ws"
        / "http_api.py"
    )
    source = path.read_text(encoding="utf-8")
    block = source.split('if path_only == "/api/device_servo":', 1)[1]
    block = block.split('if path_only == "/api/pb_scenes":', 1)[0]
    builder = block.split("def _build_servo_sequences", 1)[1]

    assert "_prepare_pb_scene_chain_frames" not in builder
    assert '"scene_frames": scene_frames_durable' in block
    assert 'kind == "device_servo"' in source
    assert 'reason="http_device_servo_with_scene"' in source
