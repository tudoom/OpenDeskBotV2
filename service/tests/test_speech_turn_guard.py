"""「叫我小朋友」被截成「叫我小」的三层兜底：断句判半句、称呼写入前确认、旧称呼作废。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_incomplete_tail_detection():
    from deskbot_server.application.speech_turn import looks_incomplete

    assert looks_incomplete("叫我小")
    assert looks_incomplete("我叫")
    assert looks_incomplete("你可以叫我")
    assert looks_incomplete("我今天有点累，然后")
    assert looks_incomplete("帮我把")
    # 完整的句子不误判
    assert not looks_incomplete("叫我小朋友")
    assert not looks_incomplete("有点累。")
    assert not looks_incomplete("好的")
    assert not looks_incomplete("是")
    assert not looks_incomplete("")


def test_turn_detector_holds_the_turn_for_half_sentences():
    from deskbot_server.application.speech_turn import DeskbotTurnDetector

    class _Msg:
        def __init__(self, role, text):
            self.role = role
            self.text_content = text

    class _Ctx:
        def __init__(self, items):
            self.items = items

    det = DeskbotTurnDetector()
    half = _Ctx([_Msg("assistant", "我该怎么称呼你呀？"), _Msg("user", "叫我小")])
    full = _Ctx([_Msg("assistant", "我该怎么称呼你呀？"), _Msg("user", "叫我小朋友。")])
    threshold = asyncio.run(det.unlikely_threshold("zh"))
    assert asyncio.run(det.predict_end_of_turn(half)) < threshold
    assert asyncio.run(det.predict_end_of_turn(full)) > threshold
    assert asyncio.run(det.supports_language("zh")) and asyncio.run(det.supports_language(None))
    assert asyncio.run(det.predict_end_of_turn(_Ctx([]))) > threshold


def test_address_name_extraction_and_suspicion():
    from deskbot_server.application.speech_turn import extract_address_name, name_is_suspicious

    assert extract_address_name("主人希望我称呼他为“小”") == "小"
    assert extract_address_name("主人希望我称呼他为小鹏") == "小鹏"
    assert extract_address_name("主人明确告诉我，希望我称呼他为豆包") == "豆包"
    assert extract_address_name("主人希望小歪对他的称呼是十三点") == "十三点"
    assert extract_address_name("主人说叫他小朋友就行") == "小朋友"
    assert extract_address_name("主人今天有点累") is None
    assert name_is_suspicious("小") and name_is_suspicious("") and name_is_suspicious("那个")
    assert not name_is_suspicious("小朋友") and not name_is_suspicious("豆包")


def test_memory_add_refuses_truncated_name_and_supersedes_old_names(monkeypatch):
    from deskbot_server.application import llm_tool_runner as runner

    store: list[dict] = [
        {"id": "a1", "text": "主人希望我称呼他为豆包"},
        {"id": "a2", "text": "主人喜欢猫"},
    ]

    def _add(text):
        entry = {"id": f"n{len(store)}", "text": text}
        store.append(entry)
        return entry

    def _delete(eid):
        for i, e in enumerate(store):
            if e["id"] == eid:
                store.pop(i)
                return True
        return False

    monkeypatch.setattr(runner, "add_memory", _add)
    monkeypatch.setattr(runner, "list_memory_entries", lambda **_kw: list(store))
    monkeypatch.setattr(runner, "delete_memory", _delete)

    def _run(text):
        try:
            res = runner.execute_llm_tools([{"tool": "memory_add", "text": text}], device_id="dev")
        except ValueError as exc:
            return {"tool": "memory_add", "ok": False, "error": str(exc)}
        return next(r for r in res if r.get("tool") == "memory_add")

    bad = _run("主人希望我称呼他为“小”")
    assert not bad["ok"] and "不完整" in str(bad.get("error") or bad)
    assert all("“小”" not in e["text"] for e in store)

    good = _run("主人希望我称呼他为“小朋友”")
    assert good["ok"] and good.get("superseded") == ["a1"]
    assert [e["text"] for e in store] == ["主人喜欢猫", "主人希望我称呼他为“小朋友”"]


def test_quest_result_guard_blocks_one_char_name():
    from deskbot_server.application import quest_service

    with pytest.raises(quest_service.QuestError, match="不完整"):
        quest_service.execute_quest_tool(
            {"tool": "update_task_result", "task_id": "g_learn_name", "status": "success", "result": "主人明确告知称呼为“小”，已记录"},
            device_id="dev",
        )


def test_endpointing_and_playbook_carry_the_fix():
    sdk = (ROOT / "service/src/deskbot_server/rtc_agent_sdk.py").read_text(encoding="utf-8")
    plugins = (ROOT / "service/src/deskbot_server/rtc_livekit_plugins.py").read_text(encoding="utf-8")
    for src in (sdk, plugins):
        assert 'kwargs.setdefault("min_endpointing_delay", 0.35)' in src
        assert 'kwargs.setdefault("max_endpointing_delay", 1.5)' in src
        assert 'kwargs.setdefault("turn_detection", DeskbotTurnDetector())' in src
    assert 'kwargs.setdefault("min_silence_duration", 0.55)' in sdk
    # rtc_agent_sdk 的会话补丁运行在 worker 进程的 sitecustomize 字符串模板里：
    # 名字必须在模板内部 import，并且失败不能让会话起不来（2026-09-09 事故）。
    from deskbot_server import rtc_agent_sdk

    template = rtc_agent_sdk._SITECUSTOMIZE_CODE  # noqa: SLF001
    assert "from deskbot_server.application.speech_turn import DeskbotTurnDetector" in template
    assert template.index("from deskbot_server.application.speech_turn import DeskbotTurnDetector") < template.index(
        'kwargs.setdefault("turn_detection", DeskbotTurnDetector())'
    )
    assert "turn detector unavailable" in template
    # 模板本身必须能编译，且函数体内引用的全局名都在模板里有定义（防再犯 NameError）
    import ast
    import builtins

    tree = ast.parse(template)
    defined = set(dir(builtins))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                defined.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
            for arg in getattr(getattr(node, "args", None), "args", []) or []:
                defined.add(arg.arg)
            if getattr(node, "args", None):
                for extra in (node.args.vararg, node.args.kwarg):
                    if extra:
                        defined.add(extra.arg)
                for arg in node.args.kwonlyargs:
                    defined.add(arg.arg)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            defined.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            defined.add(node.name)
        elif isinstance(node, ast.comprehension):
            for n in ast.walk(node.target):
                if isinstance(n, ast.Name):
                    defined.add(n.id)
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    assert not (used - defined), f"sitecustomize 模板里有未定义的名字: {sorted(used - defined)}"
    tools = (ROOT / "service/src/deskbot_server/rtc_worker_tools.py").read_text(encoding="utf-8")
    assert "truncated transcript" in tools
    import json

    playbook = json.loads((ROOT / "service/src/deskbot_server/quest_default_playbook.json").read_text(encoding="utf-8"))
    assert playbook["template_version"] >= 8
    learn = next(t for t in playbook["tasks"] if t["id"] == "g_learn_name")
    assert "截断" in learn["strategy"]
