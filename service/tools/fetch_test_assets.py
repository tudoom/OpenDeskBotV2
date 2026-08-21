#!/usr/bin/env python3
"""Fetch CI-only binary assets from immutable, checksum-verified sources."""

from __future__ import annotations

import argparse
import hashlib
import os
import tempfile
from pathlib import Path
from urllib.request import urlopen

SILERO_COMMIT = "76e3dc408eb2a5c655c34e230d2d5459b4439daa"
SILERO_SHA256 = "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3"
SILERO_URL = (
    "https://raw.githubusercontent.com/snakers4/silero-vad/"
    f"{SILERO_COMMIT}/src/silero_vad/data/silero_vad.onnx"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_silero(target: Path) -> bool:
    return target.is_file() and _sha256(target) == SILERO_SHA256


def fetch_silero(target: Path) -> None:
    target = target.resolve()
    if verify_silero(target):
        print(f"verified existing Silero VAD model: {target}")
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".download",
        dir=target.parent,
    )
    temp = Path(raw_temp)
    try:
        with os.fdopen(fd, "wb") as output, urlopen(SILERO_URL, timeout=60) as response:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        actual = _sha256(temp)
        if actual != SILERO_SHA256:
            raise RuntimeError(
                f"Silero VAD checksum mismatch: expected {SILERO_SHA256}, got {actual}"
            )
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    print(f"downloaded and verified Silero VAD model: {target}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--silero-target",
        type=Path,
        default=Path("models/silero_vad/silero_vad.onnx"),
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="fail unless the existing model matches the pinned checksum",
    )
    args = parser.parse_args()
    if args.check_only:
        if not verify_silero(args.silero_target.resolve()):
            raise SystemExit("Silero VAD model is missing or has an invalid checksum")
        return
    fetch_silero(args.silero_target)


if __name__ == "__main__":
    main()
