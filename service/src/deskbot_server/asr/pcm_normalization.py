"""Conservative utterance-level PCM normalization for speech recognition."""

from __future__ import annotations

from array import array


def utterance_p95_magnitude(pcm: bytes) -> int:
    """Return the DC-removed P95 sample magnitude of one S16LE utterance.

    与 ``normalize_quiet_pcm_for_asr`` 同口径的响度度量，供播放期回声判定
    区分“真实近端语音（响）”与“AEC 残留回声（弱）”。
    """

    even_len = len(pcm) - len(pcm) % 2
    if even_len < 320:
        return 0
    samples = array("h")
    samples.frombytes(pcm[:even_len])
    if not samples:
        return 0
    mean = sum(samples) / len(samples)
    magnitudes = sorted(abs(int(value - mean)) for value in samples)
    return magnitudes[min(len(magnitudes) - 1, int(len(magnitudes) * 0.95))]


def normalize_quiet_pcm_for_asr(pcm: bytes) -> tuple[bytes, float]:
    """Raise unusually quiet S16LE speech without amplifying flat noise.

    The V2 PDM microphone has substantially less digital gain than the XIAO
    board.  Work on a complete utterance so the chosen gain remains constant
    across every sample and cannot pump between real-time transport frames.
    """

    even_len = len(pcm) - len(pcm) % 2
    if even_len < 320:
        return pcm, 1.0
    samples = array("h")
    samples.frombytes(pcm[:even_len])
    if not samples:
        return pcm, 1.0
    mean = sum(samples) / len(samples)
    magnitudes = sorted(abs(int(value - mean)) for value in samples)
    p95 = magnitudes[min(len(magnitudes) - 1, int(len(magnitudes) * 0.95))]
    peak = magnitudes[-1]
    if p95 >= 2800 or peak >= 12000 or peak < 180:
        return pcm, 1.0
    gain = min(8.0, 5000.0 / max(1.0, float(p95)), 30000.0 / peak)
    if gain < 1.25:
        return pcm, 1.0
    for index, value in enumerate(samples):
        amplified = int(round((value - mean) * gain))
        samples[index] = max(-32768, min(32767, amplified))
    return samples.tobytes() + pcm[even_len:], gain
