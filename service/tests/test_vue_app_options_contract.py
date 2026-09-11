"""控制台模板的 Vue 选项对象契约。

2026-09-03 回归：给首页加「最近事件」面板时，三个方法被插到了 ``methods`` 的
闭合括号之后，成了 ``createApp({...})`` 的顶层选项。JS 语法完全合法、node 解析
也通过，但 ``this.pollTelemetry`` 变成 undefined，``mounted()`` 第一行就抛异常，
于是 ``load()`` 与设备状态订阅全部没执行——首页三项卡在「状态未知」。

这个测试只做一件事：``createApp({...})`` 的顶层键必须都是 Vue 认识的选项名。
任何方法/计算属性掉到外面都会立刻红。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

TEMPLATES = (Path(__file__).resolve().parents[1] / "src/deskbot_server/web/templates").rglob("*.html")

# Vue 3 选项式 API 里我们实际会用到的顶层键
_ALLOWED_OPTIONS = {
    "beforeCreate", "beforeMount", "beforeUnmount", "beforeUpdate", "components",
    "computed", "created", "data", "delimiters", "directives", "emits", "errorCaptured",
    "expose", "inject", "methods", "mounted", "props", "provide", "setup", "unmounted",
    "updated", "watch",
}


def _script_bodies(html: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r"<script>([\s\S]*?)</script>", html)]


def _createapp_object(js: str) -> str | None:
    """取出 createApp({ ... }) 的对象字面量正文（含首尾大括号内的内容）。"""
    start = js.find("createApp({")
    if start < 0:
        return None
    open_at = js.index("{", start)
    depth = 0
    in_str: str | None = None
    escaped = False
    for i in range(open_at, len(js)):
        ch = js[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_str:
                in_str = None
            continue
        if ch in "\"'`":
            in_str = ch
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return js[open_at + 1 : i]
    return None


def _top_level_keys(body: str) -> list[str]:
    """对象正文里深度为 0 的键名。

    必须同时认出 ``name:`` 与方法简写 ``name(){}``——放错位置的正是后者，
    早先版本在遇到 ``(`` 时先加深度再清缓冲，于是方法简写一个也认不出来。
    """
    keys: list[str] = []
    depth = 0
    i = 0
    buf: list[str] = []
    n = len(body)
    while i < n:
        ch = body[i]
        # 字符串（含模板串）整体跳过
        if ch in "\"'`":
            quote = ch
            i += 1
            while i < n:
                if body[i] == "\\":
                    i += 2
                    continue
                if body[i] == quote:
                    break
                i += 1
            i += 1
            continue
        # 注释整体跳过
        if ch == "/" and i + 1 < n and body[i + 1] == "/":
            i = body.find("\n", i)
            if i < 0:
                break
            continue
        if ch == "/" and i + 1 < n and body[i + 1] == "*":
            j = body.find("*/", i + 2)
            i = (j + 2) if j >= 0 else n
            continue
        if depth == 0 and ch in ":(":
            text = "".join(buf).strip()
            m = re.fullmatch(r"(?:async\s+)?(\w+)", text)
            if m:
                keys.append(m.group(1))
            buf.clear()
        if ch in "{[(":
            depth += 1
            buf.clear()
        elif ch in "}])":
            depth -= 1
            buf.clear()
        elif depth == 0:
            if ch in ",\n":
                buf.clear()
            else:
                buf.append(ch)
        i += 1
    return keys


@pytest.mark.parametrize("path", sorted(TEMPLATES), ids=lambda p: p.name)
def test_createapp_top_level_keys_are_vue_options(path: Path):
    html = path.read_text(encoding="utf-8")
    for js in _script_bodies(html):
        body = _createapp_object(js)
        if body is None:
            continue
        stray = [k for k in _top_level_keys(body) if k not in _ALLOWED_OPTIONS]
        assert not stray, (
            f"{path.name}: createApp 顶层出现非 Vue 选项 {stray}——"
            "多半是方法漏在了 methods:{...} 的闭合括号之外，"
            "运行时会变成 this.X is not a function"
        )
