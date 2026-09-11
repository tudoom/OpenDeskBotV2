from __future__ import annotations

import pytest


@pytest.fixture()
def face_profiles_file(monkeypatch, tmp_path):
    path = tmp_path / "local" / "face_profiles.json"
    monkeypatch.setattr(
        "deskbot_server.face_profiles_store.resolve_json_path",
        lambda _default: str(path),
    )
    return path


def _profile(person_id: int, name: str) -> dict:
    return {
        "person_id": person_id,
        "name": name,
        "descriptor": [0.1] * 512,
        "descriptor_kind": "embedding",
    }


def test_delete_face_profile(face_profiles_file):
    from deskbot_server.face_profiles_store import (
        delete_face_profile,
        list_face_profiles_summary,
        save_face_profiles,
    )

    save_face_profiles([_profile(1, "Alice"), _profile(2, "Bob")])
    assert delete_face_profile(1)
    rows = list_face_profiles_summary()
    assert [row["person_id"] for row in rows] == [2]
    assert "descriptor" not in rows[0]


def test_update_face_profile_is_pc_local(face_profiles_file):
    from deskbot_server.face_profiles_store import (
        list_face_profiles_summary,
        save_face_profiles,
        update_face_profile_name,
    )

    save_face_profiles([_profile(1, "Old")])
    updated = update_face_profile_name(1, "New")
    assert updated is not None
    assert updated["name"] == "New"
    assert list_face_profiles_summary()[0]["name"] == "New"


def test_update_face_profile_api_requires_no_login_or_device(monkeypatch, tmp_path):
    db_path = tmp_path / "test.db"
    data_dir = tmp_path / "data"
    profiles_path = data_dir / "local" / "face_profiles.json"
    monkeypatch.setenv("DESKBOT_DB_PATH", str(db_path))
    from deskbot_server import device_data
    from deskbot_server.db import init_database
    from deskbot_server.db.engine import init_engine, reset_engine

    monkeypatch.setattr(device_data, "DATA_DIR", data_dir)
    monkeypatch.setattr(device_data, "LOCAL_DATA_ROOT", data_dir / "local")
    monkeypatch.setattr(
        "deskbot_server.face_profiles_store.resolve_json_path",
        lambda _default: str(profiles_path),
    )
    reset_engine()
    init_engine(db_path)
    init_database()
    try:
        from deskbot_server.face_profiles_store import save_face_profiles
        from deskbot_server.web.app import create_app

        save_face_profiles([_profile(1, "Old")])
        response = create_app().test_client().put(
            "/app/api/face-profiles/1",
            json={"name": "New"},
        )
        assert response.status_code == 200
        assert response.get_json()["profile"]["name"] == "New"
    finally:
        reset_engine()
