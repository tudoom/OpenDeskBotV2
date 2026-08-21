"""Conservative playback-echo rejection for RTC full-duplex barge-in.

The device AEC is the primary defence.  This module is the semantic safety
net: while recent TTS is still audible, a Seed ASR result that is merely a
fragment of that TTS must not be promoted to a new user turn.
"""

from __future__ import annotations

import threading
import time
import unicodedata
from difflib import SequenceMatcher

BARGE_IN_TOPIC = "deskbot.barge_in"
SPEECH_ACTIVITY_TOPIC = "deskbot.speech_activity"
USER_SPEECH_START_PAYLOAD = b"user_speech_start"
USER_SPEECH_CONFIRMED_END_PAYLOAD = b"user_speech_confirmed_end"
_MAX_PLAYBACK_TEXT_CHARS = 800
_PLAYBACK_TAIL_SECONDS = 1.2
_MIN_SEGMENT_SECONDS = 0.45
_CJK_CHARS_PER_SECOND = 4.2
_OTHER_CHARS_PER_SECOND = 11.0
_EXPLICIT_BARGE_IN = (
    "停一下",
    "等一下",
    "等等",
    "别说了",
    "先停",
    "暂停",
    "打断",
    "stop",
    "wait",
    "pause",
)
_MIN_ORDINARY_BARGE_IN_CHARS = 4
_MIN_ORDINARY_BARGE_IN_SECONDS = 0.90
# Chinese carries roughly one syllable/morpheme per character, so three CJK
# characters are already an intentional utterance ("那不对", "太快了") while a
# lone filler ("嗯", "啊") stays below the gate.  The duration floor scales
# with the shorter text (3 chars ≈ 0.71 s at 4.2 chars/s).
_MIN_CJK_BARGE_IN_CHARS = 3
_MIN_CJK_BARGE_IN_SECONDS = 0.60


def normalize_transcript(text: str) -> str:
    """Return a comparison form that is stable across ASR punctuation/case."""

    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return "".join(char for char in normalized if char.isalnum())


def is_explicit_barge_in(text: str) -> bool:
    """Return whether ``text`` is an unambiguous request to stop speaking."""

    candidate = normalize_transcript(text)
    return bool(
        candidate
        and any(phrase in candidate for phrase in _EXPLICIT_BARGE_IN)
    )


def should_confirm_barge_in(
    text: str,
    *,
    speech_seconds: float | None = None,
) -> bool:
    """Reject tiny ASR fragments while preserving intentional interruption.

    This gate is used only while/recently after the robot is speaking. Short
    utterances such as ``你好`` remain valid normal turns when it is idle.
    """

    candidate = normalize_transcript(text)
    if not candidate:
        return False
    if is_explicit_barge_in(candidate):
        return True
    if speech_seconds is None:
        return False
    # CJK-aware length gate: ``candidate`` has no spaces to split on, so the
    # threshold counts normalized characters.  Three CJK characters are a
    # deliberate Chinese interruption; alphabetic text keeps the original
    # four-character rule so English behaviour is unchanged.
    cjk_chars = sum(
        "㐀" <= char <= "鿿" for char in candidate
    )
    if cjk_chars >= _MIN_CJK_BARGE_IN_CHARS:
        return speech_seconds >= _MIN_CJK_BARGE_IN_SECONDS
    return bool(
        len(candidate) >= _MIN_ORDINARY_BARGE_IN_CHARS
        and speech_seconds >= _MIN_ORDINARY_BARGE_IN_SECONDS
    )


def _estimated_speech_seconds(text: str) -> float:
    cjk = sum("\u3400" <= char <= "\u9fff" for char in text)
    other = sum(char.isalnum() and not ("\u3400" <= char <= "\u9fff") for char in text)
    return max(
        _MIN_SEGMENT_SECONDS,
        cjk / _CJK_CHARS_PER_SECOND + other / _OTHER_CHARS_PER_SECOND,
    )


