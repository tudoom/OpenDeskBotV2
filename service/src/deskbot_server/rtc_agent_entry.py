"""Portable module entry point for the LampGo LiveKit Agent SDK.

The generated console-script executable embeds the build machine's Python
path on Windows.  Launching the Typer application as a module keeps worker
spawning portable inside the desktop client's extracted Python runtime.
"""

from __future__ import annotations

from lampgo_livekit_agent.cli import app


def main() -> None:
    app()


if __name__ == "__main__":
    main()
