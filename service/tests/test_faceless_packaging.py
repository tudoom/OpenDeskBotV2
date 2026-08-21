"""Faceless 打包与运行时降级契约。

产品默认不带人脸功能：``Build-Client.ps1`` 默认从 stage 删除人脸视觉栈
（mediapipe / cv2 / insightface / onnx 及仅被它们拖入的传递依赖），
``-IncludeFaceStack`` 才恢复完整栈。运行时对缺包必须优雅降级：

* ``import deskbot_server.main`` 照常成功（冷启动契约的双保险）；
* 相机预览与拍照问答不受影响（入口校验只用 Pillow/numpy）；
* 人脸检测器初始化失败记一次 warning 后关闭检测，不逐帧重试导入；
* 人脸注册返回明确的「人脸功能未安装」错误，而不是误导性文案；
* Web /people 页在无档案时提示「组件未随本安装包提供」。

缺包用「隐藏包」模拟：子进程 sitecustomize 注入 meta-path 拦截器，对被
拦截的顶层模块名在 ``find_spec`` 即抛 ``ModuleNotFoundError``，与真实
缺包在 ``import`` 和 ``importlib.util.find_spec`` 两条路径上行为一致。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]
_SRC = SERVICE_ROOT / "src"

_SITECUSTOMIZE_BLOCKER = '''\
import sys

_BLOCKED = frozenset({
    "mediapipe", "cv2", "insightface", "onnx",
    "matplotlib", "mpl_toolkits", "scipy", "skimage", "absl",
})


class _FacelessBlocker:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".", 1)[0] in _BLOCKED:
            raise ModuleNotFoundError(
                "No module named %r (faceless build simulation)" % (fullname,)
            )
        return None


sys.meta_path.insert(0, _FacelessBlocker())
'''

_FACELESS_PROBE = '''\
import asyncio
import logging
import sys
from types import SimpleNamespace

# 1) main 的导入闭包不依赖人脸栈（冷启动契约的双保险）。
import deskbot_server.main  # noqa: F401

# 2) 可用性探测在拦截器下必须报「不可用」。
from deskbot_server.application.face_detector import face_stack_available

assert face_stack_available() is False, "face_stack_available must be False"

# 3) ensure_ready：一次 warning、关闭检测、不逐帧重试。
from deskbot_server.application.asr_chat_uplink import AsrChatCameraPipeline
from deskbot_server.vision.undistort import CameraFaceRuntime

messages = []


class _Capture(logging.Handler):
    def emit(self, record):
        messages.append(record.getMessage())


logger = logging.getLogger("deskbot-server")
logger.addHandler(_Capture())
logger.setLevel(logging.DEBUG)

runtime = CameraFaceRuntime(
    undistorter=None,
    min_face_detection_confidence=0.5,
    min_face_presence_confidence=0.5,
    face_embedding_enabled=False,
)
pipe = AsrChatCameraPipeline(runtime=runtime, device_id="dev_faceless_probe")
assert asyncio.run(pipe.ensure_ready()) is False
assert pipe.detector_closed is True, "init failure must close detection"
assert asyncio.run(pipe.ensure_ready()) is False  # closed -> no retry

missing = [m for m in messages if "人脸检测组件未安装" in m]
assert len(missing) == 1, "expected exactly one missing-stack warning, got %r" % (
    missing,
)

# 第二条连接不再重复刷同一条 warning（进程内只记一次）。
pipe2 = AsrChatCameraPipeline(runtime=runtime, device_id="dev_faceless_probe_2")
assert asyncio.run(pipe2.ensure_ready()) is False
assert len([m for m in messages if "人脸检测组件未安装" in m]) == 1

# 4) 注册工具给出明确「人脸功能未安装」。
from deskbot_server.application.face_registration import register_face_for_device

try:
    register_face_for_device("dev_faceless_probe", "测试人名")
except ValueError as exc:
    assert "人脸功能未安装" in str(exc), "unexpected register error: %s" % (exc,)
else:
    raise SystemExit("register_face_for_device must fail without the face stack")

# 5) 相机链路核心：入口 JPEG 校验（Pillow 路径）照常工作。
import io

from PIL import Image

from deskbot_server.llm.vision_input import validate_jpeg

buf = io.BytesIO()
Image.new("RGB", (16, 16), color=(40, 120, 200)).save(buf, format="JPEG")
validated = validate_jpeg(buf.getvalue())
assert validated.width == 16 and validated.height == 16

print("FACELESS_PROBE_OK")
'''


def _run_probe_without_face_stack(tmp_path) -> subprocess.CompletedProcess:
    blocker_dir = tmp_path / "faceless_blocker"
    blocker_dir.mkdir()
    (blocker_dir / "sitecustomize.py").write_text(
        _SITECUSTOMIZE_BLOCKER, encoding="utf-8"
    )
    profile = tmp_path / "profile"
    env = dict(os.environ)
    env["PYTHONPATH"] = (
        str(blocker_dir) + os.pathsep + str(_SRC) + os.pathsep + env.get("PYTHONPATH", "")
    )
    env["PYTHONUTF8"] = "1"
    env.update(
        {
            "DESKBOT_PROJECT_ROOT": str(profile),
            "DESKBOT_DATA_DIR": str(profile / "state"),
            "DESKBOT_MODELS_DIR": str(profile / "models"),
        }
    )
    return subprocess.run(
        [sys.executable, "-c", _FACELESS_PROBE],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=300,
    )


def test_runtime_degrades_gracefully_without_face_stack(tmp_path) -> None:
    proc = _run_probe_without_face_stack(tmp_path)
    assert proc.returncode == 0, (
        f"faceless degradation probe failed:\n{proc.stderr}"
    )
    assert "FACELESS_PROBE_OK" in proc.stdout


def test_face_stack_available_is_true_in_the_full_test_venv() -> None:
    from deskbot_server.application.face_detector import face_stack_available

    try:
        import cv2  # noqa: F401
        import mediapipe  # noqa: F401
    except ImportError:
        import pytest

        pytest.skip("test venv itself lacks the face stack")
    assert face_stack_available() is True


def _build_script_text() -> str:
    return (SERVICE_ROOT / "client" / "Build-Client.ps1").read_text(
        encoding="utf-8"
    )


def test_build_script_defaults_to_faceless_with_explicit_removal_list() -> None:
    build_script = _build_script_text()

    # Opt-in switch: the default build ships without the face stack.
    assert "[switch]$IncludeFaceStack" in build_script
    assert "Remove-FaceStackFromStage" in build_script
    assert "-not $IncludeFaceStack" in build_script

    # The removal list mirrors pyproject's `face` extra plus face-only
    # transitive deps, spelled out as a script array.
    assert "$faceStackSitePackages = @(" in build_script
    removal_block = build_script.split("$faceStackSitePackages = @(", 1)[1]
    removal_block = removal_block.split("\n)", 1)[0]
    for expected in (
        '"mediapipe"',
        '"cv2"',
        '"insightface"',
        '"onnx"',
        '"matplotlib"',
        '"mpl_toolkits"',
        '"scipy"',
        '"scipy.libs"',
        '"skimage"',
        '"imageio"',
        '"tifffile"',
        '"lazy_loader"',
        '"networkx"',
        '"contourpy"',
        '"cycler"',
        '"fontTools"',
        '"kiwisolver"',
        '"pyparsing"',
        '"absl"',
    ):
        assert expected in removal_block, f"missing removal entry {expected}"

    # onnxruntime must NEVER be removed: livekit-plugins-silero (voice VAD)
    # requires it at runtime in every build tier.
    assert '"onnxruntime"' not in removal_block
    assert '"onnxruntime-*.dist-info"' not in removal_block

    # The mediapipe face model only ships with -IncludeFaceStack; silero_vad
    # (voice) always ships.
    assert '$stagedModelNames = @("silero_vad")' in build_script
    assert '"mediapipe", "silero_vad"' in build_script

    # The runtime manifest records which tier was built.
    assert "face_stack = [bool]$IncludeFaceStack" in build_script


def test_build_script_deps_gate_is_tiered_with_negative_import_assertions() -> None:
    build_script = _build_script_text()

    # Full tier keeps the original positive assertions.
    assert (
        "import flask,sqlalchemy,numpy,cv2,mediapipe,aiohttp,livekit"
        in build_script
    )

    # Faceless tier: positive assertions for the retained deps plus negative
    # child-process import assertions for the removed face stack.
    assert "import flask, sqlalchemy, numpy, aiohttp, livekit" in build_script
    assert 'for name in ("mediapipe", "cv2", "insightface"):' in build_script
    assert "except ImportError:" in build_script
    assert "face stack packages leaked into the faceless stage" in build_script

    # Gate code now runs from a file so the multi-line faceless gate survives
    # process argument quoting.
    assert 'Join-Path $buildRoot ("gate-" + $gate.Name + ".py")' in build_script


def test_pyproject_moves_face_stack_into_optional_extra() -> None:
    pyproject = (SERVICE_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    main_deps = pyproject.split("dependencies = [", 1)[1].split("]", 1)[0]
    for absent in ("mediapipe", "insightface", "opencv-contrib-python"):
        assert absent not in main_deps, f"{absent} must not be a main dependency"
    # onnxruntime stays in the main deps for the silero voice VAD.
    assert "onnxruntime" in main_deps
    for retained in ("numpy", "Pillow"):
        assert retained in main_deps, f"{retained} must stay a main dependency"

    assert "face = [" in pyproject
    face_extra = pyproject.split("face = [", 1)[1].split("]", 1)[0]
    for expected in ("mediapipe", "opencv-contrib-python", "insightface", "onnx"):
        assert expected in face_extra, f"face extra must list {expected}"
    assert "onnxruntime" not in face_extra

    requirements = (SERVICE_ROOT / "requirements.txt").read_text(encoding="utf-8")
    for absent in ("mediapipe", "insightface", "opencv-contrib-python"):
        assert f"\n{absent}" not in requirements
    assert "onnxruntime" in requirements
    assert "[face]" in requirements  # 注释里指向 optional extra 的安装方式


def test_people_page_and_api_expose_face_stack_availability() -> None:
    people_html = (
        SERVICE_ROOT
        / "src"
        / "deskbot_server"
        / "web"
        / "templates"
        / "app2c"
        / "people.html"
    ).read_text(encoding="utf-8")
    assert "人脸识别组件未随本安装包提供" in people_html
    assert "faceStackAvailable" in people_html
    assert "face_stack_available" in people_html

    app_bp = (
        SERVICE_ROOT
        / "src"
        / "deskbot_server"
        / "web"
        / "blueprints"
        / "app_bp.py"
    ).read_text(encoding="utf-8")
    assert "face_stack_available" in app_bp
