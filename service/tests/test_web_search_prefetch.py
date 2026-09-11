from __future__ import annotations

from concurrent.futures import Future

from deskbot_server.application import web_search_prefetch as prefetch


def test_time_sensitive_detection():
    hits = [
        "今天天气如何?",
        "今天有什么科技新闻",
        "英伟达现在股价多少",
        "最近有什么热点",
        "明天北京会下雨吗",
        "金价怎么样",
    ]
    misses = ["你叫什么名字", "把头转向左边", "给我讲个笑话", "帮我记一下明天开会"]
    for text in hits:
        assert prefetch.looks_time_sensitive(text), text
    for text in misses:
        assert not prefetch.looks_time_sensitive(text), text


def test_start_prefetch_only_for_time_sensitive(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        "deskbot_server.web_tools.websearch",
        lambda q, max_results=5: calls.append(q) or {"ok": True, "query": q, "results": []},
    )
    assert prefetch.start_prefetch("你叫什么名字") is None
    fut = prefetch.start_prefetch("今天天气如何?")
    assert fut is not None
    assert fut.result(timeout=5)["query"] == "今天天气如何?"
    assert calls == ["今天天气如何?"]


def _done(value) -> Future:
    fut: Future = Future()
    fut.set_result(value)
    return fut


def test_take_prefetched_replaces_first_websearch_only():
    tools = [
        {"tool": "websearch", "query": "北京 天气"},
        {"tool": "miot", "action": "list"},
        {"tool": "websearch", "query": "上海 天气"},
    ]
    fut = _done({"ok": True, "provider": "ark_web_search", "abstract": "晴", "results": [{"url": "u"}]})
    results, remaining, used = prefetch.take_prefetched_websearch(tools, fut)
    assert used is True
    assert results == [
        {
            "tool": "websearch",
            "ok": True,
            "provider": "ark_web_search",
            "abstract": "晴",
            "results": [{"url": "u"}],
            "model_query": "北京 天气",
        }
    ]
    assert remaining == [tools[1], tools[2]]


def test_take_prefetched_falls_back_without_search_or_on_failure():
    tools = [{"tool": "miot", "action": "list"}]
    assert prefetch.take_prefetched_websearch(tools, _done({"ok": True})) == ([], tools, False)
    assert prefetch.take_prefetched_websearch(tools, None) == ([], tools, False)

    search_tools = [{"tool": "websearch", "query": "x"}]
    failed: Future = Future()
    failed.set_exception(RuntimeError("boom"))
    assert prefetch.take_prefetched_websearch(search_tools, failed) == ([], search_tools, False)
    assert prefetch.take_prefetched_websearch(search_tools, _done({"ok": False})) == (
        [],
        search_tools,
        False,
    )


def test_discard_prefetch_cancels_queued_future():
    from concurrent.futures import Future

    from deskbot_server.application.web_search_prefetch import discard_prefetch

    fut: Future = Future()
    discard_prefetch(fut)
    assert fut.cancelled()
    discard_prefetch(None)  # 无预取时是 no-op
