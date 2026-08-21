"""旧 ``emotion_expr_map.json`` 的只读迁移入口。

情绪→场景映射的权威存储是 ``deskbot-face.json`` 的 ``mappings``（经
``/api/face_expression_transaction`` 读写）。旧拆分文件只在尚未写入
schema v2 的历史安装上作为一次性迁移来源被读取（``face_design_store``
seed 时、``expression_runtime`` 目录构建缺 mappings 时）；写路径与旧
``/api/emotion_expr_map`` 端点已删除。
"""

from __future__ import annotations

import json
import os

from deskbot_server.constants import EMOTION_EXPR_MAP_FILE
from deskbot_server.device_data import resolve_json_path


def _normalize(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ValueError("emotion map must be an object")
    out: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(v, str):
            raise ValueError(f"scene for emotion {k!r} must be a string")
        out[str(k)] = v
    return out


def load_legacy_emotion_expr_map() -> dict[str, str]:
    """Read the retired split file without consulting the face document."""

    path = resolve_json_path(EMOTION_EXPR_MAP_FILE)
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return _normalize(json.load(f))
