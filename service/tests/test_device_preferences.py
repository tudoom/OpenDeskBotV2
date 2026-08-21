from __future__ import annotations

from datetime import datetime

import pytest


@pytest.fixture()
def preference_env(tmp_path, monkeypatch):
    from deskbot_server import device_data

    data_dir = tmp_path / "data"
    monkeypatch.setattr(device_data, "DATA_DIR", data_dir)
    monkeypatch.setattr(device_data, "LOCAL_DATA_ROOT", data_dir / "local")
    return data_dir / "local"


def test_preferences_are_pc_local_and_revisioned(preference_env):
    from deskbot_server.device_preferences import load_preferences, update_preferences

    assert load_preferences()["revision"] == 0
    saved = update_preferences(
        {
            "quiet_hours": {"start": "23:30", "end": "07:15"},
        },
        expected_revision=0,
    )
    assert saved["revision"] == 1
    assert saved["quiet_hours"]["start"] == "23:30"
    with pytest.raises(RuntimeError):
        update_preferences({"quiet_hours": {"enabled": False}}, expected_revision=0)


def test_preferences_api_has_no_hardware_scope(preference_env):
    import inspect

    from deskbot_server.device_preferences import (
        load_preferences,
        update_preferences,
    )

    assert "device_id" not in inspect.signature(load_preferences).parameters
    assert "device_id" not in inspect.signature(update_preferences).parameters
    update_preferences({"quiet_hours": {"enabled": False}}, expected_revision=0)
    assert load_preferences()["quiet_hours"]["enabled"] is False
    assert (preference_env / "interaction_preferences.json").is_file()


def test_tts_voice_preference_is_shared_by_all_connected_robots(
    preference_env, monkeypatch
):
    from deskbot_server.device_preferences import update_preferences
    from deskbot_server.tts.doubao import load_doubao_tts_config

    monkeypatch.setenv("DOUBAO_TTS_SPEAKER", "zh_female_vv_uranus_bigtts")
    monkeypatch.setenv("DOUBAO_TTS_RESOURCE_ID", "seed-tts-2.0")
    saved = update_preferences(
        {
            "tts": {
                "speaker": "S_local_custom_voice",
                "resource_id": "seed-icl-2.0",
            }
        }
    )
    assert saved["tts"]["speaker"] == "S_local_custom_voice"
    assert load_doubao_tts_config().speaker == "S_local_custom_voice"


def test_quiet_hours_cross_midnight(preference_env):
    from deskbot_server.device_preferences import (
        quiet_hours_active,
        update_preferences,
    )

    update_preferences(
        {
            "quiet_hours": {
                "enabled": True,
                "start": "22:00",
                "end": "08:00",
                "timezone": "Asia/Shanghai",
            },
        }
    )
    assert quiet_hours_active(now=datetime(2026, 7, 27, 23, 0))
    assert quiet_hours_active(now=datetime(2026, 7, 27, 7, 59))
    assert not quiet_hours_active(now=datetime(2026, 7, 27, 12, 0))


def _resume_tuple(resume):
    return (resume.year, resume.month, resume.day, resume.hour, resume.minute)


def test_quiet_hours_resume_at_cross_midnight(preference_env):
    from deskbot_server.device_preferences import (
        quiet_hours_resume_at,
        update_preferences,
    )

    update_preferences(
        {
            "quiet_hours": {
                "enabled": True,
                "start": "22:00",
                "end": "08:00",
                "timezone": "Asia/Shanghai",
            },
        }
    )
    # 22:00 后进入窗口：结束时刻是次日 08:00。
    late = quiet_hours_resume_at(now=datetime(2026, 7, 27, 23, 0))
    assert late is not None
    assert _resume_tuple(late) == (2026, 7, 28, 8, 0)
    # 凌晨（午夜后）仍在同一窗口：结束时刻是当日 08:00。
    early = quiet_hours_resume_at(now=datetime(2026, 7, 27, 7, 30))
    assert early is not None
    assert _resume_tuple(early) == (2026, 7, 27, 8, 0)
    # 窗口外与关闭勿扰时都不给结束时刻。
    assert quiet_hours_resume_at(now=datetime(2026, 7, 27, 12, 0)) is None
    update_preferences({"quiet_hours": {"enabled": False}})
    assert quiet_hours_resume_at(now=datetime(2026, 7, 27, 23, 0)) is None


def test_quiet_hours_resume_at_same_day_window(preference_env):
    from deskbot_server.device_preferences import (
        quiet_hours_resume_at,
        update_preferences,
    )

    update_preferences(
        {
            "quiet_hours": {
                "enabled": True,
                "start": "12:30",
                "end": "14:00",
                "timezone": "Asia/Shanghai",
            },
        }
    )
    inside = quiet_hours_resume_at(now=datetime(2026, 7, 27, 13, 0))
    assert inside is not None
    assert _resume_tuple(inside) == (2026, 7, 27, 14, 0)
    # 恰好到达 end 分钟即出窗口。
    assert quiet_hours_resume_at(now=datetime(2026, 7, 27, 14, 0)) is None
    assert quiet_hours_resume_at(now=datetime(2026, 7, 27, 12, 0)) is None


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"quiet_hours": {"start": "night"}}, "HH:MM"),
        ({"offline_reminder_policy": "silently_drop"}, "offline"),
        ({"tts": {"speaker": "bad speaker?token=secret"}}, "speaker"),
    ],
)
def test_invalid_preference_values_are_rejected(preference_env, patch, message):
    from deskbot_server.device_preferences import update_preferences

    with pytest.raises(ValueError):
        update_preferences(patch)
