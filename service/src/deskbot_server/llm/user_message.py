"""组装 LLM 用户消息：传感器 + 图像识别 + 用户正文。"""
from __future__ import annotations

from typing import Any, Optional

from deskbot_server.face_snapshot_cache import list_device_faces
from deskbot_server.llm.face_scene import _nose_xy


def _format_face_line(face: dict[str, Any]) -> str:
    fid = face.get("face_id")
    parts: list[str] = [f"faceid={fid if fid is not None else '?'}"]
    face_score = face.get("face_score")
    if face_score is not None:
        try:
            parts.append(f"人脸置信度={float(face_score):.2f}")
        except (TypeError, ValueError):
            pass
    name = str(face.get("person_name") or "").strip() or "未知"
    parts.append(f"name={name}")
    identity_score = face.get("identity_score")
    if identity_score is not None:
        try:
            parts.append(f"人物识别置信度={float(identity_score):.2f}")
        except (TypeError, ValueError):
            pass
    nose = _nose_xy(face)
    if nose is not None:
        nx, ny = int(round(nose[0])), int(round(nose[1]))
        parts.append(f"脸中心位置=({nx},{ny})")
    else:
        parts.append("脸中心位置=未知")
    return ", ".join(parts)


def _sorted_faces_for_message(device_id: str) -> list[dict[str, Any]]:
    faces = list_device_faces(device_id)
    rows: list[dict[str, Any]] = []
    for fid, face in faces.items():
        if not isinstance(face, dict):
            continue
        row = dict(face)
        row.setdefault("face_id", int(fid))
        rows.append(row)
    rows.sort(
        key=lambda r: (
            -(float(r.get("identity_score") or 0.0)),
            int(r.get("face_id") or 0),
        )
    )
    return rows



def build_llm_user_message(
    user_text: str,
    *,
    device_id: Optional[str] = None,
    device_context: Optional[str] = None,
) -> str:
    """按约定格式组装 LLM ``user`` 消息正文。"""
    # ACK pose is transport telemetry and may describe the state before an
    # accepted move.  It is intentionally not injected into model context.
    del device_context
    dev = str(device_id or "").strip()
    lines: list[str] = [
        "[机器人传感器信息:",
        "图像识别:",
    ]
    face_rows: list[dict[str, Any]] = []
    if dev:
        face_rows = _sorted_faces_for_message(dev)
        if face_rows:
            for row in face_rows:
                lines.append(f"   {_format_face_line(row)}")
        else:
            lines.append("   (未检测到人脸)")
    else:
        lines.append("   (无设备)")
    lines.append("]")
    body = (user_text or "").strip()
    if not body:
        body = "[未说话]"
    elif dev and not face_rows:
        lines.append("")
        lines.append(
            "（传感器未检测到人脸；用户已说话时须正常回答用户正文，"
            "勿编造「正在看你」或仅回复「看不到人」而忽略用户问题。）"
        )
    lines.append("")
    lines.append(f"用户正文: {body}")
    return "\n".join(lines)


__all__ = [
    "build_llm_user_message",
]
