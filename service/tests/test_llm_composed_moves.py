"""文字链路的自由编排动作（moves 里的 steps）。

此前文字对话只能选预设：模型遇到"傻笑"这类没有预设的动作，就只能回一句
"我没有专门的傻笑预设动作"。自由编排本来只有语音的 move_head 工具有——
同一个能力两条链路答案不同，正是要消灭的形态。下游 expand_llm_moves 早已
支持 __custom__ 并按本机限位钳制，这里把入口打开。
"""

from __future__ import annotations

from deskbot_server.llm.utils import (
    llm_pb_moves_prompt_appendix,
    parse_llm_reply,
)
from deskbot_server.servo_protocol import SERVO_MIN_SEGMENT_DURATION_MS


def _moves(payload: str):
    return parse_llm_reply(payload)["moves"]


def test_composed_steps_expand_to_custom_protocol_items():
    out = _moves(
        '{"tts":"看我摇头","moves":[{"steps":['
        '{"x":60,"y":90,"ms":300},{"x":120,"y":90,"ms":300},{"x":90,"y":90,"ms":200}]}]}'
    )
    assert [m["move"] for m in out] == ["__custom__"] * 3
    assert [m["x"] for m in out] == [60, 120, 90]
    assert [m["ms"] for m in out] == [300, 300, 200]
    # 协议层需要的字段齐全（expand_llm_moves 会据此钳制）
    assert all({"xm", "ym", "x", "y", "ms"} <= set(m) for m in out)


def test_presets_and_composed_steps_can_mix():
    out = _moves(
        '{"tts":"先点头再自己编","moves":[{"move":"nod_head","ms":800},'
        '{"steps":[{"x":70,"y":100,"ms":250}]}]}'
    )
    assert [m["move"] for m in out] == ["nod_head", "__custom__"]


def test_invalid_composed_steps_are_rejected_whole():
    # 步数超限
    many = ",".join(['{"x":90,"y":90,"ms":100}'] * 6)
    assert _moves('{"tts":"x","moves":[{"steps":[' + many + "]}]}") == []
    # 单步时长低于协议下限
    assert _moves(
        '{"tts":"x","moves":[{"steps":[{"x":90,"y":90,"ms":%d}]}]}'
        % (SERVO_MIN_SEGMENT_DURATION_MS - 1)
    ) == []
    # 角度不是整数
    assert _moves('{"tts":"x","moves":[{"steps":[{"x":"左","y":90,"ms":200}]}]}') == []
    # 空步骤
    assert _moves('{"tts":"x","moves":[{"steps":[]}]}') == []


def test_prompt_documents_composition_and_forbids_empty_promises():
    text = llm_pb_moves_prompt_appendix()
    assert "自由编排" in text and '"steps"' in text
    assert "自己编" in text
    # 说了要做动作就必须真的填 moves——用户遇到的正是"嘴上说晃脑袋、实际没动"
    assert "真的写出来" in text
