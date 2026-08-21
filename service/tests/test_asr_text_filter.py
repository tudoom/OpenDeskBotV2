from deskbot_server.asr.text_filter import (
    asr_text_without_punctuation,
    is_asr_text_acceptable,
)


def test_strip_punctuation():
    assert asr_text_without_punctuation("啊。") == "啊"
    assert asr_text_without_punctuation("能听见我说话吗？") == "能听见我说话吗"
    assert asr_text_without_punctuation("23。") == "23"


def test_min_len_two_after_strip():
    assert is_asr_text_acceptable("你好", min_len=2)
    assert is_asr_text_acceptable("嗯，好", min_len=2)
    assert not is_asr_text_acceptable("啊。", min_len=2)
    assert not is_asr_text_acceptable("。", min_len=2)


def test_single_character_commands_are_not_silently_dropped():
    for command in ("停", "开", "关", "走", "来", "退", "播", "删", "拍", "看"):
        assert is_asr_text_acceptable(command, min_len=2), command
    assert not is_asr_text_acceptable("啊", min_len=2)
