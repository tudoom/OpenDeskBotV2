"""冷启动导入契约：``deskbot_server.main`` 的导入闭包必须不含视觉重库。

Core 的「进程拉起 → :9000 就绪」耗时由 ``import deskbot_server.main`` 的导入
闭包决定。mediapipe / insightface / onnxruntime / cv2 / PIL 只在相机帧到达或
人脸功能被使用时才需要，且它们的首次导入已被安排在 ``asyncio.to_thread``
的 worker 线程（``CameraFaceDetector`` 构造、``validate_jpeg_with_rgb``、
``FaceEmbeddingEngine`` 懒加载）。本测试在干净的子进程里导入 main 并断言
这些模块名不出现在 ``sys.modules``，钉住惰性化不被将来的模块级 import 回退。

允许留在闭包里的重依赖（有意为之，勿加入禁止清单）：

* ``sqlalchemy`` — ``init_database`` 启动即用；
* ``numpy`` — ``rtc_runtime`` 的远端 PCM 重采样与 ``pipeline.mic_health``
  的首块 PCM 体检都在事件循环/串口路径上，惰性化会把 numpy 的首次重导入
  搬进事件循环，违反事件循环卫生红线。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"

# 模块级 import 一旦把任何一个名字拉回 deskbot_server.main 的导入闭包，
# 冷启动就重新为整个视觉栈买单（客户机实测曾达 80s+）。
FORBIDDEN_IN_MAIN_CLOSURE = (
    "mediapipe",
    "insightface",
    "onnxruntime",
    "cv2",
    "PIL",
)


def test_main_import_closure_excludes_vision_stack() -> None:
    probe = (
        "import json, sys\n"
        "import deskbot_server.main\n"
        f"forbidden = {FORBIDDEN_IN_MAIN_CLOSURE!r}\n"
        "loaded = sorted(\n"
        "    name\n"
        "    for name in forbidden\n"
        "    if name in sys.modules\n"
        "    or any(mod.startswith(name + '.') for mod in sys.modules)\n"
        ")\n"
        "print('CLOSURE_PROBE:' + json.dumps(loaded))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_SRC) + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    assert proc.returncode == 0, (
        f"import deskbot_server.main failed in a clean subprocess:\n{proc.stderr}"
    )
    marker_lines = [
        line
        for line in proc.stdout.splitlines()
        if line.startswith("CLOSURE_PROBE:")
    ]
    assert marker_lines, f"probe produced no marker line; stdout={proc.stdout!r}"
    loaded = json.loads(marker_lines[-1].removeprefix("CLOSURE_PROBE:"))
    assert loaded == [], (
        f"heavy vision modules re-entered deskbot_server.main's import closure: "
        f"{loaded}; move the offending module-level import into the function "
        "that uses it (first use must stay on a to_thread/worker thread)"
    )
