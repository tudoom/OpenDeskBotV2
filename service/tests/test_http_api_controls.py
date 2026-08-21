from __future__ import annotations

import asyncio
import json

import pytest

from deskbot_server.ws import http_api


@pytest.fixture()
def http_api_env(tmp_path, monkeypatch):
    db_path = tmp_path / "http-api.db"
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DESKBOT_DB_PATH", str(db_path))
    monkeypatch.setenv("DESKBOT_WEB_SECRET_KEY", "h" * 32)
    from deskbot_server import device_data
    from deskbot_server.db import init_database
    from deskbot_server.db.engine import init_engine, reset_engine

    monkeypatch.setattr(device_data, "DATA_DIR", data_dir)
    monkeypatch.setattr(device_data, "LOCAL_DATA_ROOT", data_dir / "local")
    global_dir = data_dir / "global"
    global_dir.mkdir(parents=True)
    (global_dir / "deskbot-face.json").write_text(
        '{"name":"test","phonemes":[],"emotions":[]}', encoding="utf-8"
    )
    reset_engine()
    init_engine(db_path)
    init_database()
    try:
        yield
    finally:
        reset_engine()


class _Connection:
    remote_address = ("127.0.0.1", 43123)


class _RemoteConnection:
    remote_address = ("198.51.100.23", 43123)


class _Ipv6ZoneLoopbackConnection:
    remote_address = ("::1%12", 43123, 0, 12)


class _Request:
    def __init__(
        self,
        path: str,
        *,
        api_key: str | None = None,
        method: str = "GET",
        body: bytes = b"",
    ) -> None:
        self.path = path
        self.method = method
        self.body = body
        self.headers: dict[str, str] = {}
        if api_key:
            self.headers["X-API-Key"] = api_key


class _Registry:
    @staticmethod
    def snapshot():
        return []


class _Broker:
    max_events = 100

    @staticmethod
    def snapshot_events(_device_id, _limit):
        return []


class _Hub:
    async def first_ws(self, _device_id):
        return None


class _Chat:
    settings = None


def _handler():
    return http_api._build_http_request_handler(
        _Broker(),
        _Registry(),
        asr_chat_hub=_Hub(),
        chat=_Chat(),
    )


def _call(handler, request: _Request, connection=None):
    return asyncio.run(handler(connection or _Connection(), request))


