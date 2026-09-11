from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select


@pytest.fixture()
def receipt_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "receipts.db"
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


def test_playback_receipts_are_idempotent_and_batch_aliases_are_atomic(
    receipt_db,
):
    from deskbot_server.db.engine import get_session
    from deskbot_server.db.models import PlaybackReceipt
    from deskbot_server.playback_receipts import (
        get_playback_receipt,
        record_playback_receipts,
    )

    played_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    first = record_playback_receipts(
        "deskbot-receipts",
        ["pb-req-1", "occurrence-1", "pb-req-1"],
        played_at=played_at,
        retention_days=2,
    )
    repeated = record_playback_receipts(
        "deskbot-receipts",
        ["occurrence-1"],
        played_at=played_at + timedelta(days=1),
        retention_days=100,
    )

    assert [row.request_id for row in first] == ["pb-req-1", "occurrence-1"]
    assert repeated[0].played_at == played_at
    assert repeated[0].expires_at == played_at + timedelta(days=2)
    assert get_playback_receipt(
        "deskbot-receipts", "occurrence-1"
    ) == repeated[0]

    session = get_session()
    try:
        count = session.scalar(select(func.count()).select_from(PlaybackReceipt))
    finally:
        session.close()
    assert count == 2


def test_playback_receipt_retention_expires_and_purges_rows(receipt_db):
    from deskbot_server.playback_receipts import (
        has_playback_receipt,
        purge_expired_playback_receipts,
        record_playback_receipt,
    )

    played_at = datetime(2026, 2, 1, tzinfo=timezone.utc)
    record_playback_receipt(
        "deskbot-retention",
        "old-request",
        played_at=played_at,
        retention_days=1,
    )

    assert has_playback_receipt(
        "deskbot-retention",
        "old-request",
        now=played_at + timedelta(hours=23),
    )
    assert not has_playback_receipt(
        "deskbot-retention",
        "old-request",
        now=played_at + timedelta(days=1),
    )
    assert (
        purge_expired_playback_receipts(
            now=played_at + timedelta(days=1)
        )
        == 1
    )


def test_terminal_pb_ack_persists_the_concrete_request(receipt_db):
    from deskbot_server.application.chat_flow import _send_pb_pairs
    from deskbot_server.playback_receipts import has_playback_receipt
    from deskbot_server.ws.pb_ack_waiter import pb_ack_gate

    class _Downlink:
        async def send_pb_wire(self, _wire, binaries=None):
            return True

    async def _run() -> None:
        device_id = "deskbot-pb-receipt"
        request_id = "pb-terminal-request"

        async def _acks() -> None:
            await asyncio.sleep(0.02)
            await pb_ack_gate.notify(
                device_id,
                {
                    "req": request_id,
                    "idx": 0,
                    "phase": "accepted",
                },
            )
            await asyncio.sleep(0.02)
            await pb_ack_gate.notify(
                device_id,
                {
                    "req": request_id,
                    "idx": 0,
                    "phase": "played",
                },
            )

        notifier = asyncio.create_task(_acks())
        delivery = await _send_pb_pairs(
            _Downlink(),
            pairs=[
                (
                    {
                        "type": "pb_single",
                        "req": request_id,
                        "idx": 0,
                        "chunk_ms": 20,
                        "audio": {"next_bin_len": 4},
                    },
                    [b"\0\0\0\0"],
                )
            ],
            pb_req=request_id,
            device_id=device_id,
            n_pb=1,
            persist_played_receipt=True,
        )
        await notifier
        assert delivery == "played"
        assert await pb_ack_gate.state_count() == 0
        assert has_playback_receipt(device_id, request_id)

    asyncio.run(_run())
