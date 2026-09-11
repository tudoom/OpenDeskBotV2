"""包依赖方向与函数内延迟 import 的棘轮。

方向规则（允许的依赖方向）：
    core → application → infrastructure / ws → web
即：core 不得 import 其它层；ws/infrastructure/application 不得 import web。
既有违例在 ALLOWED_VIOLATIONS 里逐条列出（每条都是待还的债），新增违例直接红。
函数内 ``from deskbot_server...`` 延迟 import 是循环依赖被压下去的痕迹：总量只许降不许升。
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "deskbot_server"

# (文件相对路径, 被引用的层) —— 2026-09-02 基线
ALLOWED_VIOLATIONS = {
    ("application/web_chat_capture.py", "web"),
    ("application/llm_tool_runner.py", "web"),
    ("application/web_search_prefetch.py", "web"),
    ("infrastructure/bootstrap.py", "application"),
    ("infrastructure/serial/integration.py", "application"),
}
DEFERRED_IMPORT_BASELINE = 341


def _layer_of(rel: str) -> str:
    top = rel.split("/", 1)[0]
    return top if top in {"core", "application", "infrastructure", "ws", "web"} else "root"


_FORBIDDEN = {
    "core": {"application", "infrastructure", "ws", "web"},
    "application": {"web"},
    "infrastructure": {"web", "application"},
    "ws": {"web"},
}


def _imports_of(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("deskbot_server."):
            yield node.module.split(".")[1], isinstance(node, ast.ImportFrom) and node.col_offset > 0


def test_dependency_direction_only_regresses_by_explicit_choice():
    violations = set()
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        layer = _layer_of(rel)
        if layer not in _FORBIDDEN:
            continue
        for target, _deferred in _imports_of(path):
            if target in _FORBIDDEN[layer]:
                violations.add((rel, target))
    new = violations - ALLOWED_VIOLATIONS
    assert not new, f"新增分层违例（下层 import 上层）: {sorted(new)}"


def test_deferred_intra_package_imports_do_not_grow():
    total = 0
    for path in SRC.rglob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith((" ", "\t")) and line.lstrip().startswith("from deskbot_server"):
                total += 1
    assert total <= DEFERRED_IMPORT_BASELINE, (
        f"函数内延迟 import 增至 {total}（基线 {DEFERRED_IMPORT_BASELINE}）——"
        "新代码请把 import 放到模块顶部，遇到循环用 core/ports 的接口断开"
    )