def _json(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


def _local_key() -> str:
    from deskbot_server.auth.api_key_service import read_free_api_key_raw

    raw = read_free_api_key_raw()
    assert raw
    return raw


def test_api_requires_provider_key_but_no_account(http_api_env):
    handler = _handler()
    denied = _call(handler, _Request("/api/devices"))
    assert denied.status_code == 401
    assert _json(denied)["error"] == "api_key_required"

    accepted = _call(handler, _Request("/api/devices", api_key=_local_key()))
    assert accepted.status_code == 200
    assert _json(accepted)["devices"] == []


def test_http_api_is_loopback_only_even_with_valid_key(http_api_env):
    response = _call(
        _handler(),
        _Request("/api/devices", api_key=_local_key()),
        connection=_RemoteConnection(),
    )
    assert response.status_code == 403
    assert _json(response)["error"] == "loopback_required"


def test_http_api_accepts_ipv6_zone_loopback(http_api_env):
    response = _call(
        _handler(),
        _Request("/api/devices", api_key=_local_key()),
        connection=_Ipv6ZoneLoopbackConnection(),
    )
    assert response.status_code == 200


def test_health_keeps_its_existing_remote_access(http_api_env):
    response = _call(
        _handler(),
        _Request("/health"),
        connection=_RemoteConnection(),
    )
    assert response.status_code == 200


def test_expression_runtime_status_exposes_current_display_owner(
    http_api_env, monkeypatch
):
    from deskbot_server.application import expression_runtime

    class _Runtime:
        @staticmethod
        def snapshot():
            return {
                "device_id": "deskbot_test",
                "displayed_expression": "adhoc_web",
                "active_source": "web",
                "desired_state": "idle",
                "lease": {"source": "web", "reason": "http_device_pb_anim"},
                "active_request": None,
                "pending": [],
            }

    monkeypatch.setattr(
        expression_runtime,
        "get_expression_runtime",
        lambda device_id: _Runtime() if device_id == "deskbot_test" else None,
    )
    response = _call(
        _handler(),
        _Request(
            "/api/expression_runtime?device_id=deskbot_test",
            api_key=_local_key(),
        ),
    )

    assert response.status_code == 200
    payload = _json(response)
    assert payload["runtime"]["displayed_expression"] == "adhoc_web"
    assert payload["runtime"]["active_source"] == "web"


def test_expression_runtime_status_is_fail_closed_when_usb_runtime_missing(
    http_api_env, monkeypatch
):
    from deskbot_server.application import expression_runtime

    monkeypatch.setattr(
        expression_runtime,
        "get_expression_runtime",
        lambda _device_id: None,
    )
    response = _call(
        _handler(),
        _Request(
            "/api/expression_runtime?device_id=deskbot_test",
            api_key=_local_key(),
        ),
    )

    assert response.status_code == 503
    assert _json(response)["error"] == "expression_runtime_unavailable"


@pytest.mark.parametrize("path", ["/api/servo_config", "/api/scene_playbooks"])
def test_pc_local_config_api_does_not_require_device_id(http_api_env, path):
    response = _call(_handler(), _Request(path, api_key=_local_key()))
    assert response.status_code == 200
    assert _json(response)["scope"] == "local"


def test_servo_contract_is_readonly_single_source(http_api_env):
    from deskbot_server import servo_protocol
    from deskbot_server.servo_config_store import VIEWER_LR_SWAP, save_servo_cfg_file

    save_servo_cfg_file(
        {
            "xMin": 10,
            "xMax": 170,
            "yMin": 70,
            "yMax": 110,
            "xReverse": 0,
            "yReverse": 0,
            "perspective": "viewer",
            "presets": [
                {
                    "id": "nod_head",
                    "label": "nod",
                    "steps": [{"x": 0, "y": 10, "xm": 1, "ym": 1, "ms": 300}],
                }
            ],
        }
    )
    handler = _handler()
    key = _local_key()

    denied = _call(handler, _Request("/api/servo_contract"))
    assert denied.status_code == 401

    mutated = _call(
        handler, _Request("/api/servo_contract", api_key=key, method="POST")
    )
    assert mutated.status_code == 405

    response = _call(handler, _Request("/api/servo_contract", api_key=key))
    assert response.status_code == 200
    body = _json(response)
    assert body["ok"] is True
    assert body["scope"] == "local"
    assert "device_id" not in body
    assert body["envelope"] == dict(servo_protocol.SERVO_HARDWARE_ENVELOPE)
    assert body["limits"] == {
        "maxSegmentsPerPb": servo_protocol.SERVO_MAX_SEGMENTS_PER_PB,
        "maxBatchDurationMs": servo_protocol.SERVO_MAX_BATCH_DURATION_MS,
        "minSegmentDurationMs": servo_protocol.SERVO_MIN_SEGMENT_DURATION_MS,
        "maxPlanSteps": servo_protocol.SERVO_MAX_PLAN_STEPS,
        "maxPlanDurationMs": servo_protocol.SERVO_MAX_PLAN_DURATION_MS,
        "maxDegreesPerTick": servo_protocol.SERVO_MAX_DEGREES_PER_TICK,
        "tickMs": servo_protocol.SERVO_TICK_MS,
    }
    assert body["viewerLrSwap"] == VIEWER_LR_SWAP
    assert [p["id"] for p in body["presets"]] == ["nod_head"]


def test_global_setting_has_no_role_matrix_and_get_cannot_mutate(
    http_api_env, monkeypatch
):
    state = {"enabled": True}
    monkeypatch.setattr(
        http_api, "persist_asr_auto_reply", lambda enabled: state.update(enabled=enabled)
    )
    monkeypatch.setattr(
        http_api, "get_asr_voice_auto_reply_enabled", lambda: state["enabled"]
    )
    key = _local_key()

    invalid_get = _call(
        _handler(),
        _Request("/api/asr_auto_reply?enabled=0", api_key=key),
    )
    assert invalid_get.status_code == 405
    assert state["enabled"] is True

    changed = _call(
        _handler(),
        _Request("/api/asr_auto_reply?enabled=0", api_key=key, method="POST"),
    )
    assert changed.status_code == 200
    assert state["enabled"] is False


def test_face_catalog_and_pb_scenes_are_pc_local(http_api_env):
    from deskbot_server.face_expr_scenes_store import save_face_expr_scenes_file

    save_face_expr_scenes_file(
        [
            {
                "name": "my_face",
                "title": "My face",
                "frames": [
                    {
                        "ms": 300,
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
        ]
    )
    key = _local_key()
    handler = _handler()
    catalog = _call(
        handler,
        _Request("/api/face_catalog", api_key=key),
    )
    scenes = _call(
        handler,
        _Request("/api/pb_scenes", api_key=key),
    )
    assert catalog.status_code == scenes.status_code == 200
    assert "device_id" not in _json(catalog)
    assert "device_id" not in _json(scenes)
    assert any(row["name"] == "my_face" for row in _json(catalog)["emotions"])
    assert "my_face" in _json(scenes)["scenes"]


def test_device_servo_uses_shared_limit_reverse_and_duration_rules(http_api_env):
    from deskbot_server.servo_config_store import save_servo_cfg_file

    save_servo_cfg_file(
        {
            "xMin": 10,
            "xMax": 170,
            "yMin": 80,
            "yMax": 100,
            "xReverse": 1,
            "yReverse": 1,
            "presets": [],
        }
    )
    response = _call(
        _handler(),
        _Request(
            "/api/device_servo?device_id=deskbot_a&dyaw=30&dpitch=200"
            "&xm=0&ym=0&ms=99999&operation_id=servo-clamp-absolute",
            api_key=_local_key(),
            method="POST",
        ),
    )
    payload = _json(response)

    assert response.status_code == 200
    assert payload["servo"] == [
        {
            "xm": 0,
            "ym": 0,
            "x": 150,
            "y": 80,
            "ms": 99999,
            "x_min": 10,
            "x_max": 170,
            "y_min": 80,
            "y_max": 100,
        }
    ]

    relative = _call(
        _handler(),
        _Request(
            "/api/device_servo?device_id=deskbot_a&dyaw=45&dpitch=-20"
            "&xm=1&ym=1&ms=50&operation_id=servo-clamp-relative",
            api_key=_local_key(),
            method="POST",
        ),
    )
    relative_payload = _json(relative)
    assert relative.status_code == 200
    assert relative_payload["servo"] == [
        {
            "xm": 1,
            "ym": 1,
            "x": -45,
            "y": 20,
            "ms": 50,
            "x_min": 10,
            "x_max": 170,
            "y_min": 80,
            "y_max": 100,
        }
    ]


def test_device_servo_rejects_non_finite_coordinates(http_api_env):
    response = _call(
        _handler(),
        _Request(
            "/api/device_servo?device_id=deskbot_a&dyaw=nan&dpitch=90",
            api_key=_local_key(),
            method="POST",
        ),
    )

    assert response.status_code == 400
    assert _json(response)["error"] == "invalid servo coordinates or duration"


def test_device_servo_accepts_atomic_json_step_sequence(http_api_env):
    from deskbot_server.servo_config_store import save_servo_cfg_file

    save_servo_cfg_file(
        {
            "xMin": 10,
            "xMax": 170,
            "yMin": 80,
            "yMax": 100,
            "xReverse": 1,
            "yReverse": 1,
            "presets": [],
        }
    )
    body = {
        "device_id": "deskbot_a",
        "steps": [
            {"x": 30, "y": 90, "xm": 0, "ym": 0, "ms": 250},
            {"x": 12, "y": -8, "xm": 1, "ym": 1, "ms": 350},
        ],
        "action": "replace",
        "level": 3,
        "operation_id": "servo-json-steps",
    }
    response = _call(
        _handler(),
        _Request(
            "/api/device_servo",
            api_key=_local_key(),
            method="POST",
            body=json.dumps(body).encode("utf-8"),
        ),
    )
    payload = _json(response)

    assert response.status_code == 200
    assert payload["operation_id"] == "servo-json-steps"
    assert payload["servo"] == [
        {
            "xm": 0,
            "ym": 0,
            "x": 150,
            "y": 90,
            "ms": 250,
            "x_min": 10,
            "x_max": 170,
            "y_min": 80,
            "y_max": 100,
        },
        {
            "xm": 1,
            "ym": 1,
            "x": -12,
            "y": 8,
            "ms": 350,
            "x_min": 10,
            "x_max": 170,
            "y_min": 80,
            "y_max": 100,
        },
    ]
    assert payload["action"] == "replace"
    assert payload["level"] == 3


def test_device_servo_accepts_hold_mode_and_forces_held_value_to_zero(http_api_env):
    body = {
        "device_id": "deskbot_a",
        "steps": [{"x": 123, "y": 90, "xm": 2, "ym": 0, "ms": 250}],
        "action": "replace",
        "level": 3,
        "operation_id": "servo-json-hold",
    }
    response = _call(
        _handler(),
        _Request(
            "/api/device_servo",
            api_key=_local_key(),
            method="POST",
            body=json.dumps(body).encode("utf-8"),
        ),
    )
    payload = _json(response)

    assert response.status_code == 200
    assert payload["servo"] == [
        {
            "xm": 2,
            "ym": 0,
            "x": 0,
            "y": 90,
            "ms": 250,
            "x_min": 10,
            "x_max": 170,
            "y_min": 70,
            "y_max": 110,
        }
    ]


def test_device_servo_wire_is_volatile_and_uses_production_axes(http_api_env):
    async def _run():
        from deskbot_server.application.control_operations import get_control_operation
        from deskbot_server.ws.pb_ack_waiter import pb_ack_gate

        device_id = "deskbot_a"
        sent: list[dict] = []
        delivered = asyncio.Event()

        class _ConnectedWebSocket:
            async def send(self, wire):
                message = json.loads(wire)
                sent.append(message)
                if message.get("type") == "pb_single":
                    await pb_ack_gate.notify(
                        device_id,
                        {
                            "req": message["req"],
                            "idx": message["idx"],
                            "phase": "played",
                        },
                    )
                    delivered.set()

        websocket = _ConnectedWebSocket()

        class _ConnectedHub:
            async def first_ws(self, requested_device_id):
                assert requested_device_id == device_id
                return websocket

        handler = http_api._build_http_request_handler(
            _Broker(),
            _Registry(),
            asr_chat_hub=_ConnectedHub(),
            chat=_Chat(),
        )
        operation_id = "servo-volatile-wire"
        body = {
            "device_id": device_id,
            "steps": [{"x": 96, "y": 90, "xm": 0, "ym": 0, "ms": 600}],
            "action": "replace",
            "level": 3,
            "operation_id": operation_id,
        }
        response = await handler(
            _Connection(),
            _Request(
                "/api/device_servo",
                api_key=_local_key(),
                method="POST",
                body=json.dumps(body).encode("utf-8"),
            ),
        )
        assert response.status_code == 202
        await asyncio.wait_for(delivered.wait(), timeout=1.0)
        for _ in range(100):
            operation = get_control_operation(operation_id=operation_id)
            if operation is not None and operation.status == "completed":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("servo control operation did not complete")

        assert len(sent) == 1
        assert sent[0]["servo"] == [
            {
                "xm": 0,
                "ym": 0,
                "x": 96,
                "y": 90,
                "ms": 600,
                "x_min": 10,
                "x_max": 170,
                "y_min": 70,
                "y_max": 110,
            }
        ]
        assert "durable" not in sent[0]

    asyncio.run(_run())


def test_device_servo_transport_failure_reaches_terminal_operation(http_api_env):
    async def _run():
        from deskbot_server.application.control_operations import get_control_operation

        device_id = "deskbot_a"

        class _FailingWebSocket:
            async def send(self, _wire):
                raise OSError("USB session closed")

        class _ConnectedHub:
            async def first_ws(self, requested_device_id):
                assert requested_device_id == device_id
                return _FailingWebSocket()

        handler = http_api._build_http_request_handler(
            _Broker(),
            _Registry(),
            asr_chat_hub=_ConnectedHub(),
            chat=_Chat(),
        )
        operation_id = "servo-transport-failure-terminal"
        response = await handler(
            _Connection(),
            _Request(
                "/api/device_servo",
                api_key=_local_key(),
                method="POST",
                body=json.dumps(
                    {
                        "device_id": device_id,
                        "steps": [
                            {"x": 90, "y": 90, "xm": 2, "ym": 2, "ms": 250}
                        ],
                        "operation_id": operation_id,
                    }
                ).encode("utf-8"),
            ),
        )
        assert response.status_code == 202

        for _ in range(100):
            operation = get_control_operation(operation_id=operation_id)
            if operation is not None and operation.status in {
                "completed",
                "failed",
                "cancelled",
                "timeout",
            }:
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("failed servo control operation remained running")

        assert operation is not None
        assert operation.status == "failed"
        assert operation.error_code == "playback_failed"

    asyncio.run(_run())


def test_device_servo_json_preset_expands_and_unknown_is_400(http_api_env):
    from deskbot_server.servo_config_store import save_servo_cfg_file

    save_servo_cfg_file(
        {
            "xMin": 10,
            "xMax": 170,
            "yMin": 70,
            "yMax": 110,
            "xReverse": 1,
            "yReverse": 0,
            "presets": [
                {
                    "id": "wave",
                    "label": "Wave",
                    "steps": [
                        {"x": 20, "y": 90, "xm": 0, "ym": 0, "ms": 200},
                        {"x": 10, "y": 0, "xm": 1, "ym": 1, "ms": 300},
                    ],
                }
            ],
        }
    )
    response = _call(
        _handler(),
        _Request(
            "/api/device_servo",
            api_key=_local_key(),
            method="POST",
            body=json.dumps(
                {
                    "device_id": "deskbot_a",
                    "preset": "wave",
                    "duration_ms": 1000,
                    "operation_id": "servo-json-preset",
                }
            ).encode("utf-8"),
        ),
    )
    payload = _json(response)
    assert response.status_code == 200
    assert payload["preset"] == "wave"
    assert len(payload["servo"]) == 2
    assert sum(step["ms"] for step in payload["servo"]) == 1000
    assert payload["servo"][0]["x"] == 160
    assert payload["servo"][1]["x"] == -10

    unknown = _call(
        _handler(),
        _Request(
            "/api/device_servo",
            api_key=_local_key(),
            method="POST",
            body=json.dumps(
                {"device_id": "deskbot_a", "preset": "missing-preset"}
            ).encode("utf-8"),
        ),
    )
    assert unknown.status_code == 400
    assert "unknown servo preset" in _json(unknown)["error"]
