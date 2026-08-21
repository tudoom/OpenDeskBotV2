"""Microphone acoustic-signal health detection for decoded PCM uplink.

The transport can be perfectly healthy while a disconnected, obstructed, or
failed microphone produces an almost-flat PCM stream.  This monitor deliberately
counts *received audio duration* instead of wall time, so a muted/TTS playback
period or a disconnected USB cable cannot create a false microphone alarm.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

import numpy as np

MIC_HEALTH_CHECKING = "checking"
MIC_HEALTH_OK = "ok"
MIC_NO_ACOUSTIC_SIGNAL = "mic_no_acoustic_signal"
MIC_HEALTH_STATES = frozenset(
    {
        MIC_HEALTH_CHECKING,
        MIC_HEALTH_OK,
        MIC_NO_ACOUSTIC_SIGNAL,
    }
)


@dataclass(frozen=True)
class MicrophoneHealthConfig:
    """Thresholds for post-firmware-gain, signed 16-bit mono PCM.

    Both energy and robust dynamic range must be extremely low before a frame
    counts as flat.  Requiring a nearly-flat 20 second rolling window avoids
    treating ordinary pauses as hardware failures.  Recovery uses a shorter
    rolling window so speaking into a repaired/reattached microphone clears the
    warning promptly without allowing a single electrical impulse to do so.
    """

    analysis_frame_ms: int = 100
    flat_window_ms: int = 20_000
    flat_ratio: float = 0.98
    flat_ac_rms_max: float = 96.0
    flat_variation_max: float = 256.0
    recovery_window_ms: int = 3_000
    recovery_active_ms: int = 600


@dataclass(frozen=True)
class _WindowFrame:
    duration_ms: float
    flat: bool


class MicrophoneHealthMonitor:
    """Classify a continuous decoded PCM stream and emit state transitions."""

    def __init__(
        self,
        config: MicrophoneHealthConfig | None = None,
        *,
        sample_rate: int = 16_000,
    ) -> None:
        self.config = config or MicrophoneHealthConfig()
        self._status = MIC_HEALTH_CHECKING
        self._sample_rate = 0
        self._analysis_bytes = 0
        self._pending_pcm = bytearray()
        self._window: deque[_WindowFrame] = deque()
        self._window_ms = 0.0
        self._flat_ms = 0.0
        self._observed_audio_ms = 0.0
        self._frame_count = 0
        self._last_ac_rms = 0.0
        self._last_variation = 0.0
        self._updated_at = time.time()
        self._pending_update: dict[str, object] | None = None
        self._set_sample_rate(sample_rate)

    @property
    def status(self) -> str:
        return self._status

    def _set_sample_rate(self, sample_rate: int) -> None:
        rate = int(sample_rate)
        if rate <= 0:
            raise ValueError("sample_rate must be positive")
        self._sample_rate = rate
        samples = max(
            1,
            round(rate * max(10, int(self.config.analysis_frame_ms)) / 1000),
        )
        self._analysis_bytes = samples * 2

    def reset(self, *, sample_rate: int | None = None) -> None:
        """Start a fresh assessment after a decoded PCM format change."""

        previous = self._status
        if sample_rate is not None:
            self._set_sample_rate(sample_rate)
        self._status = MIC_HEALTH_CHECKING
        self._pending_pcm.clear()
        self._window.clear()
        self._window_ms = 0.0
        self._flat_ms = 0.0
        self._observed_audio_ms = 0.0
        self._frame_count = 0
        self._last_ac_rms = 0.0
        self._last_variation = 0.0
        self._updated_at = time.time()
        if previous != MIC_HEALTH_CHECKING:
            self._pending_update = self.snapshot()

    @staticmethod
    def _metrics(chunk: bytes) -> tuple[float, float]:
        samples = np.frombuffer(chunk, dtype="<i2").astype(np.float32)
        if samples.size == 0:
            return 0.0, 0.0
        centered = samples - float(samples.mean())
        ac_rms = float(np.sqrt(np.mean(centered * centered)))
        # A 5th-to-95th percentile range ignores isolated USB/codec impulses
        # while still measuring short-time acoustic modulation.
        low, high = np.percentile(centered, (5.0, 95.0))
        variation = float(high - low)
        return ac_rms, variation

    def _append_window(self, frame: _WindowFrame) -> None:
        self._window.append(frame)
        self._window_ms += frame.duration_ms
        if frame.flat:
            self._flat_ms += frame.duration_ms

        cap_ms = max(
            float(self.config.flat_window_ms),
            float(self.config.recovery_window_ms),
        )
        while self._window and self._window_ms > cap_ms:
            overflow = self._window_ms - cap_ms
            first = self._window[0]
            if first.duration_ms <= overflow + 1e-6:
                self._window.popleft()
                self._window_ms -= first.duration_ms
                if first.flat:
                    self._flat_ms -= first.duration_ms
                continue
            kept = first.duration_ms - overflow
            self._window[0] = _WindowFrame(kept, first.flat)
            self._window_ms -= overflow
            if first.flat:
                self._flat_ms -= overflow
            break

    def _recent_active_ms(self) -> float:
        remaining = float(self.config.recovery_window_ms)
        active_ms = 0.0
        for frame in reversed(self._window):
            if remaining <= 0:
                break
            take = min(remaining, frame.duration_ms)
            if not frame.flat:
                active_ms += take
            remaining -= take
        return active_ms

    def _set_status(self, status: str) -> None:
        if status == self._status:
            return
        self._status = status
        self._updated_at = time.time()
        self._pending_update = self.snapshot()

    def _evaluate(self) -> None:
        recent_active_ms = self._recent_active_ms()
        if (
            self._status in {MIC_HEALTH_CHECKING, MIC_NO_ACOUSTIC_SIGNAL}
            and recent_active_ms >= float(self.config.recovery_active_ms)
        ):
            self._set_status(MIC_HEALTH_OK)
            return

        enough_audio = self._window_ms >= float(self.config.flat_window_ms) - 1e-6
        flat_ratio = self._flat_ms / self._window_ms if self._window_ms else 0.0
        if enough_audio and flat_ratio >= float(self.config.flat_ratio):
            self._set_status(MIC_NO_ACOUSTIC_SIGNAL)

    def feed_pcm(self, pcm: bytes, *, sample_rate: int | None = None) -> None:
        """Consume little-endian signed 16-bit mono PCM."""

        if sample_rate is not None and int(sample_rate) != self._sample_rate:
            self.reset(sample_rate=int(sample_rate))
        if not pcm:
            return
        even_len = len(pcm) - (len(pcm) % 2)
        if even_len <= 0:
            return
        self._pending_pcm.extend(pcm[:even_len])
        while len(self._pending_pcm) >= self._analysis_bytes:
            chunk = bytes(self._pending_pcm[: self._analysis_bytes])
            del self._pending_pcm[: self._analysis_bytes]
            ac_rms, variation = self._metrics(chunk)
            duration_ms = (
                len(chunk) // 2 * 1000.0 / max(1, self._sample_rate)
            )
            flat = (
                ac_rms <= float(self.config.flat_ac_rms_max)
                and variation <= float(self.config.flat_variation_max)
            )
            self._last_ac_rms = ac_rms
            self._last_variation = variation
            self._observed_audio_ms += duration_ms
            self._frame_count += 1
            self._updated_at = time.time()
            self._append_window(_WindowFrame(duration_ms, flat))
            self._evaluate()

    def snapshot(self) -> dict[str, object]:
        return {
            "status": self._status,
            "observed_audio_ms": int(round(self._observed_audio_ms)),
            "window_audio_ms": int(round(self._window_ms)),
            "ac_rms": round(self._last_ac_rms, 2),
            "short_term_variation": round(self._last_variation, 2),
            "frame_count": self._frame_count,
            "updated_at": self._updated_at,
        }

    def consume_update(self) -> dict[str, object] | None:
        """Return one state transition once, then clear it."""

        update = self._pending_update
        self._pending_update = None
        return dict(update) if update is not None else None


__all__ = [
    "MIC_HEALTH_CHECKING",
    "MIC_HEALTH_OK",
    "MIC_HEALTH_STATES",
    "MIC_NO_ACOUSTIC_SIGNAL",
    "MicrophoneHealthConfig",
    "MicrophoneHealthMonitor",
]
