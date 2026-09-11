"""Initialize the PC-local face library from the read-only system template."""

from __future__ import annotations

import os
from pathlib import Path

from deskbot_server import device_data
from deskbot_server.atomic_store import atomic_write_bytes, file_lock

FACE_DESIGN_FILENAME = "deskbot-face.json"


def global_face_design_path() -> Path:
    return device_data.global_config_dir() / FACE_DESIGN_FILENAME


def local_face_design_path() -> Path:
    return device_data.local_data_dir() / FACE_DESIGN_FILENAME


def resolve_face_data_path() -> Path:
    return local_face_design_path()


def ensure_face_data_file() -> Path:
    device_data.ensure_local_data_initialized()
    path = local_face_design_path()
    template = global_face_design_path()
    with file_lock(path):
        if path.is_file():
            return path
        if not template.is_file():
            raise FileNotFoundError(f"missing global face template: {template}")
        try:
            mode = os.stat(template).st_mode & 0o777
        except OSError:
            mode = None
        atomic_write_bytes(path, template.read_bytes(), mode=mode)
    return path
