"""2026-09-14 下午用户反馈的五个问题：
1. 模型配置页保存后报 d.devices 未定义（load() 下标错位）+ 已保存 Key 以掩码展示 + 切换提供方需保存的提示；
2. 声音页换音色后语音对话仍用旧嗓子（流式 TTS 路径没重读配置）；
4. DeepSeek 把工具调用写成 DSML 标记被当成回复念出来；
5. Agent 页中文输入法回车上屏时把半句话发出去。
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "service/src/deskbot_server/web/templates/app2c"


# ---------------- 4) DSML 工具调用 ----------------

DSML = (
    "你是想让我看看现在有几个人吗？我这就瞧一眼。\n"
    "<｜DSML｜calls>\n"
    '<｜DSML｜invoke name="capture_and_describe">\n'
    '<｜DSML｜parameter name="question" string="true">画面里有几个人？他们在做什么？</｜DSML｜parameter>\n'
    "</｜DSML｜invoke>\n"
    "</｜DSML｜calls>"
)


def test_dsml_tool_calls_become_tools_and_leave_the_reply_clean():
    from deskbot_server.llm.utils import extract_dsml_tool_calls, parse_llm_reply

    text, tools = extract_dsml_tool_calls(DSML)
    assert text == "你是想让我看看现在有几个人吗？我这就瞧一眼。"
    assert tools == [{"tool": "capture_and_describe", "question": "画面里有几个人？他们在做什么？"}]
    parsed = parse_llm_reply(DSML)
    assert parsed["reply"] == "你是想让我看看现在有几个人吗？我这就瞧一眼。" and "DSML" not in parsed["reply"]
    assert parsed["tools"] == tools and parsed["contract_ok"]
    # 半角竖线、非字符串参数（JSON）、没有 calls 外壳的截断输出
    raw = '<|DSML|invoke name="websearch"><|DSML|parameter name="max_results">3</|DSML|parameter><|DSML|parameter name="query" string="true">中秋 去哪玩</|DSML|parameter></|DSML|invoke>'
    text, tools = extract_dsml_tool_calls(raw)
    assert text == "" and tools == [{"tool": "websearch", "max_results": 3, "query": "中秋 去哪玩"}]
    only = parse_llm_reply(raw)
    assert only["reply"] == "" and only["tools"] == tools and only["contract_ok"]
    # 正常 JSON 协议不受影响；JSON 后面拖着 DSML 也合并进 tools
    plain = parse_llm_reply('{"need_reply":true,"tts":"好的","tools":[]}')
    assert plain["reply"] == "好的" and plain["tools"] == []
    mixed = parse_llm_reply('{"need_reply":true,"tts":"我查查","tools":[]}\n' + DSML.split("\n", 1)[1])
    assert mixed["reply"] == "我查查" and mixed["tools"][0]["tool"] == "capture_and_describe"
    assert extract_dsml_tool_calls("没有标记的普通回复") == ("没有标记的普通回复", [])


# ---------------- 1) 模型配置页 ----------------


def test_saved_key_masks_hide_the_secret_but_identify_it():
    from deskbot_server.llm.provider_keys import mask_secret, saved_key_masks

    assert mask_secret("sk-1234567890abcdef3f2a") == "sk-…3f2a"
    assert mask_secret("short") == "•••rt" and mask_secret("") == ""
    masks = saved_key_masks({"LLM_API_KEY_DEEPSEEK": "sk-aaaaaaaaaaaaaaaa1111", "LLM_API_KEY_DOUBAO": "ark-bbbbbbbbbbbbbb2222", "LLM_API_KEY_MIMO": "", "OTHER": "x"})
    assert masks == {"DEEPSEEK": "sk-…1111", "DOUBAO": "ark…2222"}
    for value in masks.values():
        assert "aaaa" not in value and "bbbb" not in value


def test_setup_llm_payload_carries_masks(monkeypatch):
    from deskbot_server.web.blueprints import app2c_bp

    monkeypatch.setattr(app2c_bp, "read_env_file", lambda: {"LLM_API_KEY_DEEPSEEK": "sk-1234567890abcdef3f2a"})
    payload = app2c_bp._system_llm_payload()  # noqa: SLF001
    assert payload["saved_key_masks"] == {"DEEPSEEK": "sk-…3f2a"} and payload["saved_key_providers"] == ["DEEPSEEK"]
    assert "api_key_masked" in payload and "1234567890" not in str(payload)


def test_advanced_page_load_indices_masked_placeholder_and_switch_hint():
    html = (TEMPLATES / "advanced.html").read_text(encoding="utf-8")
    # allSettled 里 5 个请求：模型 / 联网检索 / ASR / 语音 / 高级设置数据——/api/advanced 在第 5 项
    block = html[html.index("const results=await Promise.allSettled(["):html.index("this.msg=errors.join")]
    assert "this.api('/api/advanced')," in block
    assert "if(results[4].status==='fulfilled')this.applyPayload(results[4].value||{});" in block
    assert "results[1].status==='rejected')errors.push('联网检索" in block
    assert "results[3].status==='rejected')errors.push('语音配置" in block
    assert "results[3].value" not in block
    # 掩码展示 + 切换需保存的提示
    assert "saved_key_masks" in html and "setupKeyMask()" in html
    assert "return this.setupKeyMask||'已保存该提供方的 Key" in html  # 框里只放掩码本身
    for needle in ("asr.api_key_masked ||", "tts.api_key_masked ||", "tts.app_id_masked ||", "tts.access_token_masked ||", "if(w.api_key_masked) return w.api_key_masked;"):
        assert needle in html, needle
    assert "点下方「保存并应用」后才真正切换" in html


# ---------------- 5) Agent 页中文输入法 ----------------


def test_agent_page_ignores_enter_while_ime_is_composing():
    html = (TEMPLATES / "agent.html").read_text(encoding="utf-8")
    assert '@keydown.enter.exact="onEnter"' in html and "keydown.enter.exact.prevent" not in html
    handler = html[html.index("onEnter(e){"):html.index("async send(){")]
    assert "e.isComposing || e.keyCode === 229" in handler and "e.preventDefault();" in handler and "this.send();" in handler


# ---------------- 2) 语音链路音色 ----------------


def test_rtc_streaming_tts_reloads_speaker_each_turn():
    src = (ROOT / "service/src/deskbot_server/rtc_livekit_plugins.py").read_text(encoding="utf-8")
    streaming = src[src.index("class DeskbotSeedSpeechSynthesizeStream"):src.index("return DeskbotSeedSpeechTTS()")]
    assert "live = _reload_tts_config(config)" in streaming
    assert "synthesize_doubao_tts(\n                        clean,\n                        live," in streaming
    assert "synthesize_doubao_tts(\n                        clean,\n                        config," not in streaming
    chunked = src[src.index("class DeskbotSeedSpeechChunkedStream"):src.index("class DeskbotSeedSpeechSynthesizeStream")]
    assert "live = _reload_tts_config(config)" in chunked
    # 连接池按 speaker 分键：换音色后拿到的是新连接，不会复用旧嗓子的长连接
    doubao = (ROOT / "service/src/deskbot_server/tts/doubao.py").read_text(encoding="utf-8")
    start = doubao.index("def _config_pool_key(")
    pool_key = doubao[start:doubao.index("cfg.audio_format", start)]
    assert "cfg.speaker" in pool_key


def test_websearch_asr_tts_status_carry_masked_keys(monkeypatch):
    from deskbot_server import web_tools
    from deskbot_server.tts.doubao import DoubaoTtsConfig

    monkeypatch.setenv("ARK_WEB_SEARCH_API_KEY", "ws-1234567890abcdef7788")
    status = web_tools.ark_search_status()
    assert status["api_key_source"] == "own" and status["api_key_masked"] == "ws-…7788"
    monkeypatch.delenv("ARK_WEB_SEARCH_API_KEY")
    monkeypatch.setattr(web_tools, "_llm_ark_search_config", lambda: ("https://ark/responses", "ark-abcdefghijklmn9999", "m"))
    status = web_tools.ark_search_status()
    assert status["api_key_source"] == "llm" and status["api_key_masked"] == "ark…9999"
    monkeypatch.setattr(web_tools, "_llm_ark_search_config", lambda: None)
    assert web_tools.ark_search_status()["api_key_masked"] == ""
    cfg = DoubaoTtsConfig(api_key="tts-1234567890abcdef1234", speaker="s", resource_id="r", model="m", ws_url="wss://x", sample_rate=16000, audio_format="pcm", enable_timestamp=False, app_id="app-12345678", access_token="tok-abcdefghijkl5678")
    masked = cfg.masked()
    assert masked["api_key_masked"] == "tts…1234" and masked["app_id_masked"] == "app…5678" and masked["access_token_masked"] == "tok…5678"
    assert "1234567890" not in str(masked)


def test_asr_status_carries_masked_key(monkeypatch):
    from deskbot_server.asr import env_store

    src = open(env_store.__file__, encoding="utf-8").read()
    assert src.count('"api_key_masked"') == 3  # 缺省 + 豆包流式 + OpenAI 兼容


def test_face_svg_key_resolution_prefers_ark_keys_over_a_non_ark_llm_key(monkeypatch):
    """2026-09-14：大模型切到 DeepSeek 后画表情 401——画表情只认方舟 Key。"""
    import deskbot_server.ark_face_svg as ark_face_svg

    for name in ("ARK_IMAGE_GEN_API_KEY", "LLM_API_KEY", "LLM_BASE_URL", "LLM_API_KEY_DOUBAO", "ARK_WEB_SEARCH_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("deskbot_server.env.read_env_file", lambda: {})
    monkeypatch.setenv("LLM_API_KEY", "sk-deepseek")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("LLM_API_KEY_DOUBAO", "ark-saved-doubao")
    assert ark_face_svg._resolve_api_key() == "ark-saved-doubao"
    monkeypatch.setenv("ARK_IMAGE_GEN_API_KEY", "ark-image")
    assert ark_face_svg._resolve_api_key() == "ark-image"  # 表情页单独填的生图 Key 最优先
    monkeypatch.delenv("ARK_IMAGE_GEN_API_KEY")
    monkeypatch.delenv("LLM_API_KEY_DOUBAO")
    monkeypatch.setenv("ARK_WEB_SEARCH_API_KEY", "ark-search")
    assert ark_face_svg._resolve_api_key() == "ark-search"
    monkeypatch.delenv("ARK_WEB_SEARCH_API_KEY")
    with pytest.raises(ValueError, match="火山方舟"):
        ark_face_svg._resolve_api_key()
    # 大模型本身是方舟：照旧复用 LLM_API_KEY；.env 里保存的豆包 Key 即使没进环境变量也能读到
    monkeypatch.setenv("LLM_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
    assert ark_face_svg._resolve_api_key() == "sk-deepseek"
    monkeypatch.setenv("LLM_BASE_URL", "https://api.xiaomimimo.com/v1")
    monkeypatch.setattr("deskbot_server.env.read_env_file", lambda: {"LLM_API_KEY_DOUBAO": "ark-from-file"})
    assert ark_face_svg._resolve_api_key() == "ark-from-file"
