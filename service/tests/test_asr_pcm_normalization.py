from array import array

from deskbot_server.asr.pcm_normalization import normalize_quiet_pcm_for_asr


def _pcm(values: list[int]) -> bytes:
    return array("h", values).tobytes()


def test_quiet_voice_is_amplified_without_clipping():
    source = [80 + ((i % 40) - 20) * 70 for i in range(1600)]
    normalized, gain = normalize_quiet_pcm_for_asr(_pcm(source))
    output = array("h")
    output.frombytes(normalized)

    assert gain > 1.25
    assert max(abs(value) for value in output) > max(abs(value - 80) for value in source)
    assert max(output) <= 32767
    assert min(output) >= -32768


def test_normal_level_voice_remains_bit_identical():
    pcm = _pcm([((i % 40) - 20) * 400 for i in range(1600)])
    normalized, gain = normalize_quiet_pcm_for_asr(pcm)

    assert gain == 1.0
    assert normalized == pcm


def test_near_flat_noise_is_not_amplified():
    pcm = _pcm([30 + (i % 3) for i in range(1600)])
    normalized, gain = normalize_quiet_pcm_for_asr(pcm)

    assert gain == 1.0
    assert normalized == pcm


def test_utterance_p95_magnitude_separates_speech_from_residual():
    """近端语音的 P95 幅度显著高于 AEC 残留量级（播放期回声裁决依据）。"""
    import math

    from deskbot_server.asr.pcm_normalization import utterance_p95_magnitude

    def tone(amplitude: int, samples: int = 3200) -> bytes:
        data = bytearray()
        for n in range(samples):
            value = int(amplitude * math.sin(2 * math.pi * 220 * n / 16000))
            data += value.to_bytes(2, "little", signed=True)
        return bytes(data)

    assert utterance_p95_magnitude(tone(2000)) > 1500  # 正常语音量级
    assert utterance_p95_magnitude(tone(150)) < 200    # 残留/静息量级
    assert utterance_p95_magnitude(b"\x00\x00" * 10) == 0  # 过短样本
