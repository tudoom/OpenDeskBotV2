"""表情页「生图 API 配置」：Key 来源、接口地址、模型只是配置入口，生图逻辑不变。"""

from __future__ import annotations

import base64
import io
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_KEYS = ("ARK_IMAGE_GEN_API_KEY", "ARK_IMAGE_GEN_URL", "ARK_IMAGE_GEN_MODEL", "LLM_API_KEY")


def _jpeg_b64(color=(0, 255, 255)) -> str:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(buf, format="JPEG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


@pytest.fixture()
def clean_env(monkeypatch):
    for key in _KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture()
def env_file(tmp_path, monkeypatch, clean_env):
    from deskbot_server import env as dotenv_module

    path = tmp_path / ".env"
    monkeypatch.setattr(dotenv_module, "ENV_FILE", path)
    monkeypatch.setattr(dotenv_module, "_last_signature", None)
    monkeypatch.setattr(dotenv_module, "_file_managed_values", {})
    return path


def _capture_transport(captured):
    def transport(url, payload, api_key, timeout):
        captured.update(url=url, model=payload["model"], api_key=api_key)
        return {"data": [{"b64_json": _jpeg_b64()}]}

    return transport


def test_generator_prefers_own_key_then_llm_key(monkeypatch, clean_env):
    from deskbot_server.ark_image_gen import (
        ARK_IMAGES_URL,
        DEFAULT_IMAGE_GEN_MODEL,
        generate_cartoon_frames,
    )

    captured: dict = {}
    monkeypatch.setenv("LLM_API_KEY", "llm-key")
    generate_cartoon_frames("开心", frames=1, transport=_capture_transport(captured))
    assert captured == {"url": ARK_IMAGES_URL, "model": DEFAULT_IMAGE_GEN_MODEL, "api_key": "llm-key"}

    monkeypatch.setenv("ARK_IMAGE_GEN_API_KEY", "ark-key")
    generate_cartoon_frames("开心", frames=1, transport=_capture_transport(captured))
    assert captured["api_key"] == "ark-key"

    # 显式入参仍然最高优先（内部调用/测试用）
    generate_cartoon_frames("开心", frames=1, api_key="explicit", transport=_capture_transport(captured))
    assert captured["api_key"] == "explicit"


def test_generator_reads_url_and_model_from_env(monkeypatch, clean_env):
    from deskbot_server.ark_image_gen import generate_cartoon_frames

    captured: dict = {}
    monkeypatch.setenv("LLM_API_KEY", "llm-key")
    monkeypatch.setenv("ARK_IMAGE_GEN_URL", "https://ark.example.com/api/v3/images/generations")
    monkeypatch.setenv("ARK_IMAGE_GEN_MODEL", "doubao-seedream-5-0-260128")
    generate_cartoon_frames("开心", frames=1, transport=_capture_transport(captured))
    assert captured["url"] == "https://ark.example.com/api/v3/images/generations"
    assert captured["model"] == "doubao-seedream-5-0-260128"


def test_generator_without_any_key_points_user_to_the_config_entry(clean_env):
    from deskbot_server.ark_image_gen import generate_cartoon_frames

    with pytest.raises(ValueError, match="生图 API 配置"):
        generate_cartoon_frames("开心", frames=1, transport=_capture_transport({}))


def test_status_reports_key_source_and_defaults(monkeypatch, env_file):
    from deskbot_server.ark_image_gen import ARK_IMAGES_URL, DEFAULT_IMAGE_GEN_MODEL
    from deskbot_server.image_gen_env_store import image_gen_config_status

    none = image_gen_config_status()
    assert none["api_key_set"] is False and none["api_key_source"] == "none"
    assert none["url"] == ARK_IMAGES_URL and not none["url_overridden"]
    assert none["model"] == DEFAULT_IMAGE_GEN_MODEL and not none["model_overridden"]
    monkeypatch.setenv("LLM_API_KEY", "llm-key")
    assert image_gen_config_status()["api_key_source"] == "llm"
    monkeypatch.setenv("ARK_IMAGE_GEN_API_KEY", "ark-key")
    status = image_gen_config_status()
    assert status["api_key_source"] == "own" and status["api_key_set"] is True
    assert "ark-key" not in str(status)  # Key 本身绝不回传
    # 模型下拉：默认 4.0 与实测过的 5.0；.env 手填的陌生模型也要出现在下拉里
    ids = [m["id"] for m in status["models"]]
    assert ids == ["doubao-seedream-4-0-250828", "doubao-seedream-5-0-260128"]
    assert status["console_url"].startswith("https://console.volcengine.com/ark/")
    monkeypatch.setenv("ARK_IMAGE_GEN_MODEL", "doubao-seedream-9-9-999999")
    assert [m["id"] for m in image_gen_config_status()["models"]][-1] == "doubao-seedream-9-9-999999"


def test_save_writes_env_keeps_masked_key_and_clears(monkeypatch, env_file):
    from deskbot_server.image_gen_env_store import save_image_gen_env

    status = save_image_gen_env({
        "api_key": "ark-key", "url": "https://ark.cn-beijing.volces.com/api/v3/images/generations",
        "model": "doubao-seedream-5-0-260128",
    })
    text = env_file.read_text(encoding="utf-8")
    assert "# 卡通表情生图（火山方舟 Seedream）" in text
    assert "ARK_IMAGE_GEN_API_KEY=ark-key" in text and "ARK_IMAGE_GEN_MODEL=doubao-seedream-5-0-260128" in text
    assert status["api_key_source"] == "own" and status["model_overridden"] and status["url_overridden"]
    assert os.environ["ARK_IMAGE_GEN_API_KEY"] == "ark-key"  # 运行中的进程立即生效

    # 掩码 / 空 Key 不覆盖；地址、模型留空 = 用默认（显式写 KEY=）
    status = save_image_gen_env({"api_key": "••••", "url": "", "model": ""})
    text = env_file.read_text(encoding="utf-8")
    assert "ARK_IMAGE_GEN_API_KEY=ark-key" in text
    assert "ARK_IMAGE_GEN_URL=\n" in text and "ARK_IMAGE_GEN_MODEL=\n" in text
    assert status["api_key_source"] == "own" and not status["model_overridden"] and not status["url_overridden"]

    # 改回复用大模型 Key
    monkeypatch.setenv("LLM_API_KEY", "llm-key")
    status = save_image_gen_env({"clear_api_key": True})
    assert "ARK_IMAGE_GEN_API_KEY=\n" in env_file.read_text(encoding="utf-8")
    assert status["api_key_source"] == "llm"


def test_save_rejects_private_or_plain_http_url(env_file):
    from deskbot_server.image_gen_env_store import save_image_gen_env

    with pytest.raises(ValueError):
        save_image_gen_env({"url": "http://127.0.0.1:8000/images"})
    with pytest.raises(ValueError):
        save_image_gen_env({"url": "ftp://ark.example.com/x"})
    assert not env_file.exists()


def test_env_keys_are_registered_and_documented():
    from deskbot_server import config_registry as cr
    from deskbot_server.image_gen_env_store import IMAGE_GEN_ENV_KEYS

    for key in IMAGE_GEN_ENV_KEYS:
        assert key in cr.ENV_KEYS, key
    assert cr.ENV_KEYS["ARK_IMAGE_GEN_API_KEY"][1] is True  # 是密钥，日志/设置页要打码
    example = (ROOT / "service/.env.example").read_text(encoding="utf-8")
    for key in IMAGE_GEN_ENV_KEYS:
        assert f"# {key}=" in example, key


def test_expr_page_carries_the_config_entry():
    html = (ROOT / "service/src/deskbot_server/web/templates/app2c/expr.html").read_text(encoding="utf-8")
    assert "生图 API 配置" in html
    assert "/api/cartoon_face/config" in html
    assert "clear_api_key" in html
    assert "cartoonApi.api_key" in html and 'type="password"' in html
    # 地址默认填好、模型只能从下拉选、文案是带链接的「火山 Seedream」
    assert 'v-model="cartoonApi.model"' in html and '<select class="tinput" v-model="cartoonApi.model"' in html
    assert "a.url=d.url||a.default_url" in html
    assert 'rel="noopener">火山 Seedream</a>' in html and "console.volcengine.com/ark" in html
    assert "火山seedance" not in html.lower().replace(" ", "")


# 路由：GET 读状态、PATCH 写入、非法地址 400；页面带着入口。
from tests.test_app2c_pages import local_client  # noqa: E402,F401 - pytest fixture


def test_config_routes_read_write_and_reject_bad_url(local_client, env_file):
    client, _app = local_client
    r = client.get("/api/cartoon_face/config")
    assert r.status_code == 200 and r.get_json()["api_key_source"] == "none"
    r = client.patch("/api/cartoon_face/config", json={"api_key": "ark-key", "model": "doubao-seedream-5-0-260128"})
    body = r.get_json()
    assert r.status_code == 200 and body["ok"] and body["api_key_source"] == "own" and body["model_overridden"]
    assert "ark-key" not in r.get_data(as_text=True)
    r = client.patch("/api/cartoon_face/config", json={"url": "http://127.0.0.1:1/images"})
    assert r.status_code == 400 and r.get_json()["ok"] is False
    page = client.get("/expr")
    assert page.status_code == 200 and "生图 API 配置" in page.get_data(as_text=True)
