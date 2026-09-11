"""macOS 客户端 WKWebView 的 UI 委托契约。

WKWebView 默认不提供 JS 对话框与文件选择面板：没实现对应委托时，页面上的
confirm() 和 <input type="file"> 都是"点了毫无反应"，而且不报任何错。
两次都真实发生过（2026-09-02 固件更新按钮、2026-09-03 声音复刻选音频）。
控制台确实用到这些能力，所以在这里钉住。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN_SWIFT = ROOT / "client-macos" / "main.swift"
TEMPLATES = ROOT / "src" / "deskbot_server" / "web" / "templates"

_REQUIRED_DELEGATES = (
    "runJavaScriptAlertPanelWithMessage",
    "runJavaScriptConfirmPanelWithMessage",
    "runJavaScriptTextInputPanelWithPrompt",
    "runOpenPanelWith",
)


def test_webview_implements_every_ui_delegate_the_console_uses():
    source = MAIN_SWIFT.read_text(encoding="utf-8")
    assert "WKUIDelegate" in source and "webView.uiDelegate = self" in source
    missing = [name for name in _REQUIRED_DELEGATES if name not in source]
    assert not missing, (
        f"main.swift 缺少 WKUIDelegate 方法 {missing}——"
        "缺哪个，页面上对应的交互就会静默失效（点了没反应）"
    )


def test_file_inputs_still_exist_so_the_open_panel_is_required():
    """反向锁：控制台确实有文件选择，上面的委托不是可选项。"""
    used = [
        p.relative_to(TEMPLATES).as_posix()
        for p in TEMPLATES.rglob("*.html")
        if re.search(r'type="file"', p.read_text(encoding="utf-8"))
    ]
    assert used, "控制台已无文件上传？那就该同步复核 runOpenPanelWith 是否仍必要"
