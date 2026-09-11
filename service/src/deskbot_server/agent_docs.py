"""The three Markdown documents behind the Agent 伙伴 page.

Everything the agent carries between conversations lives in one directory
as plain Markdown, so it can be read, diffed and hand-edited:

``Agent.md``
    How the robot should behave — the behaviour preferences that used to
    sit behind a form.
``Memory.md``
    Durable facts, written the moment they are learned and curated nightly.
``User.md``
    What the agent has come to think about the person it lives with,
    written by the agent rather than configured by hand.

Memory.md is the same file the memory store already owns; the other two
are new here. Keeping all three behind one accessor means the page, the
prompt assembly and the nightly pass agree on where they are.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deskbot_server import memory_md
from deskbot_server.atomic_store import atomic_write_text

AGENT_FILENAME = "Agent.md"
USER_FILENAME = "User.md"
MEMORY_FILENAME = "Memory.md"

MAX_DOC_BYTES = 64 * 1024

USER_TITLE = "# 关于主人"


def _agent_seed() -> str:
    """Agent.md 就是这台机器人的系统提示，首次打开时装载真实人设。

    人设原本只存在于 data/global/llm_system.txt，页面上看不到也改不了，
    于是 Agent.md 写着「行为偏好」的空模板，机器人却自称小歪——两边对不上。
    这里直接以现行提示词作为初始内容，编辑它就是在改人设。
    """
    from deskbot_server.device_data import load_llm_system_prompt

    try:
        text = (load_llm_system_prompt() or "").strip()
    except Exception:  # noqa: BLE001 - 读不到就退回一份空骨架
        text = ""
    return (text + "\n") if text else "# 人设与行为\n\n（还没有设定。）\n"

USER_SEED = f"""{USER_TITLE}

这份文件由机器人自己维护，记录它对主人的理解。你也可以手动修改。

（还没有足够的了解。）
"""

_DOCS: dict[str, str] = {
    AGENT_FILENAME: "agent",
    MEMORY_FILENAME: "memory",
    USER_FILENAME: "user",
}


MEMORY_SEED = """# 长期记忆

值得记住的事会在对话中即时写进这里；每晚的整理任务负责去重、提炼，
并丢掉当天就失效的琐事。你也可以手动增删。

（还没有记忆。）
"""


def _seed_for(name: str) -> str:
    if name == AGENT_FILENAME:
        return _agent_seed()
    if name == USER_FILENAME:
        return USER_SEED
    if name == MEMORY_FILENAME:
        return MEMORY_SEED
    return ""

_BY_KEY = {key: name for name, key in _DOCS.items()}


def docs_dir() -> Path:
    return memory_md.memory_dir()


def doc_path(name: str) -> Path:
    return docs_dir() / name


def resolve(name_or_key: str) -> str | None:
    """Accept either the file name or its short key."""
    raw = str(name_or_key or "").strip()
    if raw in _DOCS:
        return raw
    return _BY_KEY.get(raw.lower())


# 首版把 Agent.md 建成了一份「行为偏好」空模板，人设仍只在打包提示词里，
# 于是页面上看不到小歪、机器人却自称小歪。已经落过盘的装机版不会再走建档
# 分支，必须显式认出这份占位内容并换成真实人设。
_LEGACY_AGENT_PLACEHOLDER_MARKS = (
    "写在这里的内容会进入机器人的系统提示",
    "# 行为偏好",
)


def _is_legacy_agent_placeholder(text: str) -> bool:
    body = (text or "").strip()
    if not body:
        return True
    return all(mark in body for mark in _LEGACY_AGENT_PLACEHOLDER_MARKS)


def read_doc(name_or_key: str) -> str:
    name = resolve(name_or_key)
    if name is None:
        raise ValueError(f"unknown agent document: {name_or_key}")
    path = doc_path(name)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        seed = _seed_for(name)
        if seed:
            write_doc(name, seed)
        return seed
    if name == AGENT_FILENAME and _is_legacy_agent_placeholder(text):
        seed = _seed_for(name)
        if seed.strip():
            write_doc(name, seed)
            return seed
    return text


def write_doc(name_or_key: str, text: str) -> dict[str, Any]:
    name = resolve(name_or_key)
    if name is None:
        raise ValueError(f"unknown agent document: {name_or_key}")
    body = str(text or "")
    if len(body.encode("utf-8")) > MAX_DOC_BYTES:
        raise ValueError(f"{name} exceeds {MAX_DOC_BYTES} bytes")
    docs_dir().mkdir(parents=True, exist_ok=True)
    if body and not body.endswith("\n"):
        body += "\n"
    atomic_write_text(doc_path(name), body)
    return {"name": name, "bytes": len(body.encode("utf-8"))}


def list_docs() -> list[dict[str, Any]]:
    """Metadata for the page's document switcher."""
    labels = {
        AGENT_FILENAME: "人设与行为",
        MEMORY_FILENAME: "长期记忆",
        USER_FILENAME: "对主人的了解",
    }
    rows = []
    for name, key in _DOCS.items():
        path = doc_path(name)
        try:
            size = path.stat().st_size
            exists = True
        except OSError:
            size = 0
            exists = False
        rows.append(
            {
                "name": name,
                "key": key,
                "label": labels[name],
                "bytes": size,
                "exists": exists,
            }
        )
    return rows


def system_prompt() -> str:
    """Agent.md is the system prompt; fall back to the packaged template."""
    text = read_doc(AGENT_FILENAME).strip()
    if text:
        return text
    from deskbot_server.device_data import load_llm_system_prompt

    return (load_llm_system_prompt() or "").strip()


def user_prompt_appendix() -> str:
    """User.md as prompt text, skipping the untouched seed."""
    about_user = read_doc(USER_FILENAME).strip()
    if not about_user or about_user == USER_SEED.strip():
        return ""
    return about_user
