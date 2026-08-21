from __future__ import annotations

import threading
import time

import pytest

import deskbot_server.atomic_store as atomic_store
from deskbot_server.atomic_store import file_lock


def _lock_key(target) -> str:
    lock_path = target.with_name(target.name + ".lock")
    return str(lock_path.resolve())


def _cached_users(target) -> int | None:
    key = _lock_key(target)
    with atomic_store._process_locks_guard:
        entry = atomic_store._process_locks.get(key)
        return entry.users if entry is not None else None


def test_lock_file_does_not_grow_per_acquisition(tmp_path):
    target = tmp_path / "shared.json"

    for _ in range(20):
        with file_lock(target):
            pass

    lock_path = tmp_path / "shared.json.lock"
    assert lock_path.exists()
    if lock_path.stat().st_size:
        assert lock_path.stat().st_size == 1
    assert _cached_users(target) is None


def test_process_lock_keeps_one_entry_for_holder_and_waiters(tmp_path):
    target = tmp_path / "contended.json"
    waiter_count = 8
    holder_entered = threading.Event()
    release_holder = threading.Event()
    waiter_start = threading.Barrier(waiter_count + 1)
    state_guard = threading.Lock()
    active = 0
    max_active = 0
    completed: list[int] = []

    def enter_section(index: int, *, hold: bool = False) -> None:
        nonlocal active, max_active
        with file_lock(target):
            with state_guard:
                active += 1
                max_active = max(max_active, active)
            try:
                if hold:
                    holder_entered.set()
                    assert release_holder.wait(timeout=5)
                else:
                    time.sleep(0.005)
                    completed.append(index)
            finally:
                with state_guard:
                    active -= 1

    holder = threading.Thread(
        target=enter_section,
        args=(-1,),
        kwargs={"hold": True},
    )
    holder.start()
    assert holder_entered.wait(timeout=5)

    def waiter(index: int) -> None:
        waiter_start.wait(timeout=5)
        enter_section(index)

    waiters = [
        threading.Thread(target=waiter, args=(index,))
        for index in range(waiter_count)
    ]
    for thread in waiters:
        thread.start()
    waiter_start.wait(timeout=5)

    deadline = time.monotonic() + 5
    while _cached_users(target) != waiter_count + 1:
        if time.monotonic() >= deadline:
            raise AssertionError("waiters were not all retained in the lock entry")
        time.sleep(0.005)

    release_holder.set()
    holder.join(timeout=5)
    for thread in waiters:
        thread.join(timeout=5)
    assert not holder.is_alive()
    assert not any(thread.is_alive() for thread in waiters)
    assert sorted(completed) == list(range(waiter_count))
    assert max_active == 1
    assert _cached_users(target) is None


def test_process_lock_entry_is_evicted_after_exception(tmp_path):
    target = tmp_path / "exception.json"

    with pytest.raises(RuntimeError, match="boom"):
        with file_lock(target):
            assert _cached_users(target) == 1
            raise RuntimeError("boom")

    assert _cached_users(target) is None
    with file_lock(target):
        assert _cached_users(target) == 1
    assert _cached_users(target) is None


def test_process_lock_cache_does_not_grow_with_path_churn(tmp_path):
    targets = [tmp_path / f"churn-{index}.json" for index in range(250)]

    for target in targets:
        with file_lock(target):
            assert _cached_users(target) == 1

    with atomic_store._process_locks_guard:
        cached_keys = set(atomic_store._process_locks)
    assert not cached_keys.intersection(_lock_key(target) for target in targets)


def test_file_lock_timeout_and_nonblocking(tmp_path):
    target = tmp_path / "timed.json"
    holder_entered = threading.Event()
    release_holder = threading.Event()

    def holder() -> None:
        with file_lock(target):
            holder_entered.set()
            assert release_holder.wait(timeout=5)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert holder_entered.wait(timeout=5)

        start = time.monotonic()
        with pytest.raises(TimeoutError):
            with file_lock(target, timeout=0.3):
                pass
        assert time.monotonic() - start < 5

        with pytest.raises(TimeoutError):
            with file_lock(target, blocking=False):
                pass
    finally:
        release_holder.set()
        thread.join(timeout=5)
    assert not thread.is_alive()

    # Lock is free again: both variants succeed immediately.
    with file_lock(target, timeout=1.0):
        pass
    with file_lock(target, blocking=False):
        pass
    assert _cached_users(target) is None