def _best_window_similarity(needle: str, haystack: str) -> float:
    if not needle or not haystack:
        return 0.0
    if needle in haystack:
        return 1.0
    if len(haystack) <= len(needle) + 2:
        return SequenceMatcher(None, needle, haystack, autojunk=False).ratio()

    best = 0.0
    minimum = max(1, len(needle) - 2)
    maximum = min(len(haystack), len(needle) + 2)
    for width in range(minimum, maximum + 1):
        for offset in range(0, len(haystack) - width + 1):
            ratio = SequenceMatcher(
                None,
                needle,
                haystack[offset : offset + width],
                autojunk=False,
            ).ratio()
            if ratio > best:
                best = ratio
    return best


class PlaybackEchoGuard:
    """Track recent TTS text and classify Seed ASR results during playback."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._request_id = ""
        self._text = ""
        self._audio_until = 0.0
        self._playback_state_bound = False
        self._playback_active = False
        self._playback_tail_until = 0.0

    def reset(self) -> None:
        with self._lock:
            self._request_id = ""
            self._text = ""
            self._audio_until = 0.0
            self._playback_state_bound = False
            self._playback_active = False
            self._playback_tail_until = 0.0

    def note_tts_segment(
        self,
        request_id: str,
        text: str,
        *,
        now: float | None = None,
    ) -> None:
        timestamp = time.monotonic() if now is None else float(now)
        clean = normalize_transcript(text)
        if not clean:
            return
        with self._lock:
            if request_id != self._request_id:
                self._request_id = str(request_id)
                self._text = ""
                self._audio_until = timestamp
            self._text = (self._text + clean)[-_MAX_PLAYBACK_TEXT_CHARS:]
            self._audio_until = (
                max(timestamp, self._audio_until)
                + _estimated_speech_seconds(clean)
            )

    def set_playback_active(
        self,
        active: bool,
        *,
        now: float | None = None,
    ) -> None:
        """Bind the guard to the session's real speaker playback state.

        Text-duration estimates remain a fallback for adapters used outside an
        ``AgentSession``. Once a session state is observed, the actual
        ``speaking`` transition is authoritative and exactly one acoustic tail
        is applied when playback stops.
        """

        timestamp = time.monotonic() if now is None else float(now)
        with self._lock:
            self._playback_state_bound = True
            self._playback_active = bool(active)
            self._playback_tail_until = (
                0.0
                if active or not self._text
                else timestamp + _PLAYBACK_TAIL_SECONDS
            )

    def _is_playback_active_locked(self, timestamp: float) -> bool:
        if not self._text:
            return False
        if self._playback_state_bound:
            return self._playback_active or timestamp <= self._playback_tail_until
        return timestamp <= self._audio_until + _PLAYBACK_TAIL_SECONDS

    def is_playback_active(self, *, now: float | None = None) -> bool:
        timestamp = time.monotonic() if now is None else float(now)
        with self._lock:
            return self._is_playback_active_locked(timestamp)

    def classify(
        self,
        transcript: str,
        *,
        now: float | None = None,
    ) -> tuple[bool, float]:
        """Return ``(is_echo, similarity)`` for a candidate user transcript."""

        candidate = normalize_transcript(transcript)
        if not candidate:
            return False, 0.0
        if is_explicit_barge_in(candidate):
            return False, 0.0

        timestamp = time.monotonic() if now is None else float(now)
        with self._lock:
            if not self._is_playback_active_locked(timestamp):
                return False, 0.0
            playback = self._text

        similarity = _best_window_similarity(candidate, playback)
        # One-character ASR results are too ambiguous to classify as echo.
        # 注意：文本相似度无法区分“AEC 泄漏的回声”与“用户复述了回答里
        # 的词组/机器人复述了用户的问题”，调用方必须结合近端响度做最终
        # 判定（见 rtc_livekit_plugins 的 near-end override）。
        return len(candidate) >= 2 and similarity >= 0.78, similarity


playback_echo_guard = PlaybackEchoGuard()


__all__ = [
    "BARGE_IN_TOPIC",
    "PlaybackEchoGuard",
    "SPEECH_ACTIVITY_TOPIC",
    "USER_SPEECH_CONFIRMED_END_PAYLOAD",
    "USER_SPEECH_START_PAYLOAD",
    "is_explicit_barge_in",
    "normalize_transcript",
    "playback_echo_guard",
    "should_confirm_barge_in",
]
