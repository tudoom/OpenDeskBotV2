from __future__ import annotations

import asyncio
import base64
import copy
import io
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from PIL import Image


def _test_jpeg(color: tuple[int, int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (16, 16), color=color).save(output, format="JPEG")
    return output.getvalue()


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        monkeypatch.setenv("DESKBOT_DB_PATH", str(db_path))
        from deskbot_server.db import init_database
        from deskbot_server.db.engine import init_engine, reset_engine

        reset_engine()
        init_engine(db_path)
        init_database()
        try:
            yield db_path
        finally:
            reset_engine()


def test_complete_llm_with_tool_loop_two_rounds(temp_db):
    from deskbot_server.application.llm_tool_loop import complete_llm_with_tool_loop

    round1 = json.dumps(
        {"tts": "", "tools": [{"tool": "memory_add", "text": "喜欢猫"}], "moves": [], "anims": []},
        ensure_ascii=False,
    )
    round2 = json.dumps(
        {"tts": "已记住你喜欢猫", "tools": [], "moves": [], "anims": []},
        ensure_ascii=False,
    )

    chat = AsyncMock()
    chat.llm = AsyncMock(side_effect=[round1, round2])

    async def _run():
        return await complete_llm_with_tool_loop(
            chat,
            "记住我喜欢猫",
            device_id="deskbot_a",
            request_id="req1",
        )

    parsed, tools, results, raw = asyncio.run(_run())
    assert parsed["reply"] == "已记住你喜欢猫"
    assert len(tools) == 1
    assert tools[0]["tool"] == "memory_add"
    assert len(results) == 1
    assert results[0]["ok"] is True
    assert chat.llm.call_count == 2
    assert raw == round2


def test_capture_camera_reaches_llm_as_one_transient_image(
    temp_db,
    caplog,
):
    from sqlalchemy import select

    from deskbot_server.application.llm_tool_loop import (
        complete_llm_with_tool_loop,
    )
    from deskbot_server.db.engine import get_session
    from deskbot_server.db.models import ToolOperation
    from deskbot_server.device_camera_frame_store import update_device_camera_frame
    from deskbot_server.llm.vision_input import TRANSIENT_VISION_IMAGE_KEY, validate_jpeg

    jpeg = _test_jpeg((40, 120, 200))
    encoded = base64.b64encode(jpeg).decode("ascii")
    update_device_camera_frame(
        "deskbot_vision",
        validate_jpeg(jpeg),
        width=320,
        height=240,
    )
    replies = [
        json.dumps(
            {
                "tts": "",
                # Even if the model asks to display, a visual Q&A request did
                # not authorize covering the robot screen with the photo.
                "tools": [{"tool": "capture_camera", "display": True}],
                "moves": [],
                "anims": [],
            }
        ),
        json.dumps(
            {
                "tts": "画面里有一个杯子",
                "tools": [],
                "moves": [],
                "anims": [],
            },
            ensure_ascii=False,
        ),
    ]

    class _FakeChat:
        def __init__(self):
            self.calls: list[object] = []

        async def llm(self, _text, **kwargs):
            self.calls.append(copy.deepcopy(kwargs.get("extra_messages")))
            return replies.pop(0)

    chat = _FakeChat()
    parsed, _tools, results, _raw = asyncio.run(
        complete_llm_with_tool_loop(
            chat,  # type: ignore[arg-type]
            "现在画面里有什么",
            device_id="deskbot_vision",
            request_id="vision-request",
        )
    )

    assert parsed["reply"] == "画面里有一个杯子"
    assert len(chat.calls) == 2
    second_extra = chat.calls[1]
    assert isinstance(second_extra, list)
    content = second_extra[-1]["content"]
    assert [part["type"] for part in content] == ["text", "image_url"]
    assert encoded not in content[0]["text"]
    assert '"image_attached": true' in content[0]["text"]
    assert content[1]["image_url"]["url"] == (
        f"data:image/jpeg;base64,{encoded}"
    )

    assert results[0]["image_transient"] is True
    assert results[0]["image_retained"] is False
    assert results[0]["display_requested"] is False
    assert TRANSIENT_VISION_IMAGE_KEY not in results[0]
    assert "image_display" not in results[0]
    assert "jpeg_base64" not in results[0]
    assert "_transient_display_images" not in parsed
    json.dumps(results, ensure_ascii=False)
    assert encoded not in caplog.text

    session = get_session()
    try:
        ledgers = list(session.scalars(select(ToolOperation)))
    finally:
        session.close()
    assert ledgers
    assert all(encoded not in str(row.result_json or "") for row in ledgers)
    assert all("data:image/" not in str(row.result_json or "") for row in ledgers)


def test_capture_camera_display_true_is_playback_only():
    from deskbot_server.application.llm_tool_loop import (
        complete_llm_with_tool_loop,
    )
    from deskbot_server.device_camera_frame_store import update_device_camera_frame
    from deskbot_server.llm.vision_input import TRANSIENT_VISION_IMAGE_KEY, validate_jpeg

    jpeg = _test_jpeg((80, 30, 160))
    update_device_camera_frame("deskbot_photo", validate_jpeg(jpeg))
    replies = [
        json.dumps(
            {
                "tts": "",
                "tools": [{"tool": "capture_camera", "display": True}],
                "moves": [],
                "anims": [],
            }
        ),
        json.dumps(
            {
                "tts": "拍好了",
                "tools": [],
                "moves": [],
                "anims": [],
            },
            ensure_ascii=False,
        ),
    ]
    chat = AsyncMock()
    chat.llm = AsyncMock(side_effect=replies)

    parsed, _tools, results, _raw = asyncio.run(
        complete_llm_with_tool_loop(
            chat,
            "拍张照片显示给我",
            device_id="deskbot_photo",
        )
    )

    assert parsed["reply"] == "拍好了"
    assert parsed["_transient_display_images"][0]["bytes"]
    assert "image_display" not in results[0]
    assert TRANSIENT_VISION_IMAGE_KEY not in results[0]
    assert results[0]["display_requested"] is True


def test_vision_provider_rejection_returns_actionable_answer_without_guessing():
    from deskbot_server.application.llm_tool_loop import (
        complete_llm_with_tool_loop,
    )
    from deskbot_server.device_camera_frame_store import update_device_camera_frame
    from deskbot_server.llm.vision_input import LlmVisionUnsupportedError, validate_jpeg

    jpeg = _test_jpeg((20, 180, 90))
    update_device_camera_frame("deskbot_text_model", validate_jpeg(jpeg))
    first = json.dumps(
        {
            "tts": "",
            "tools": [{"tool": "capture_camera", "display": False}],
            "moves": [],
            "anims": [],
        }
    )

    class _TextOnlyChat:
        def __init__(self):
            self.calls = 0
            self.last_extra = None

        async def llm(self, _text, **kwargs):
            self.calls += 1
            self.last_extra = kwargs.get("extra_messages")
            if self.calls == 1:
                return first
            raise LlmVisionUnsupportedError(
                "当前模型不支持图像，请在 PC 高级设置中切换视觉模型。"
            )

    chat = _TextOnlyChat()
    parsed, _tools, results, _raw = asyncio.run(
        complete_llm_with_tool_loop(
            chat,  # type: ignore[arg-type]
            "画面里是什么",
            device_id="deskbot_text_model",
        )
    )

    assert chat.calls == 2
    assert "切换视觉模型" in parsed["reply"]
    assert "杯子" not in parsed["reply"]
    assert results[0]["image_transient"] is True
    assert isinstance(chat.last_extra[-1]["content"], str)
    assert "data:image/" not in chat.last_extra[-1]["content"]


def test_tool_round_discards_model_success_claim_and_prefetched_audio(temp_db):
    from deskbot_server.application.llm_tool_loop import complete_llm_with_tool_loop

    round1 = json.dumps(
        {
            "tts": "已经替你记好了",
            "tools": [{"tool": "memory_add", "text": "喜欢茶"}],
            "moves": [],
            "anims": [],
        },
        ensure_ascii=False,
    )
    round2 = json.dumps(
        {"tts": "已经记下你喜欢茶", "tools": [], "moves": [], "anims": []},
        ensure_ascii=False,
    )
    chat = AsyncMock()
    chat.llm = AsyncMock(side_effect=[round1, round2])
    played: list[str] = []

    class _FakePrefetch:
        def __init__(self):
            self.cancel_count = 0

        def cancel(self):
            self.cancel_count += 1

    prefetch = _FakePrefetch()

    async def _play(text: str, _round_idx: int):
        assert prefetch.cancel_count >= 1
        played.append(text)

    async def _run():
        return await complete_llm_with_tool_loop(
            chat,
            "记住我喜欢茶",
            device_id="deskbot_interim",
            request_id="req-interim",
            tts_prefetch=prefetch,  # type: ignore[arg-type]
            on_interim_tts_play=_play,
        )

    parsed, _tools, results, _raw = asyncio.run(_run())
    assert results[0]["ok"] is True
    assert played == ["稍等，我来记录一下。"]
    assert "已经" not in played[0]
    assert parsed["reply"] == "已经记下你喜欢茶"


def test_complete_llm_with_tool_loop_single_round_without_hardware(temp_db):
    from sqlalchemy import select

    from deskbot_server.application.llm_tool_loop import complete_llm_with_tool_loop
    from deskbot_server.db.engine import get_session
    from deskbot_server.db.models import ToolOperation

    answer = json.dumps({"tts": "你好", "tools": [], "moves": [], "anims": []})

    class _FakeChat:
        async def llm(
            self,
            text,
            *,
            device_context=None,
            device_id=None,
            history_messages=None,
            extra_messages=None,
            on_tts_ready=None,
        ):
            return answer

    async def _run():
        return await complete_llm_with_tool_loop(
            _FakeChat(),
            "你好",
            device_id=None,
            request_id="pc-local-final",
        )

    parsed, tools, results, _raw = asyncio.run(_run())
    assert parsed["reply"] == "你好"
    assert tools == []
    assert results == []
    session = get_session()
    try:
        ledgers = list(session.scalars(select(ToolOperation)))
    finally:
        session.close()
    assert len(ledgers) == 1
    assert ledgers[0].operation_id == "turn:pc-local-final:round:0"
    assert ledgers[0].status == "completed"


def test_explicit_confirmation_rejects_negated_text():
    from deskbot_server.application.llm_tool_loop import (
        _user_explicitly_confirms,
        _user_requested_camera_display,
    )

    assert _user_explicitly_confirms("我确认，继续执行")
    assert not _user_explicitly_confirms("不要执行，我不确认")
    assert not _user_explicitly_confirms("帮我删除它")
    assert _user_requested_camera_display("帮我拍张照片显示出来")
    assert not _user_requested_camera_display("看看画面里有什么")
    assert not _user_requested_camera_display("不要拍照，只告诉我画面内容")


def test_operation_ids_are_stable_per_client_request():
    from deskbot_server.application.llm_tool_loop import _with_operation_ids

    tool = {"tool": "memory_add", "text": "喜欢猫"}
    first = _with_operation_ids([tool], "client-request-a")
    replay = _with_operation_ids([tool], "client-request-a")
    changed_payload = _with_operation_ids(
        [{"tool": "memory_add", "text": "喜欢狗"}],
        "client-request-a",
    )
    model_supplied = _with_operation_ids(
        [
            {
                "tool": "memory_add",
                "text": "喜欢猫",
                "operation_id": "untrusted-model-id",
            }
        ],
        "client-request-a",
    )
    another_turn = _with_operation_ids([tool], "client-request-b")

    assert first[0]["operation_id"] == replay[0]["operation_id"]
    assert first[0]["operation_id"] == changed_payload[0]["operation_id"]
    assert first[0]["operation_id"] == model_supplied[0]["operation_id"]
    assert model_supplied[0]["operation_id"] != "untrusted-model-id"
    assert first[0]["operation_id"] != another_turn[0]["operation_id"]
    assert "operation_id" not in tool


def test_client_request_retry_cannot_change_tool_payload(
    temp_db,
    monkeypatch,
):
    import deskbot_server.application.llm_tool_runner as runner
    from deskbot_server.application.llm_tool_loop import (
        complete_llm_with_tool_loop,
    )

    provider_calls: list[str] = []

    def fake_add_memory(text):
        provider_calls.append(text)
        return {"id": f"memory-{len(provider_calls)}", "text": text}

    monkeypatch.setattr(runner, "add_memory", fake_add_memory)

    def tool_reply(text: str) -> str:
        return json.dumps(
            {
                "tts": "",
                "tools": [
                    {
                        "tool": "memory_add",
                        "text": text,
                        "operation_id": f"model-{text}",
                    }
                ],
                "moves": [],
                "anims": [],
            },
            ensure_ascii=False,
        )

    final_reply = json.dumps(
        {"tts": "完成", "tools": [], "moves": [], "anims": []},
        ensure_ascii=False,
    )

    async def run_once(first_reply: str):
        chat = AsyncMock()
        chat.llm = AsyncMock(side_effect=[first_reply, final_reply])
        result = await complete_llm_with_tool_loop(
            chat,
            "记住我的偏好",
            device_id="deskbot-request-ledger",
            request_id="same-client-request",
        )
        return result, chat.llm.call_count

    first, first_calls = asyncio.run(run_once(tool_reply("喜欢猫")))
    exact_replay, replay_calls = asyncio.run(run_once(tool_reply("喜欢猫")))
    changed, changed_calls = asyncio.run(run_once(tool_reply("喜欢狗")))

    assert first_calls == 2
    assert replay_calls == 2
    assert exact_replay[2][0]["idempotent_replay"] is True
    assert provider_calls == ["喜欢猫"]

    parsed, _tools, results, _raw = changed
    assert changed_calls == 1
    assert "同一请求编号" in parsed["reply"]
    assert results[0]["operation_status"] == "idempotency_conflict"
    assert results[0]["retry_safe"] is False


def test_confirmation_is_bound_to_exact_payload_and_replays_once(temp_db, monkeypatch):
    import deskbot_server.application.llm_tool_runner as runner

    provider_calls: list[dict] = []

    def fake_execute_miot_tool(raw):
        provider_calls.append(dict(raw))
        return {"tool": "miot", "ok": True, "result": "scene-started"}

    monkeypatch.setattr(runner, "execute_miot_tool", fake_execute_miot_tool)
    command = {
        "tool": "miot",
        "action": "run_scene",
        "scene_id": "scene-a",
    }
    pending = runner.execute_llm_tools(
        [command],
        device_id="deskbot-confirm-a",
    )
    confirmation_id = pending[0]["confirmation_id"]

    mismatched_payload = runner.execute_llm_tools(
        [
            {
                **command,
                "scene_id": "scene-b",
                "confirmation_id": confirmation_id,
            }
        ],
        device_id="deskbot-confirm-b",
        user_confirmed=True,
    )

    assert mismatched_payload[0]["confirmation_required"] is True
    assert provider_calls == []

    confirmed = runner.execute_llm_tools(
        [{**command, "confirmation_id": confirmation_id}],
        device_id="deskbot-confirm-b",
        user_confirmed=True,
    )
    assert confirmed[0]["ok"] is True
    assert provider_calls[0]["scene_id"] == "scene-a"

    replay = runner.execute_llm_tools(
        [{**command, "confirmation_id": confirmation_id}],
        device_id="deskbot-confirm-a",
        user_confirmed=True,
    )
    assert replay[0]["idempotent_replay"] is True
    assert len(provider_calls) == 1


def test_operation_id_replay_is_pc_local_and_requires_same_payload(temp_db, monkeypatch):
    import deskbot_server.application.llm_tool_runner as runner

    provider_calls: list[str] = []

    def fake_add_memory(text):
        provider_calls.append(text)
        return {"id": "memory-fixed", "text": text}

    monkeypatch.setattr(runner, "add_memory", fake_add_memory)
    command = {
        "tool": "memory_add",
        "text": "喜欢猫",
        "operation_id": "client-operation-1",
    }
    first = runner.execute_llm_tools([command], device_id="deskbot-operation-a")
    replay = runner.execute_llm_tools([command], device_id="deskbot-operation-b")
    changed_payload = runner.execute_llm_tools(
        [{**command, "text": "喜欢狗"}],
        device_id="deskbot-operation-b",
    )

    assert first[0]["ok"] is True
    assert replay[0]["ok"] is True
    assert replay[0]["idempotent_replay"] is True
    assert changed_payload[0]["operation_status"] == "idempotency_conflict"
    assert changed_payload[0]["retry_safe"] is False
    assert provider_calls == ["喜欢猫"]


def test_provider_success_then_ledger_failure_is_never_replayed(
    temp_db,
    monkeypatch,
):
    import deskbot_server.application.llm_tool_runner as runner
    from deskbot_server.db.engine import get_session
    from deskbot_server.db.models import ToolConfirmation, ToolOperation

    provider_calls: list[dict] = []

    def fake_execute_miot_tool(raw):
        provider_calls.append(dict(raw))
        return {"tool": "miot", "ok": True, "result": "provider-accepted"}

    def fail_finish(*_args, **_kwargs):
        raise RuntimeError("simulated ledger commit failure")

    monkeypatch.setattr(runner, "execute_miot_tool", fake_execute_miot_tool)
    monkeypatch.setattr(runner, "_finish_tool_operation", fail_finish)
    command = {
        "tool": "miot",
        "action": "run_scene",
        "scene_id": "scene-crash-window",
    }
    routing = {"device_id": "deskbot-crash-window"}
    pending = runner.execute_llm_tools([command], **routing)
    confirmed_command = {
        **command,
        "confirmation_id": pending[0]["confirmation_id"],
    }

    first = runner.execute_llm_tools(
        [confirmed_command],
        user_confirmed=True,
        **routing,
    )

    # Expired confirmations are garbage-collected.  A retransmission with the
    # original opaque id must still consult the durable operation ledger and
    # must never turn into a fresh provider call.
    session = get_session()
    try:
        session.query(ToolConfirmation).delete()
        session.commit()
    finally:
        session.close()
    replay = runner.execute_llm_tools(
        [confirmed_command],
        user_confirmed=True,
        **routing,
    )

    assert first[0]["operation_status"] == "unknown"
    assert first[0]["reconciliation_required"] is True
    assert first[0]["retry_safe"] is False
    assert first[0]["side_effect_may_have_succeeded"] is True
    assert replay[0]["operation_status"] == "unknown"
    assert replay[0]["idempotent_replay"] is True
    assert len(provider_calls) == 1

    session = get_session()
    try:
        operation = session.query(ToolOperation).one()
        assert operation.status == "unknown"
    finally:
        session.close()
