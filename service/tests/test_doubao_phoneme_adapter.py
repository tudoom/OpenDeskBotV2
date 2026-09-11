from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

from deskbot_server.core.ports.tts import PhonemeSegment
from deskbot_server.infrastructure.tts import doubao_phoneme


def test_phoneme_segmentation_runs_off_the_asyncio_thread(monkeypatch):
    event_loop_thread = threading.get_ident()
    cloud_threads: list[int] = []
    segmentation_threads: list[int] = []
    pcm = b"\x01\x02" * 240

    async def fake_synthesize(text, config):
        cloud_threads.append(threading.get_ident())
        assert text == "hello"
        assert config.sample_rate == 24000
        return SimpleNamespace(
            pcm=pcm,
            sample_rate=24000,
            sentence_end=None,
            subtitles=[],
            elapsed_ms=1,
        )

    def fake_build_segments(**kwargs):
        segmentation_threads.append(threading.get_ident())
        assert kwargs == {
            "text": "hello",
            "pcm": pcm,
            "sample_rate": 24000,
            "sentence_end": None,
            "subtitles": [],
        }
        return [PhonemeSegment(phoneme="h", ms=10, pcm=pcm)]

    monkeypatch.setattr(
        doubao_phoneme,
        "load_doubao_tts_config",
        lambda **_kwargs: SimpleNamespace(sample_rate=24000),
    )
    monkeypatch.setattr(doubao_phoneme, "synthesize_doubao_tts", fake_synthesize)
    monkeypatch.setattr(
        doubao_phoneme,
        "build_phoneme_segments",
        fake_build_segments,
    )

    adapter = doubao_phoneme.DoubaoPhonemeTtsAdapter(
        SimpleNamespace(tts=SimpleNamespace(sample_rate=24000))
    )
    sample_rate, segments = asyncio.run(
        adapter.synthesize_phoneme_segments("hello")
    )

    assert sample_rate == 24000
    assert segments == [PhonemeSegment(phoneme="h", ms=10, pcm=pcm)]
    assert cloud_threads == [event_loop_thread]
    assert len(segmentation_threads) == 1
    assert segmentation_threads[0] != event_loop_thread
