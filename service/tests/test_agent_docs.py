from __future__ import annotations

import pytest


@pytest.fixture()
def docs_home(monkeypatch, tmp_path):
    local = tmp_path / "local"
    local.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("deskbot_server.memory_md.local_data_dir", lambda: local)
    return local


def test_the_three_documents_are_named_for_what_they_hold(docs_home):
    from deskbot_server import agent_docs

    names = {row["name"] for row in agent_docs.list_docs()}
    assert names == {"Agent.md", "Memory.md", "User.md"}


def test_reading_an_absent_document_seeds_it(docs_home):
    from deskbot_server import agent_docs

    text = agent_docs.read_doc("Agent.md")
    assert text.strip()
    assert agent_docs.doc_path("Agent.md").is_file()


def test_documents_resolve_by_short_key_too(docs_home):
    from deskbot_server import agent_docs

    agent_docs.write_doc("user", "# 关于主人\n\n- 喜欢安静\n")
    assert "喜欢安静" in agent_docs.read_doc("User.md")


def test_user_note_appends_instead_of_overwriting(docs_home):
    """One new observation must not wipe everything learned so far."""
    from deskbot_server.application.llm_tool_runner import execute_llm_tools

    execute_llm_tools(
        [{"tool": "user_note", "action": "append", "text": "说话直接"}],
        device_id="dev",
    )
    execute_llm_tools(
        [{"tool": "user_note", "action": "append", "text": "晚上不回消息"}],
        device_id="dev",
    )

    from deskbot_server import agent_docs

    text = agent_docs.read_doc("User.md")
    assert "说话直接" in text
    assert "晚上不回消息" in text


def test_user_note_replace_rewrites_the_whole_note(docs_home):
    from deskbot_server import agent_docs
    from deskbot_server.application.llm_tool_runner import execute_llm_tools

    agent_docs.write_doc("User.md", "# 关于主人\n\n- 旧的理解\n")
    execute_llm_tools(
        [{"tool": "user_note", "action": "replace", "text": "# 关于主人\n\n- 新的理解\n"}],
        device_id="dev",
    )

    text = agent_docs.read_doc("User.md")
    assert "新的理解" in text
    assert "旧的理解" not in text


def test_user_note_read_returns_the_current_text(docs_home):
    from deskbot_server import agent_docs
    from deskbot_server.application.llm_tool_runner import execute_llm_tools

    agent_docs.write_doc("User.md", "# 关于主人\n\n- 喜欢猫\n")
    rows = execute_llm_tools(
        [{"tool": "user_note", "action": "read"}], device_id="dev"
    )
    assert "喜欢猫" in rows[0]["text"]


def test_append_without_text_is_refused(docs_home):
    from deskbot_server.application.llm_tool_runner import execute_llm_tools

    rows = execute_llm_tools(
        [{"tool": "user_note", "action": "append"}], device_id="dev"
    )
    assert rows[0].get("ok") is not True


def test_the_tool_is_offered_to_the_model(docs_home):
    from deskbot_server.rtc_worker_tools import build_rtc_tool_schemas

    schema = next(
        (s for s in build_rtc_tool_schemas() if s["name"] == "user_note"), None
    )
    assert schema is not None, "模型看不到这个工具就不会主动记录"
    assert set(schema["parameters"]["properties"]["action"]["enum"]) == {
        "read",
        "append",
        "replace",
    }


def test_untouched_user_seed_stays_out_of_the_prompt(docs_home):
    """A seed file is boilerplate, not something the model should be told."""
    from deskbot_server import agent_docs

    assert agent_docs.user_prompt_appendix() == ""

    agent_docs.write_doc("User.md", "# 关于主人\n\n- 说话直接\n")
    assert "说话直接" in agent_docs.user_prompt_appendix()
