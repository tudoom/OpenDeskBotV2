from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_STATIC = (
    Path(__file__).resolve().parents[1] / "src" / "deskbot_server" / "web" / "static"
)


def test_face_parts_editor_node_contract():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    script = Path(__file__).with_name("js") / "face_parts_editor_2c.test.js"
    spec_helper = _STATIC / "generated" / "primitive_spec.js"
    helper = _STATIC / "face_parts_editor_2c.js"
    test_source = script.read_text(encoding="utf-8")
    for required in (
        'require(path.resolve(__dirname, "../../src/deskbot_server/web/static/generated/primitive_spec.js"));',
        'require(path.resolve(__dirname, "../../src/deskbot_server/web/static/face_parts_editor_2c.js"));',
    ):
        assert required in test_source
        test_source = test_source.replace(required, "")
    subprocess.run(
        [node, "-"],
        input=spec_helper.read_text(encoding="utf-8")
        + "\n"
        + helper.read_text(encoding="utf-8")
        + "\n"
        + test_source,
        text=True,
        check=True,
        timeout=20,
    )
