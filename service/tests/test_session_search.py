from __future__ import annotations

import json
import time

import pytest


@pytest.fixture()
def session_home(monkeypatch, tmp_path):
    local = tmp_path / "local"
    (local / "session").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "deskbot_server.session_search.local_data_dir", lambda: local
    )
    return local


def _write(local, session_id, title, turns):
    (local / "session" / f"{session_id}.json").write_text(
        json.dumps(
            {
                "session_id": session_id,
                "title": title,
                "messages": [
                    {"role": role, "message": text, "ts": ts}
                    for role, text, ts in turns
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_search_reaches_the_whole_archive_not_just_recent_sessions(session_home):
    from deskbot_server import session_search

    now = time.time()
    _write(
        session_home,
        "old",
        "很久以前",
        [("user", "我最喜欢喝美式咖啡", now - 86400 * 60)],
    )
    _write(session_home, "new", "今天", [("user", "今天去了公园", now)])

    hits = session_search.search("美式咖啡")
    assert [h["session_id"] for h in hits] == ["old"]
    # 命中片段带出上下文，模型不必再把整个会话读回来。
    assert "美式咖啡" in hits[0]["snippet"]


def test_short_chinese_queries_fall_back_to_a_scan(session_home):
    from deskbot_server import session_search

    _write(session_home, "s1", "宠物", [("user", "今天带团子去看兽医", time.time())])

    # trigram 索引不响应两字查询，必须有兜底，否则"团子"这种常见词永远搜不到。
    hits = session_search.search("团子")
    assert len(hits) == 1
    assert hits[0]["session_id"] == "s1"


def test_role_filter_separates_what_the_user_said(session_home):
    from deskbot_server import session_search

    now = time.time()
    _write(
        session_home,
        "s1",
        "咖啡",
        [
            ("user", "我喜欢美式咖啡", now),
            ("assistant", "记住啦，你喜欢美式咖啡", now + 1),
        ],
    )

    assert len(session_search.search("美式咖啡")) == 2
    assert len(session_search.search("美式咖啡", role="user")) == 1


def test_index_follows_edits_and_deletions(session_home):
    from deskbot_server import session_search

    now = time.time()
    _write(session_home, "s1", "聊天", [("user", "第一句", now)])
    assert session_search.search("第一句")

    _write(
        session_home,
        "s1",
        "聊天",
        [("user", "第一句", now), ("user", "兽医说要多喝水", now + 1)],
    )
    assert session_search.search("多喝水"), "改动后的会话必须能被搜到"

    (session_home / "session" / "s1.json").unlink()
    assert session_search.search("第一句") == [], "会话删除后索引要跟着清"


def test_punctuation_in_the_query_is_not_treated_as_fts_syntax(session_home):
    from deskbot_server import session_search

    _write(session_home, "s1", "引用", [("user", '他说"下周出差"', time.time())])

    # 未转义的引号会让 FTS5 把查询当语法解析并报错。
    assert session_search.search('"下周出差"')


def test_session_tool_exposes_search(session_home, monkeypatch):
    from deskbot_server import session_search, session_store

    monkeypatch.setattr(session_store, "local_data_dir", lambda: session_home)
    _write(session_home, "s1", "咖啡", [("user", "我喜欢美式咖啡", time.time())])
    assert session_search.stats()["messages"] == 1

    result = session_store.execute_session_tool(
        {"action": "search", "query": "美式咖啡"}
    )
    assert result["ok"] is True
    assert result["count"] == 1
    assert result["hits"][0]["session_id"] == "s1"

    with pytest.raises(ValueError, match="query"):
        session_store.execute_session_tool({"action": "search"})
