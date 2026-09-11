from __future__ import annotations

import time

from deskbot_server import device_camera_health as health


def setup_function(_fn):
    health.reset_device("dev1")


def test_unknown_without_any_signal():
    snap = health.snapshot("dev1")
    assert snap["status"] == "unknown"
    assert snap["setup_failures"] == 0


def test_recent_frame_means_ok():
    snap = health.snapshot("dev1", last_frame_at=time.time() - 2)
    assert snap["status"] == "ok"
    assert snap["last_frame_age_s"] is not None and snap["last_frame_age_s"] < 5


def test_repeated_setup_failures_report_failing_with_human_message():
    health.observe_device_log("dev1", "[00:12:11.609] [ERROR] [CAMERA] supervisor setup failed attempt=4 error=0xffffffff retry_in=1000ms")
    assert health.snapshot("dev1")["status"] == "degraded"
    health.observe_device_log("dev1", "[00:12:17.427] [ERROR] [CAMERA] supervisor setup failed attempt=5 error=0xffffffff retry_in=2000ms")
    snap = health.snapshot("dev1")
    assert snap["status"] == "failing"
    assert snap["setup_failures"] == 2
    assert "重启" in snap["message"]


def test_low_memory_deferral_is_failing_with_memory_hint():
    health.observe_device_log(
        "dev1",
        "[00:13:01.985] [WARN ] [CAMERA] setup deferred: internal largest_block=31732 < 40960 retry_in=60000ms",
    )
    snap = health.snapshot("dev1")
    assert snap["status"] == "failing"
    assert snap["last_error"] == "low_memory"
    assert snap["largest_block"] == 31732
    assert "内存" in snap["message"]


def test_recovery_clears_failures_and_unrelated_lines_are_ignored():
    health.observe_device_log("dev1", "[CAMERA] supervisor setup failed attempt=1 error=0xffffffff retry_in=1s")
    health.observe_device_log("dev1", "[CAMERA] supervisor setup failed attempt=2 error=0xffffffff retry_in=1s")
    health.observe_device_log("dev1", "[AUDIO_OUT] stream ended tail_ok=1")
    assert health.snapshot("dev1")["status"] == "failing"
    health.observe_device_log("dev1", "[00:11:44.563] [WARN ] [CAMERA] supervisor recovery complete attempt=3")
    snap = health.snapshot("dev1", last_frame_at=time.time())
    assert snap["status"] == "ok"
    assert snap["setup_failures"] == 0


def test_fresh_frame_wins_over_recorded_failures():
    """画面持续到达就是 ok：不能因为几分钟前的两次失败一直显示"需重启"。"""
    health.observe_device_log("dev1", "[CAMERA] supervisor setup failed attempt=1 error=0xffffffff retry_in=1s")
    health.observe_device_log("dev1", "[CAMERA] supervisor setup failed attempt=2 error=0xffffffff retry_in=1s")
    assert health.snapshot("dev1")["status"] == "failing"
    assert health.snapshot("dev1", last_frame_at=time.time() - 3)["status"] == "ok"


def test_observed_frame_and_new_session_clear_failures():
    health.observe_device_log(
        "dev1",
        "[CAMERA] setup deferred: internal largest_block=31732 < 40960 retry_in=60000ms",
    )
    assert health.snapshot("dev1")["status"] == "failing"
    health.observe_frame("dev1")
    snap = health.snapshot("dev1")
    assert snap["setup_failures"] == 0 and snap["status"] == "unknown"
    health.observe_device_log("dev1", "[CAMERA] supervisor setup failed attempt=1 error=0xffffffff retry_in=1s")
    health.observe_device_log("dev1", "[CAMERA] supervisor setup failed attempt=2 error=0xffffffff retry_in=1s")
    health.reset_device("dev1")
    assert health.snapshot("dev1")["setup_failures"] == 0
