from __future__ import annotations

import asyncio
from datetime import datetime

import pytest


@pytest.fixture()
def memory_home(monkeypatch, tmp_path):
    local = tmp_path / "local"
    local.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("deskbot_server.memory_md.local_data_dir", lambda: local)
    monkeypatch.delenv("DESKBOT_MEMORY_CONSOLIDATION_HOUR", raising=False)
    return local


def test_a_machine_that_was_off_at_night_still_consolidates_when_it_wakes(
    memory_home,
):
    from deskbot_server import memory_consolidation as mc

    # 台式机在凌晨三点多半是关着的；按日历日判定，中午开机也要补上。
    assert mc.should_run(
        {"last_run_date": "2026-08-22"}, now=datetime(2026, 8, 25, 12, 0), hour=3
    )
    assert mc.should_run({}, now=datetime(2026, 8, 25, 3, 0), hour=3)


def test_it_runs_at_most_once_a_day(memory_home):
    from deskbot_server import memory_consolidation as mc

    assert not mc.should_run(
        {"last_run_date": "2026-08-25"}, now=datetime(2026, 8, 25, 23, 0), hour=3
    )
    # 刚过午夜、还没到整理时刻，昨晚那次已经算数了。
    assert not mc.should_run(
        {"last_run_date": "2026-08-24"}, now=datetime(2026, 8, 25, 1, 0), hour=3
    )


def test_only_days_after_the_last_pass_are_reconsidered(memory_home):
    from deskbot_server import memory_consolidation as mc
    from deskbot_server import memory_md

    for day in ("2026-08-20", "2026-08-21", "2026-08-22"):
        memory_md.write_daily(day, [{"id": f"x{day[-2:]}", "text": "x"}])

    pending = mc.pending_dates({"last_consolidated_date": "2026-08-20"})
    assert pending == ["2026-08-21", "2026-08-22"]


def test_consolidation_rewrites_the_master_and_records_progress(
    memory_home, monkeypatch
):
    from deskbot_server import memory_consolidation as mc
    from deskbot_server import memory_md

    memory_md.write_master([{"id": "old001", "text": "主人喜欢咖啡"}])
    memory_md.write_daily(
        "2026-08-24",
        [
            {"id": "d00001", "text": "主人说更喜欢美式，不加糖"},
            {"id": "d00002", "text": "今天中午吃了面"},
        ],
    )

    captured: dict = {}

    async def fake_llm(messages, **kwargs):
        captured["prompt"] = messages[0]["content"]
        captured["json_mode"] = kwargs.get("json_mode")
        return (
            "- [old001] 主人喜欢喝美式咖啡，不加糖\n- [new] 主人不爱吃甜食\n",
            {},
        )

    monkeypatch.setattr("deskbot_server.llm.runtime.chat_acompletion", fake_llm)

    result = asyncio.run(mc.consolidate_once(now=datetime(2026, 8, 25, 3, 0)))

    assert result["ok"] is True
    assert result["before"] == 1 and result["after"] == 2
    # 纯文本条目行，不能走 JSON 模式。
    assert captured["json_mode"] is False
    # 当天的琐事进了提示词，交给模型判断该不该留。
    assert "今天中午吃了面" in captured["prompt"]

    rows = memory_md.read_master()
    assert rows[0]["id"] == "old001", "保留原 id，删除入口才不会失效"
    assert "美式" in rows[0]["text"]
    assert rows[1]["id"] != "new", "new 占位必须换成真实 id"

    state = mc.load_state()
    assert state["last_run_date"] == "2026-08-25"
    assert state["last_consolidated_date"] == "2026-08-24"


def test_a_failed_pass_leaves_the_existing_master_untouched(memory_home, monkeypatch):
    from deskbot_server import memory_consolidation as mc
    from deskbot_server import memory_md

    memory_md.write_master([{"id": "keep01", "text": "别弄丢我"}])
    memory_md.write_daily("2026-08-24", [{"id": "d1", "text": "今天下雨"}])

    async def boom(messages, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr("deskbot_server.llm.runtime.chat_acompletion", boom)

    result = asyncio.run(mc.consolidate_once(now=datetime(2026, 8, 25, 3, 0)))

    assert result["ok"] is False
    assert memory_md.read_master() == [{"id": "keep01", "text": "别弄丢我"}]
    # 没有记为已完成，明晚会重试。
    assert mc.load_state().get("last_run_date") != "2026-08-25"


def test_an_empty_reply_never_wipes_the_master(memory_home, monkeypatch):
    from deskbot_server import memory_consolidation as mc
    from deskbot_server import memory_md

    memory_md.write_master([{"id": "keep01", "text": "别弄丢我"}])
    memory_md.write_daily("2026-08-24", [{"id": "d1", "text": "今天下雨"}])

    async def empty(messages, **kwargs):
        return ("我想不出要保留什么。", {})

    monkeypatch.setattr("deskbot_server.llm.runtime.chat_acompletion", empty)

    result = asyncio.run(mc.consolidate_once(now=datetime(2026, 8, 25, 3, 0)))
    assert result["ok"] is False
    assert memory_md.read_master() == [{"id": "keep01", "text": "别弄丢我"}]


def test_duplicate_ids_in_the_reply_are_given_fresh_ones(memory_home):
    from deskbot_server import memory_consolidation as mc

    rows = mc.parse_master_reply("- [same] 第一条\n- [same] 第二条\n")
    assert len(rows) == 2
    assert rows[0]["id"] != rows[1]["id"], "重复 id 会让删除删错条目"
