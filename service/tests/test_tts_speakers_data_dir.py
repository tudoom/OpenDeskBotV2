from __future__ import annotations

import json


def test_speaker_table_is_read_from_the_data_dir(tmp_path, monkeypatch):
    """The packaged layout has no data/ next to the sources.

    speakers.py used to locate the table by walking up from __file__, which
    resolves to <service>/data in a source checkout but to <runtime>/app/data
    in the shipped client — and app/ only ever contains bin, models and src,
    because data/global is seeded into the user profile instead. The table was
    therefore always missing once installed and the voice page listed no
    speakers at all, TTS 2.0 included, while source runs looked fine.
    """
    import importlib

    from deskbot_server import paths

    data_dir = tmp_path / "data"
    (data_dir / "global").mkdir(parents=True)
    (data_dir / "global" / "doubao_tts_speakers.json").write_text(
        json.dumps(
            [
                {
                    "label": "测试音色 2.0",
                    "id": "zh_female_test_uranus_bigtts",
                    "scene": "通用",
                    "resource_id": "seed-tts-2.0",
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(paths, "DATA_DIR", data_dir)

    speakers = importlib.reload(importlib.import_module("deskbot_server.tts.speakers"))
    try:
        presets = speakers.list_doubao_tts_speaker_presets()
        ids = {p["id"] for p in presets}
        assert "zh_female_test_uranus_bigtts" in ids
        assert any(p["resource_id"] == "seed-tts-2.0" for p in presets)
    finally:
        importlib.reload(speakers)
