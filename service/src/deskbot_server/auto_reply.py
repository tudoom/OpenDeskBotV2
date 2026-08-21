# 实验台「自动应答」总开关。为 False 时：
# - 设备真实语音：/asr_chat 上行音频不进入 RTC binding（ws/asr_chat.py 跳过
#   publish_usb_pcm），ESP-SR speech_start 也不触发 listening 表情——完全不理会；
# - Web 文本调试：chat_flow.run_chat_turn 跳过 LLM+TTS，不触发任何自动 pb 下发；
# - 定时提醒走 force_voice=True，不受本开关影响（提醒由勿扰时段单独约束）。
# 翻回 True 立即恢复，无需重连。
_asr_voice_auto_reply_enabled = True


def get_asr_voice_auto_reply_enabled() -> bool:
    return _asr_voice_auto_reply_enabled


def set_asr_voice_auto_reply_enabled(enabled: bool) -> None:
    global _asr_voice_auto_reply_enabled
    _asr_voice_auto_reply_enabled = bool(enabled)
