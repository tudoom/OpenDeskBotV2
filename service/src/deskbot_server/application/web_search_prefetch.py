"""文本对话的联网预取。

控制台文字对话走「模型第一轮 → JSON 里请求 websearch → Core 联网 → 第二轮」，
实测 3s + 5–6s + 3s ≈ 11s。时效性问题（天气/新闻/股价…）几乎必然要联网，
所以用户消息一到就并行发起搜索，等模型真的请求 websearch 时直接拿预取结果，
把联网那段从串行变成与第一轮模型调用重叠：≈ 3s + 3s。

预取只对第一次 websearch 请求生效；模型没请求就丢弃结果（一次搜索的成本），
预取失败则回落到正常的工具执行，不改变原有语义。
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

logger = logging.getLogger("deskbot-server")

# 命中即预取：时间词 × 时效性主题，或明确的"实时"类主题。
_TIME_WORDS = r"(今天|今日|现在|目前|最近|最新|当前|昨天|明天|本周|这周|刚刚|实时|近期)"
_TOPIC_WORDS = (
    r"(天气|气温|下雨|降雨|台风|新闻|头条|热点|股价|股票|大盘|指数|汇率|金价|油价|"
    r"价格|行情|比分|赛果|赛程|发布|上市|限行|航班|路况|疫情|地震)"
)
_TIME_SENSITIVE_RE = re.compile(
    rf"(?:{_TIME_WORDS}.*{_TOPIC_WORDS})|(?:{_TOPIC_WORDS}.*{_TIME_WORDS})|(?:天气|股价|新闻|头条|比分|汇率|金价|油价)"
)
_WAIT_TIMEOUT_SEC = 40.0
_PREFETCH_RESULTS = 5

# 模型没请求 websearch 时预取结果会被丢弃，但那次搜索仍会跑完（最长 45s）；
# 池子留够并发，避免两个滞留任务就把后面的预取排成队。
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="websearch-prefetch")


def looks_time_sensitive(text: str) -> bool:
    return bool(_TIME_SENSITIVE_RE.search(str(text or "")))


def start_prefetch(text: str) -> Future | None:
    """命中时效性规则时后台发起联网搜索；否则返回 None。"""
    query = str(text or "").strip()
    if not query or not looks_time_sensitive(query):
        return None
    from deskbot_server.web_tools import websearch

    def _run() -> dict[str, Any]:
        return websearch(query, max_results=_PREFETCH_RESULTS)

    return _executor.submit(_run)


def take_prefetched_websearch(
    round_tools: list[dict[str, Any]],
    future: Future | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """把模型请求的第一个 websearch 换成预取结果。

    返回 (prefetched_results, remaining_tools, used)：``remaining_tools`` 交给
    正常的 execute_llm_tools；预取未命中/失败时原样返回全部工具，used=False。
    """
    if future is None:
        return [], list(round_tools), False
    index = next(
        (
            i
            for i, t in enumerate(round_tools)
            if str(t.get("tool") or t.get("name") or "").strip().lower() == "websearch"
        ),
        None,
    )
    if index is None:
        return [], list(round_tools), False
    try:
        out = future.result(timeout=_WAIT_TIMEOUT_SEC)
    except Exception as exc:  # noqa: BLE001 —— 预取失败就走正常执行
        logger.info("web 对话联网预取失败，回落正常执行: %s", str(exc)[:200])
        return [], list(round_tools), False
    if not isinstance(out, dict) or not out.get("ok"):
        return [], list(round_tools), False
    result = {"tool": "websearch", **out}
    # 记录模型自己拟的检索词，便于对照预取用的原话
    requested = str(round_tools[index].get("query") or round_tools[index].get("q") or "").strip()
    if requested:
        result["model_query"] = requested
    remaining = [t for i, t in enumerate(round_tools) if i != index]
    return [result], remaining, True


def discard_prefetch(future: Future | None) -> None:
    """对话结束仍未消费的预取：还在排队的直接取消，已在跑的让它自然结束。"""
    if future is not None and not future.done():
        future.cancel()


__all__ = [
    "discard_prefetch",
    "looks_time_sensitive",
    "start_prefetch",
    "take_prefetched_websearch",
]
