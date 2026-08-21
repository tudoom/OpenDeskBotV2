from __future__ import annotations


def test_playback_echo_guard_rejects_exact_and_noisy_tts_fragments():
    from deskbot_server.rtc_barge_in import PlaybackEchoGuard

    guard = PlaybackEchoGuard()
    guard.note_tts_segment("reply-1", "今天北京天气晴朗，适合出门。", now=10.0)

    assert guard.classify("北京天气晴朗", now=10.5)[0] is True
    assert guard.classify("北京天气睛朗", now=10.5)[0] is True


def test_playback_echo_guard_allows_real_barge_in_and_explicit_stop():
    from deskbot_server.rtc_barge_in import PlaybackEchoGuard

    guard = PlaybackEchoGuard()
    guard.note_tts_segment("reply-1", "今天北京天气晴朗，适合出门。", now=10.0)

    assert guard.classify("你喜欢吃什么", now=10.5)[0] is False
    assert guard.classify("停一下", now=10.5)[0] is False


def test_playback_echo_guard_only_applies_during_estimated_playback_tail():
    from deskbot_server.rtc_barge_in import PlaybackEchoGuard

    guard = PlaybackEchoGuard()
    guard.note_tts_segment("reply-1", "你好", now=10.0)

    assert guard.is_playback_active(now=10.2) is True
    assert guard.classify("你好", now=20.0)[0] is False


def test_segment_estimates_accumulate_audio_but_add_tail_only_once():
    from deskbot_server.rtc_barge_in import PlaybackEchoGuard

    guard = PlaybackEchoGuard()
    guard.note_tts_segment("reply-1", "hello", now=10.0)
    guard.note_tts_segment("reply-1", "world", now=10.0)

    # Two five-letter segments estimate about 0.91 s of audio. With the
    # 1.2 s acoustic tail the guard is active around t=12.0, but the tail must
    # not be added once per text segment (which would keep it active past 13s).
    assert guard.is_playback_active(now=12.0) is True
    assert guard.is_playback_active(now=12.2) is False


def test_playback_state_uses_one_tail_after_the_real_audio_end():
    from deskbot_server.rtc_barge_in import PlaybackEchoGuard

    guard = PlaybackEchoGuard()
    guard.note_tts_segment("reply-1", "hello", now=10.0)
    guard.set_playback_active(True, now=12.0)
    guard.set_playback_active(False, now=15.0)

    assert guard.is_playback_active(now=16.1) is True
    assert guard.is_playback_active(now=16.3) is False


def test_new_tts_request_replaces_old_echo_context():
    from deskbot_server.rtc_barge_in import PlaybackEchoGuard

    guard = PlaybackEchoGuard()
    guard.note_tts_segment("reply-1", "第一段旧回答", now=10.0)
    guard.note_tts_segment("reply-2", "第二段新回答", now=11.0)

    assert guard.classify("第一段旧回答", now=11.2)[0] is False
    assert guard.classify("第二段新回答", now=11.2)[0] is True


def test_ordinary_barge_in_requires_sustained_audio_but_stop_does_not():
    from deskbot_server.rtc_barge_in import should_confirm_barge_in

    assert should_confirm_barge_in("你喜欢吃什么") is False
    assert should_confirm_barge_in(
        "你喜欢吃什么", speech_seconds=0.5
    ) is False
    assert should_confirm_barge_in(
        "你喜欢吃什么", speech_seconds=1.2
    ) is True
    assert should_confirm_barge_in("停一下", speech_seconds=0.2) is True


def test_three_cjk_characters_confirm_an_ordinary_interruption():
    from deskbot_server.rtc_barge_in import should_confirm_barge_in

    # Chinese has no spaces, so word-count gates never trigger; three CJK
    # characters with matching audio are a deliberate interruption.
    assert should_confirm_barge_in("那不对", speech_seconds=0.7) is True
    assert should_confirm_barge_in("太快了", speech_seconds=0.65) is True
    # Still gated on sustained near-end audio, not text length alone.
    assert should_confirm_barge_in("那不对", speech_seconds=0.3) is False
    assert should_confirm_barge_in("那不对") is False


def test_single_character_fillers_never_interrupt_playback():
    from deskbot_server.rtc_barge_in import should_confirm_barge_in

    assert should_confirm_barge_in("嗯", speech_seconds=2.0) is False
    assert should_confirm_barge_in("啊", speech_seconds=2.0) is False
    assert should_confirm_barge_in("嗯嗯", speech_seconds=2.0) is False


def test_english_interruption_behaviour_is_unchanged():
    from deskbot_server.rtc_barge_in import should_confirm_barge_in

    # Short alphabetic fragments keep the original 4-character floor.
    assert should_confirm_barge_in("hey", speech_seconds=1.2) is False
    # Ordinary English interruptions still need the 0.9 s sustained floor.
    assert should_confirm_barge_in("that is wrong", speech_seconds=0.7) is False
    assert should_confirm_barge_in("that is wrong", speech_seconds=1.0) is True
    # Explicit stop words bypass the duration gate exactly as before.
    assert should_confirm_barge_in("stop", speech_seconds=0.1) is True


def test_sdk_min_interruption_words_stays_at_one_word():
    from pathlib import Path

    source = (
        Path(__file__).parents[1]
        / "src"
        / "deskbot_server"
        / "rtc_livekit_plugins.py"
    ).read_text(encoding="utf-8")
    # LiveKit counts each CJK character as a "word"; anything above 1 would
    # re-introduce the gate that swallowed short Chinese interruptions, and 0
    # would let raw VAD edges cancel TTS before echo rejection.
    assert 'kwargs.setdefault("min_interruption_words", 1)' in source


def test_playback_verbatim_fragment_still_counts_as_echo():
    """AEC 泄漏的逐字片段仍应判回声（响度旁路在 STT 适配器层，见 plugins）。"""
    from deskbot_server.rtc_barge_in import PlaybackEchoGuard

    guard = PlaybackEchoGuard()
    guard.note_tts_segment("reply-1", "我喜欢吃苹果和香蕉哦，特别是脆脆的苹果", now=10.0)
    is_echo, similarity = guard.classify("喜欢吃苹果和香蕉", now=10.5)
    assert is_echo, f"similarity={similarity}"
