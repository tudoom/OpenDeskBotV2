"""LLM 网络工具：webfetch / websearch。"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from html import unescape
from typing import Any

from deskbot_server.safe_fetch import (
    safe_provider_urlopen,
    safe_urlopen,
    validate_public_url,
)

_USER_AGENT = "OpenDesk-Deskbot/1.0"
_MAX_FETCH_BYTES = 120_000
_FETCH_TIMEOUT_SEC = 20
_MAX_SEARCH_RESULTS = 8

# 火山方舟「联网内容插件」：Responses API + tools=[{"type":"web_search"}]，模型自行
# 检索并给出带引用的摘要（output: web_search_call / message.content[].annotations）。
# 计费按实际搜索次数，max_keyword 限制单轮关键词数以控制成本；账号默认 5 QPS。
_ARK_SEARCH_TIMEOUT_SEC = 30
_ARK_SEARCH_MAX_KEYWORD = 3
_ARK_HOST_SUFFIX = "volces.com"
# 模型配置页「联网检索」：单独的方舟 Key 优先；没填时大模型是方舟就复用它的 Key。
ARK_WEB_SEARCH_API_KEY_ENV = "ARK_WEB_SEARCH_API_KEY"
ARK_WEB_SEARCH_MODEL_ENV = "ARK_WEB_SEARCH_MODEL"
DEFAULT_ARK_WEB_SEARCH_MODEL = "doubao-seed-2-0-lite-260428"
DEFAULT_ARK_RESPONSES_URL = "https://ark.cn-beijing.volces.com/api/v3/responses"
ARK_CONSOLE_URL = "https://console.volcengine.com/ark"

_DDG_TOPIC_RE = re.compile(
    r'<a class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>'
    r'.*?<a class="result__snippet"[^>]*>(.*?)</a>',
    re.DOTALL | re.IGNORECASE,
)


def _http_get(url: str, *, timeout: int = _FETCH_TIMEOUT_SEC, max_bytes: int = _MAX_FETCH_BYTES) -> tuple[int, str, bytes]:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": _USER_AGENT, "Accept": "text/html,application/json,*/*"},
        method="GET",
    )
    with safe_urlopen(req, timeout=timeout) as resp:
        status = int(getattr(resp, "status", 200) or 200)
        chunks: list[bytes] = []
        total = 0
        while True:
            block = resp.read(min(8192, max_bytes - total))
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total >= max_bytes:
                break
        body = b"".join(chunks)
    return status, str(resp.headers.get("Content-Type") or ""), body


def webfetch(url: str) -> dict[str, Any]:
    raw = str(url or "").strip()
    if not raw:
        raise ValueError("url 不能为空")
    validate_public_url(raw)
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("仅支持 http/https URL")
    if not parsed.netloc:
        raise ValueError("url 无效")
    try:
        status, content_type, body = _http_get(raw)
    except urllib.error.HTTPError as exc:
        err_body = exc.read(_MAX_FETCH_BYTES) if exc.fp else b""
        text = err_body.decode("utf-8", errors="replace")[:8000]
        return {
            "ok": False,
            "url": raw,
            "status": int(exc.code),
            "content_type": str(exc.headers.get("Content-Type") or ""),
            "error": str(exc.reason),
            "text": text,
        }
    except urllib.error.URLError as exc:
        return {"ok": False, "url": raw, "error": str(exc.reason)}
    text = body.decode("utf-8", errors="replace")
    if len(body) >= _MAX_FETCH_BYTES:
        text += "\n…(内容已截断)"
    return {
        "ok": True,
        "url": raw,
        "status": status,
        "content_type": content_type,
        "bytes": len(body),
        "text": text[:12000],
    }


def _strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html or "")
    return unescape(re.sub(r"\s+", " ", text)).strip()


def _own_search_key() -> str:
    return str(os.environ.get(ARK_WEB_SEARCH_API_KEY_ENV) or "").strip()


def _own_search_model() -> str:
    return str(os.environ.get(ARK_WEB_SEARCH_MODEL_ENV) or "").strip() or DEFAULT_ARK_WEB_SEARCH_MODEL


def _ark_search_config() -> tuple[str, str, str] | None:
    """联网检索用的 (responses_url, api_key, model)。

    单独填的 ``ARK_WEB_SEARCH_API_KEY`` 优先（大模型是 DeepSeek/MiMo 时的唯一出路）；
    没填时大模型本身指向火山方舟就复用它的 Key 与模型；都没有返回 None（调用方降级）。
    """
    own = _own_search_key()
    if own:
        return DEFAULT_ARK_RESPONSES_URL, own, _own_search_model()
    return _llm_ark_search_config()


def ark_search_status() -> dict[str, Any]:
    """模型配置页展示：Key 来源（own/llm/none）、检索模型；不回传 Key。"""
    own = _own_search_key()
    llm = _llm_ark_search_config()
    if own:
        source, model = "own", _own_search_model()
    elif llm is not None:
        source, model = "llm", llm[2]
    else:
        source, model = "none", _own_search_model()
    from deskbot_server.llm.provider_keys import mask_secret

    return {
        "api_key_set": source != "none",
        "api_key_source": source,
        "api_key_masked": mask_secret(own if own else (llm[1] if llm is not None else "")),
        "llm_is_ark": llm is not None,
        "model": model,
        "default_model": DEFAULT_ARK_WEB_SEARCH_MODEL,
        "console_url": ARK_CONSOLE_URL,
    }


def _llm_ark_search_config() -> tuple[str, str, str] | None:
    """当前 LLM 配置若指向火山方舟，返回 (responses_url, api_key, model)，否则 None。"""
    try:
        from deskbot_server.llm.runtime import resolve_llm_config

        cfg = resolve_llm_config()
    except Exception:
        return None
    api_key = str(cfg.api_key or "").strip()
    base = str(cfg.api_base or "").strip().rstrip("/")
    model = str(cfg.model or "").strip()
    if not api_key or not base or not model or "请替换" in api_key:
        return None
    host = (urllib.parse.urlsplit(base).hostname or "").lower()
    if not host.endswith(_ARK_HOST_SUFFIX):
        return None
    for suffix in ("/chat/completions", "/responses"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return f"{base}/responses", api_key, model


def _post_ark_web_search(url: str, payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        },
        method="POST",
    )
    try:
        with safe_provider_urlopen(req, timeout=_ARK_SEARCH_TIMEOUT_SEC) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace").strip()[:600] if exc.fp else ""
        raise RuntimeError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(str(exc.reason)) from exc
    data = json.loads(raw.decode("utf-8", errors="replace"))
    if not isinstance(data, dict):
        raise RuntimeError("响应不是 JSON 对象")
    if data.get("error"):
        raise RuntimeError(json.dumps(data["error"], ensure_ascii=False)[:600])
    return data


def _parse_ark_web_search(data: dict[str, Any], *, limit: int) -> dict[str, Any]:
    summary_parts: list[str] = []
    citations: list[dict[str, str]] = []
    queries: list[str] = []
    seen_urls: set[str] = set()
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "")
        if kind == "web_search_call":
            action = item.get("action") or {}
            q = str(action.get("query") or "").strip() if isinstance(action, dict) else ""
            if q:
                queries.append(q)
        elif kind == "message":
            for content in item.get("content") or []:
                if not isinstance(content, dict):
                    continue
                text = str(content.get("text") or "").strip()
                if text:
                    summary_parts.append(text)
                for ann in content.get("annotations") or []:
                    if not isinstance(ann, dict) or ann.get("type") != "url_citation":
                        continue
                    url = str(ann.get("url") or "").strip()
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    citations.append(
                        {
                            "title": str(ann.get("title") or ann.get("site_name") or url)[:160],
                            "url": url,
                            "snippet": str(ann.get("summary") or "")[:400],
                            "site_name": str(ann.get("site_name") or ""),
                            "publish_time": str(ann.get("publish_time") or ""),
                        }
                    )
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    tool_usage = usage.get("tool_usage") if isinstance(usage.get("tool_usage"), dict) else {}
    return {
        "abstract": "\n".join(summary_parts).strip() or None,
        "results": citations[:limit],
        "queries": queries,
        "search_calls": int(tool_usage.get("web_search") or len(queries)),
    }


def _ark_web_search(q: str, *, limit: int) -> dict[str, Any] | None:
    """走火山方舟联网内容插件；未配置方舟或调用失败时返回 None（由调用方降级）。"""
    cfg = _ark_search_config()
    if cfg is None:
        return None
    url, api_key, model = cfg
    payload = {
        "model": model,
        "stream": False,
        # 搜索+摘要不需要深度思考：实测开启时 ~18s（超过 RTC 工具桥 15s），关闭后 ~6s。
        # 不支持该字段的模型会返回 400，此时去掉后重试一次。
        "thinking": {"type": "disabled"},
        "tools": [{"type": "web_search", "max_keyword": _ARK_SEARCH_MAX_KEYWORD, "limit": limit}],
        "input": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        # 摘要长度是延迟的主要杠杆（实测 200 字 6–10s，60 字 ~4.5s）；
                        # 来源明细走 annotations，正文只要一句结论，agent 会再组织播报。
                        "text": (
                            "联网搜索后用中文一句话回答（不超过 60 字，保留关键数字与时间）：\n"
                            + q
                        ),
                    }
                ],
            }
        ],
    }
    try:
        try:
            data = _post_ark_web_search(url, payload, api_key)
        except RuntimeError as exc:
            if "thinking" not in payload or "HTTP 400" not in str(exc):
                raise
            payload = {k: v for k, v in payload.items() if k != "thinking"}
            data = _post_ark_web_search(url, payload, api_key)
    except (RuntimeError, ValueError, OSError) as exc:
        return {"ok": False, "error": str(exc)[:300]}
    parsed = _parse_ark_web_search(data, limit=limit)
    if not parsed["abstract"] and not parsed["results"]:
        return {"ok": False, "error": "联网搜索未返回内容"}
    return {"ok": True, "provider": "ark_web_search", "model": model, **parsed}


def websearch(query: str, *, max_results: int = _MAX_SEARCH_RESULTS) -> dict[str, Any]:
    q = str(query or "").strip()
    if not q:
        raise ValueError("query 不能为空")
    limit = max(1, min(int(max_results), _MAX_SEARCH_RESULTS))

    # 首选：火山方舟联网内容插件（豆包模型自带检索 + 摘要 + 引用）
    ark = _ark_web_search(q, limit=limit)
    if ark is not None and ark.get("ok"):
        return {"query": q, **ark}
    ark_error = (ark or {}).get("error") if isinstance(ark, dict) else None

    # 兜底：DuckDuckGo Instant Answer API（无密钥；国内网络下常不可达）
    api_url = "https://api.duckduckgo.com/?" + urllib.parse.urlencode(
        {"q": q, "format": "json", "no_html": 1, "skip_disambig": 1}
    )
    results: list[dict[str, str]] = []
    abstract = ""
    try:
        status, _ct, body = _http_get(api_url, max_bytes=64_000)
        if status == 200:
            data = json.loads(body.decode("utf-8", errors="replace"))
            abstract = str(data.get("AbstractText") or "").strip()
            if abstract:
                results.append(
                    {
                        "title": str(data.get("Heading") or "摘要"),
                        "url": str(data.get("AbstractURL") or ""),
                        "snippet": abstract,
                    }
                )
            for topic in data.get("RelatedTopics") or []:
                if len(results) >= limit:
                    break
                if isinstance(topic, dict) and topic.get("Text"):
                    results.append(
                        {
                            "title": str(topic.get("Text") or "")[:120],
                            "url": str(topic.get("FirstURL") or ""),
                            "snippet": str(topic.get("Text") or "")[:400],
                        }
                    )
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        pass

    # HTML 备用检索
    if len(results) < limit:
        html_url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": q})
        try:
            _status, _ct, body = _http_get(html_url, max_bytes=200_000)
            html = body.decode("utf-8", errors="replace")
            for m in _DDG_TOPIC_RE.finditer(html):
                if len(results) >= limit:
                    break
                href = unescape(m.group(1))
                title = _strip_html(m.group(2))
                snippet = _strip_html(m.group(3))
                if title:
                    results.append({"title": title, "url": href, "snippet": snippet})
        except (urllib.error.URLError, OSError):
            pass

    out: dict[str, Any] = {
        "ok": True,
        "provider": "duckduckgo",
        "query": q,
        "results": results[:limit],
        "abstract": abstract or None,
    }
    if ark_error:
        out["ark_error"] = ark_error
    return out
