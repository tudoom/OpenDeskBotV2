"""PC-local playback volume persistence (``data/local/device_volume.json``)."""

from __future__ import annotations

from typing import Any

from deskbot_server.constants import DEVICE_VOLUME_FILE
from deskbot_server.core.json_store import JsonDocumentStore
from deskbot_server.device_data import resolve_json_path
from deskbot_server.pb.servo_pcm import parse_pb_volume

_DEFAULT_VOLUME = 80


def _canonical_doc(raw: Any) -> dict[str, int]:
    source = raw if isinstance(raw, dict) else {}
    volume = parse_pb_volume(source.get("default"))
    return {"default": volume if volume is not None else _DEFAULT_VOLUME}


_STORE = JsonDocumentStore(
    lambda: resolve_json_path(DEVICE_VOLUME_FILE),
    normalize=_canonical_doc,
    default=lambda: _canonical_doc({}),
    corrupt_to_default=True,
)


def get_device_volume() -> int:
    """Read the one PC-local playback volume."""

    doc = _STORE.load()
    v = parse_pb_volume(doc.get("default"))
    return v if v is not None else _DEFAULT_VOLUME


def persist_device_volume(volume: object) -> int:
    """Persist the one PC-local playback volume."""

    v = parse_pb_volume(volume)
    if v is None:
        v = _DEFAULT_VOLUME
    _STORE.save({"default": v})
    return v
