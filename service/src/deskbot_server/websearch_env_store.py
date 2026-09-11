"""模型配置页「联网检索」：火山方舟 Key 的读写与测试。

联网检索走火山方舟的联网内容插件（Responses API + web_search）。Key 的取法：
本页单独填的 ``ARK_WEB_SEARCH_API_KEY`` 优先；没填时，大模型本身就是火山方舟
就直接复用大模型的 Key（用户要求：填过火山的 Key 就不用再填）；两者都没有则
检索退化到无 Key 的 DuckDuckGo 兜底（国内网络下基本查不到）。
"""

from __future__ import annotations

from typing import Any

from deskbot_server.env import load_dotenv, looks_masked, update_env_keys
from deskbot_server.web_tools import (
    ARK_WEB_SEARCH_API_KEY_ENV,
    ARK_WEB_SEARCH_MODEL_ENV,
    _ark_search_config,
    _ark_web_search,
    ark_search_status,
)

WEBSEARCH_ENV_KEYS = (ARK_WEB_SEARCH_API_KEY_ENV, ARK_WEB_SEARCH_MODEL_ENV)
_ENV_COMMENT = "# 联网检索（火山方舟）"
_TEST_QUERY = "今天北京的天气怎么样"


def websearch_config_status() -> dict[str, Any]:
    load_dotenv()
    return ark_search_status()


def save_websearch_env(payload: dict[str, Any]) -> dict[str, Any]:
    """写入 / 清除单独的检索 Key（掩码或空值不覆盖）；``model`` 可选，空 = 默认。"""
    updates: dict[str, str] = {}
    explicit_empty: set[str] = set()
    if payload.get("clear_api_key"):
        updates[ARK_WEB_SEARCH_API_KEY_ENV] = ""
        explicit_empty.add(ARK_WEB_SEARCH_API_KEY_ENV)
    elif "api_key" in payload:
        key = str(payload.get("api_key") or "").strip()
        if key and not looks_masked(key):
            updates[ARK_WEB_SEARCH_API_KEY_ENV] = key
    if "model" in payload:
        model = str(payload.get("model") or "").strip()
        updates[ARK_WEB_SEARCH_MODEL_ENV] = model
        if not model:
            explicit_empty.add(ARK_WEB_SEARCH_MODEL_ENV)
    if updates:
        update_env_keys(updates, keys=WEBSEARCH_ENV_KEYS, comment=_ENV_COMMENT, write_empty_keys=explicit_empty)
    return websearch_config_status()


def test_websearch(query: str = _TEST_QUERY) -> dict[str, Any]:
    """真的搜一次：证明 Key 能用、走的是方舟而不是兜底引擎。"""
    load_dotenv()
    cfg = _ark_search_config()
    if cfg is None:
        return {
            "ok": False,
            "error": "还没有可用的火山方舟 Key：大模型不是火山方舟时，请在这里单独填一个",
        }
    _url, _key, model = cfg
    result = _ark_web_search(str(query or _TEST_QUERY).strip() or _TEST_QUERY, limit=3) or {}
    if not result.get("ok"):
        return {"ok": False, "error": f"火山方舟联网检索失败：{result.get('error') or '未返回内容'}", "model": model}
    abstract = str(result.get("abstract") or "").strip()
    results = result.get("results") or []
    return {
        "ok": True,
        "model": model,
        "reply": abstract or (f"检索成功，返回 {len(results)} 条来源" if results else "检索成功"),
        "sources": len(results),
    }


__all__ = ["WEBSEARCH_ENV_KEYS", "save_websearch_env", "test_websearch", "websearch_config_status"]
