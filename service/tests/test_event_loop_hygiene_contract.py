"""事件循环卫生契约：实时路径上的同步 SQLite / 磁盘 I/O 必须走 to_thread。

Core 的唯一 asyncio 事件循环承载 USB 串口帧分发、麦克风发布、下行播放与
PB ACK。共享 SQLite 库 busy_timeout=30s：Web 进程持写锁时，任何在循环上
内联执行的落库调用都会把整个实时面冻住。本测试用窄文本断言钉住已修复的
调用点——这些函数只允许以 ``asyncio.to_thread(func, ...)`` 的引用形式出现，
一旦重新出现 ``func(`` 裸调即失败。
"""
from __future__ import annotations

from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src" / "deskbot_server"


def _read(relative: str) -> str:
    return (_SRC / relative).read_text(encoding="utf-8")


_SCHEDULER_DB_HELPERS = (
    "recover_expired_running_tasks",
    "expire_overdue_active_tasks",
    "claim_due_tasks",
    "finish_scheduled_task",
    "defer_offline_scheduled_task",
    "defer_quiet_hours_scheduled_task",
    "quiet_hours_resume_at",
    "mark_scheduled_task_delivery_attempt",
    "retry_scheduled_task",
    "renew_scheduled_task_lease",
    "wake_deliver_when_online_tasks",
)


def test_scheduler_never_runs_scheduled_task_db_calls_on_the_loop():
    source = _read("application/scheduled_task_scheduler.py")
    assert "asyncio.to_thread(" in source
    for name in _SCHEDULER_DB_HELPERS:
        # Still used at all (guards against the assertion rotting away).
        assert name in source, f"{name} disappeared from the scheduler"
        # A bare ``name(...)`` call would run an UPDATE+commit inline on the
        # event loop.  Passing the function reference to asyncio.to_thread is
        # the only allowed call form in this file.
        assert f"{name}(" not in source, (
            f"bare call of {name} in scheduled_task_scheduler.py; "
            "wrap it in asyncio.to_thread"
        )


_HTTP_CONTROL_LEDGER_HELPERS = (
    "heartbeat_control_operation",
    "mark_control_operation_running",
    "finish_control_operation",
    "accept_control_operation",
    "get_control_operation",
)


def test_http_control_ledger_calls_run_off_loop():
    source = _read("ws/http_api.py")
    # ``_fail_accepted_control_operation`` is the one sanctioned synchronous
    # helper: it batches its ledger writes and is itself dispatched through
    # asyncio.to_thread by every async caller.  Bare ledger calls are only
    # legal inside its body.
    before, rest = source.split("def _fail_accepted_control_operation(", 1)
    helper_body, after = rest.split("def _scene_playbook_operation_payload(", 1)
    loop_side = before + after

    assert "asyncio.to_thread(" in loop_side
    for name in _HTTP_CONTROL_LEDGER_HELPERS:
        assert name in loop_side, f"{name} disappeared from http_api"
        assert f"{name}(" not in loop_side, (
            f"bare call of {name} on the event loop in http_api.py; "
            "wrap it in asyncio.to_thread"
        )

    # The helper's own ledger writes stay synchronous by design (they run in
    # the dispatched worker thread).
    assert "finish_control_operation(" in helper_body

    # The sync helper itself must never be bare-called from the async side.
    assert "_fail_accepted_control_operation(" not in loop_side, (
        "bare call of _fail_accepted_control_operation in an async handler; "
        "dispatch it via asyncio.to_thread"
    )
    assert "_fail_accepted_control_operation," in loop_side


def test_retention_purge_is_out_of_the_accept_path_and_periodic():
    ops_source = _read("application/control_operations.py")
    accept_block = ops_source.split("def accept_control_operation(", 1)[1]
    accept_block = accept_block.split("def recover_stale_control_operations(", 1)[0]
    # The multi-commit purge used to piggyback on the first accept after the
    # cleanup interval, stalling that HTTP control request on the loop.
    assert "maybe_purge_expired_control_operations()" not in accept_block, (
        "accept_control_operation must not trigger retention cleanup inline"
    )

    main_source = _read("main.py")
    assert "_control_operation_retention_loop" in main_source
    assert "maybe_purge_expired_control_operations" in main_source
    assert "control_op_retention.cancel()" in main_source


_CHAT_FLOW_SESSION_WRITES = (
    "ensure_active_session",
    "append_turn",
    "update_assistant_delivery",
)


def test_chat_flow_session_file_writes_run_off_loop():
    source = _read("application/chat_flow.py")
    for name in _CHAT_FLOW_SESSION_WRITES:
        assert name in source, f"{name} disappeared from chat_flow"
        # Session writes fsync and take the cross-process file lock (up to
        # ~10s wait on Windows); they must go through asyncio.to_thread.
        assert f"{name}(" not in source, (
            f"bare call of {name} in chat_flow.py; wrap it in asyncio.to_thread"
        )
    # PB framing includes up to 10s-of-PCM Opus encoding per chunk.
    assert "build_pb_wire_pairs(" not in source, (
        "bare call of build_pb_wire_pairs in chat_flow.py; "
        "wrap it in asyncio.to_thread"
    )
    assert "build_pb_wire_pairs," in source


def test_voice_link_feedback_catalog_load_runs_off_loop():
    source = _read("application/voice_link_feedback.py")
    # 表情目录读取会打开本地 face 文档文件；必须以函数引用形式交给
    # asyncio.to_thread，绝不在事件循环上内联执行。
    assert "asyncio.to_thread(load_expression_catalog)" in source
    stripped = source.replace("asyncio.to_thread(load_expression_catalog)", "")
    assert "load_expression_catalog(" not in stripped, (
        "bare call of load_expression_catalog in voice_link_feedback.py; "
        "wrap it in asyncio.to_thread"
    )
