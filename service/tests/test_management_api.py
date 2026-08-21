from __future__ import annotations

import pytest


@pytest.fixture()
def management_client(monkeypatch, tmp_path):
    db_path = tmp_path / "management.db"
    data_dir = tmp_path / "data"
    monkeypatch.setenv("DESKBOT_DB_PATH", str(db_path))
    monkeypatch.setenv(
        "DESKBOT_WEB_SECRET_KEY",
        "test-only-local-secret-key-with-at-least-32-characters",
    )
    from deskbot_server import device_data
    from deskbot_server.db import init_database
    from deskbot_server.db.engine import init_engine, reset_engine

    monkeypatch.setattr(device_data, "DATA_DIR", data_dir)
    monkeypatch.setattr(device_data, "LOCAL_DATA_ROOT", data_dir / "local")
    reset_engine()
    init_engine(db_path)
    init_database()
    from deskbot_server.web.app import create_app

    try:
        yield create_app().test_client()
    finally:
        reset_engine()


def test_preferences_are_pc_local_and_need_no_device(management_client):
    saved = management_client.patch(
        "/app/api/preferences",
        json={
            "expected_revision": 0,
            "preferences": {
                "quiet_hours": {"start": "23:00", "end": "07:00"},
            },
        },
    )
    assert saved.status_code == 200
    payload = saved.get_json()
    assert payload["revision"] == payload["applied_revision"] == 1
    assert payload["scope"] == "local"
    assert "owner_user_id" not in payload

    # A hardware-shaped query parameter cannot select a second preference set.
    loaded = management_client.get(
        "/app/api/preferences?device_id=another-hardware"
    )
    assert loaded.status_code == 200
    assert loaded.get_json()["preferences"]["revision"] == 1

    assert management_client.get("/app/api/device-preferences").status_code == 404
    assert management_client.patch(
        "/app/api/device-preferences", json={"preferences": {}}
    ).status_code == 404


def test_legacy_user_memory_debug_api_is_not_registered(management_client):
    assert management_client.get("/api/user_memory").status_code == 404
    assert management_client.post(
        "/api/user_memory", json={"text": "obsolete"}
    ).status_code == 404
    assert management_client.delete(
        "/api/user_memory/obsolete"
    ).status_code == 404


def test_reminder_lifecycle_is_local_and_needs_no_device(management_client):
    created = management_client.post(
        "/app/api/scheduled-tasks",
        json={
            "description": "local reminder",
            "task_kind": "once",
            "run_at": "2099-07-27T15:49:30+08:00",
        },
    )
    assert created.status_code == 201
    task = created.get_json()["task"]
    assert task["next_run_at"] == "2099-07-27 15:49:30"
    assert task["session_id"] is None
    assert "owner_user_id" not in task

    paused = management_client.post(
        f"/app/api/scheduled-tasks/{task['id']}/pause", json={}
    )
    assert paused.status_code == 200
    assert paused.get_json()["task"]["status"] == "paused"
    resumed = management_client.post(
        f"/app/api/scheduled-tasks/{task['id']}/resume", json={}
    )
    assert resumed.status_code == 200
    assert resumed.get_json()["task"]["status"] == "active"


def test_session_center_has_one_pc_local_namespace(management_client):
    created = management_client.post(
        "/app/api/sessions", json={"title": "local conversation"}
    )
    assert created.status_code == 201
    session_id = created.get_json()["session"]["session_id"]

    listed = management_client.get(
        "/app/api/sessions?device_id=ignored-hardware&page=1&per_page=20"
    )
    assert listed.status_code == 200
    assert listed.get_json()["sessions"][0]["session_id"] == session_id

    exported = management_client.get(
        f"/app/api/sessions/{session_id}/export?device_id=other-hardware"
    )
    assert exported.status_code == 200
    assert exported.mimetype == "application/json"

    assert management_client.post(
        "/app/api/sessions/clear-current", json={}
    ).get_json()["cleared"] is True
    assert management_client.delete(
        f"/app/api/sessions/{session_id}"
    ).status_code == 200


def test_voice_clone_jobs_are_one_local_library(management_client):
    from deskbot_server.tts.voice_clone_jobs import (
        create_voice_clone_job,
        get_voice_clone_job,
        list_voice_clone_jobs,
        voice_clone_job_payload,
    )

    job = create_voice_clone_job(
        speaker_id="S_local_voice",
        display_name="My local voice",
    )
    loaded = get_voice_clone_job(job_id=job.id)
    assert loaded is not None
    payload = voice_clone_job_payload(loaded)
    assert payload["speaker_id"] == "S_local_voice"
    assert "owner_user_id" not in payload
    assert "device_id" not in payload
    assert [row.id for row in list_voice_clone_jobs()] == [job.id]
