"""llm_config_store 单元测试。"""
from __future__ import annotations

import json

import pytest

from deskbot_server.device_data import local_data_dir
from deskbot_server.llm_config_store import (
    LLM_MODELS_FILENAME,
    add_llm_model,
    delete_llm_model,
    get_active_llm_model,
    list_llm_models,
    set_active_llm_model,
    update_llm_model,
)


@pytest.fixture
def local_store(tmp_path, monkeypatch):
    monkeypatch.setattr("deskbot_server.device_data.DATA_DIR", tmp_path)
    monkeypatch.setattr("deskbot_server.device_data.LOCAL_DATA_ROOT", tmp_path / "local")
    return tmp_path / "local"


def test_add_list_select_delete(local_store):
    m1 = add_llm_model(
        name="Test Qwen",
        model_name="qwen-flash",
        protocol="openai",
        base_url="https://example.com/v1",
        api_key="sk-test1234567890",
    )
    assert m1["name"] == "Test Qwen"
    assert m1["api_key_set"] is True
    assert "sk-test" not in m1["api_key"]

    models = list_llm_models()
    assert len(models) == 1

    set_active_llm_model(m1["id"])
    active = get_active_llm_model()
    assert active is not None
    assert active.model_name == "qwen-flash"

    updated = update_llm_model(m1["id"], name="Renamed")
    assert updated is not None
    assert updated["name"] == "Renamed"

    set_active_llm_model(None)
    assert get_active_llm_model() is None

    assert delete_llm_model(m1["id"])
    assert list_llm_models() == []


def test_persisted_json_is_under_local_profile(local_store):
    add_llm_model(name="A", model_name="gpt-4o", protocol="openai", api_key="key1")
    path = local_data_dir() / LLM_MODELS_FILENAME
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data["models"], list)
    assert data["models"][0]["model_name"] == "gpt-4o"


def test_model_store_rejects_private_or_plaintext_base_url(local_store):
    for base_url in (
        "http://127.0.0.1:11434/v1",
        "http://169.254.169.254/latest",
        "http://example.com/v1",
    ):
        with pytest.raises(ValueError):
            add_llm_model(
                name="unsafe",
                model_name="unsafe-model",
                protocol="openai",
                base_url=base_url,
                api_key="key",
            )


def test_concurrent_model_adds_do_not_overwrite_each_other(local_store):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=10) as pool:
        created = list(
            pool.map(
                lambda index: add_llm_model(
                    name=f"model-{index}",
                    model_name=f"endpoint-{index}",
                    protocol="openai",
                    api_key=f"key-{index}",
                ),
                range(30),
            )
        )

    stored = list_llm_models()
    assert len(created) == 30
    assert len(stored) == 30
    assert {row["name"] for row in stored} == {
        f"model-{index}" for index in range(30)
    }
