from __future__ import annotations

import os
import time

import pytest


@pytest.fixture()
def local_tmp(monkeypatch, tmp_path):
    root = tmp_path / "local"
    monkeypatch.setattr(
        "deskbot_server.device_tmp_store.local_data_dir",
        lambda: root,
    )
    return root / "tmp"


def test_write_read_local_tmp_file(local_tmp):
    from deskbot_server.device_tmp_store import (
        read_local_tmp_file,
        write_local_tmp_file,
    )

    write_local_tmp_file("notes/hello.txt", "你好")
    out = read_local_tmp_file("notes/hello.txt")
    assert out["content"] == "你好"
    assert (local_tmp / "notes" / "hello.txt").is_file()


def test_tmp_path_traversal_blocked(local_tmp):
    from deskbot_server.device_tmp_store import resolve_local_tmp_path

    with pytest.raises(ValueError):
        resolve_local_tmp_path("../secret.txt")


def test_tmp_store_enforces_total_quota(local_tmp, monkeypatch):
    from deskbot_server import device_tmp_store as store

    monkeypatch.setattr(store, "_MAX_TOTAL_BYTES", 8)
    store.write_local_tmp_file("a.txt", "1234")
    store.write_local_tmp_file("b.txt", "5678")
    with pytest.raises(ValueError, match="总容量"):
        store.write_local_tmp_file("c.txt", "x")
    store.write_local_tmp_file("a.txt", "1")


def test_tmp_store_prunes_expired_files(local_tmp, monkeypatch):
    from deskbot_server import device_tmp_store as store

    monkeypatch.setattr(store, "_TTL_SECONDS", 10)
    store.write_local_tmp_file("old.txt", "old")
    target = store.resolve_local_tmp_path("old.txt")
    old = time.time() - 20
    os.utime(target, (old, old))

    assert store.list_local_tmp_files() == []
    with pytest.raises(ValueError, match="已过期"):
        store.read_local_tmp_file("old.txt")
