"""Application-layer public API with lazy imports.

Importing a lightweight helper such as ``application.llm_tool_runner`` must
not eagerly import OpenCV/MediaPipe and initialise the camera stack.  Besides
slowing CLI and test startup, the old eager imports made unrelated features
fail when an optional vision runtime was unavailable.
"""
from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from deskbot_server.application.camera_broker import (
        CameraImageBroker as CameraImageBroker,
    )
    from deskbot_server.application.camera_frame import (
        analyze_face_detection as analyze_face_detection,
    )
    from deskbot_server.application.chat_flow import (
        publish_chat_turn as publish_chat_turn,
    )
    from deskbot_server.application.chat_flow import (
        run_chat_turn as run_chat_turn,
    )
    from deskbot_server.application.chat_service import (
        ChatService as ChatService,
    )
    from deskbot_server.application.face_detector import (
        CameraFaceDetector as CameraFaceDetector,
    )
    from deskbot_server.application.face_detector import (
        resolve_camera_model_path as resolve_camera_model_path,
    )

_EXPORTS = {
    "CameraImageBroker": (
        "deskbot_server.application.camera_broker",
        "CameraImageBroker",
    ),
    "analyze_face_detection": (
        "deskbot_server.application.camera_frame",
        "analyze_face_detection",
    ),
    "publish_chat_turn": (
        "deskbot_server.application.chat_flow",
        "publish_chat_turn",
    ),
    "run_chat_turn": ("deskbot_server.application.chat_flow", "run_chat_turn"),
    "ChatService": ("deskbot_server.application.chat_service", "ChatService"),
    "CameraFaceDetector": (
        "deskbot_server.application.face_detector",
        "CameraFaceDetector",
    ),
    "resolve_camera_model_path": (
        "deskbot_server.application.face_detector",
        "resolve_camera_model_path",
    ),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attribute = target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value
