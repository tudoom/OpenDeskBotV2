"""RTC 语音链路的「单趟联网」LLM 适配器。

默认链路是 LampGo 用 LiveKit 的 OpenAI Chat Completions 适配器调方舟模型，联网
只能靠 Core 的 ``websearch`` 函数工具：模型先决定调工具 → Core 再发一次方舟
Responses 请求做检索+摘要 → 结果回给模型 → 模型再组织播报，端到端约 8s。

本模块把主模型切到 **方舟 Responses API**（LiveKit ``openai.responses.LLM`` 的
HTTP 模式），并把方舟「联网内容插件」作为服务端内置工具挂到 Agent 上：模型在
回答那一轮自行检索并流式直出文本，省掉一整轮工具往返（实测 ~4–5s）。

启用条件：LLM base_url 指向火山方舟（``*.volces.com``）且未设置
``DESKBOT_RTC_WEB_SEARCH=0``。其它模型/网关保持原有 Chat Completions 链路。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger("deskbot-server")

_DISABLE_ENV = "DESKBOT_RTC_WEB_SEARCH"
# 由 install 阶段写入 worker 进程环境，供工具构造判断是否用内置联网替代 Core websearch。
ACTIVE_ENV = "DESKBOT_RTC_ARK_WEB_SEARCH_ACTIVE"
_ARK_HOST_SUFFIX = "volces.com"
# 单轮最多关键词数 / 返回条数：联网插件按检索次数计费，语音场景 2 个关键词足够。
_WEB_SEARCH_MAX_KEYWORD = 2
_WEB_SEARCH_LIMIT = 3
# 联网时 web_search_call 到首个文本 delta 之间可能安静数秒；SDK 默认 read=5s 会误杀。
_READ_TIMEOUT_SECONDS = 45.0

_installed = False


def web_search_enabled_for(base_url: str | None) -> bool:
    if str(os.environ.get(_DISABLE_ENV) or "").strip().lower() in {"0", "false", "off", "no"}:
        return False
    host = (urlsplit(str(base_url or "")).hostname or "").lower()
    return host.endswith(_ARK_HOST_SUFFIX)


def ark_web_search_active() -> bool:
    return str(os.environ.get(ACTIVE_ENV) or "").strip() == "1"


def responses_base_url(base_url: str) -> str:
    """把 ``.../api/v3[/chat/completions|/responses]`` 归一成 Responses 客户端的 base。"""
    base = str(base_url or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/responses"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return base


@dataclass(frozen=True)
class ArkLlmParams:
    model: str
    api_key: str
    base_url: str
    temperature: float | None
    max_output_tokens: int | None


def _ark_web_search_tool_dict() -> dict[str, Any]:
    return {
        "type": "web_search",
        "max_keyword": _WEB_SEARCH_MAX_KEYWORD,
        "limit": _WEB_SEARCH_LIMIT,
    }


def build_ark_web_search_tool() -> Any:
    """方舟联网插件作为 LiveKit ``ProviderTool``：随 Agent tools 一起序列化进请求。"""
    from livekit.plugins.openai.tools import OpenAITool

    class ArkWebSearch(OpenAITool):
        def __init__(self) -> None:
            super().__init__(id="ark_web_search")

        def to_dict(self) -> dict[str, Any]:
            return _ark_web_search_tool_dict()

    return ArkWebSearch()


# 联网检索期间模型不出字（实测首字 4–6s）。收到 web_search_call 开始事件时先
# 吐一句过渡语进 TTS，把等待遮住；正文随后接上。每个 response 最多一次。
SEARCH_FILLER_TEXT = "我查一下，"


def _attach_search_filler(stream: Any) -> None:
    from livekit.agents import llm as lk_llm

    original = stream._process_event
    state = {"sent": False}

    def _process_event(event: Any) -> None:
        kind = str(getattr(event, "type", "") or "")
        if not state["sent"] and kind == "response.web_search_call.in_progress":
            state["sent"] = True
            stream._event_ch.send_nowait(
                lk_llm.ChatChunk(
                    id=stream._response_id or "filler",
                    delta=lk_llm.ChoiceDelta(content=SEARCH_FILLER_TEXT, role="assistant"),
                )
            )
        original(event)

    stream._process_event = _process_event


def build_ark_responses_llm(params: ArkLlmParams) -> Any:
    import httpx
    from livekit.agents.types import NOT_GIVEN
    from livekit.plugins.openai.responses import LLM as ResponsesLLM

    class DeskbotArkResponsesLLM(ResponsesLLM):
        """方舟 Responses：关闭深度思考（联网场景实测 18s→6s），不用服务端会话状态。"""

        def chat(self, *args: Any, **kwargs: Any):  # type: ignore[override]
            extra = dict(kwargs.pop("extra_kwargs", None) or {})
            body = dict(extra.get("extra_body") or {})
            body.setdefault("thinking", {"type": "disabled"})
            extra["extra_body"] = body
            kwargs["extra_kwargs"] = extra
            stream = super().chat(*args, **kwargs)
            _attach_search_filler(stream)
            return stream

    return DeskbotArkResponsesLLM(
        model=params.model,
        api_key=params.api_key,
        base_url=responses_base_url(params.base_url),
        use_websocket=False,
        # store=False：每轮全量重发上下文，不依赖 previous_response_id（方舟未承诺支持）。
        store=False,
        temperature=params.temperature if params.temperature is not None else NOT_GIVEN,
        max_output_tokens=(
            params.max_output_tokens if params.max_output_tokens is not None else NOT_GIVEN
        ),
        timeout=httpx.Timeout(connect=15.0, read=_READ_TIMEOUT_SECONDS, write=10.0, pool=5.0),
    )


def params_from_lampgo(config: Any, runtime: Any) -> ArkLlmParams | None:
    """从 LampGo 的 roles 配置里取出与 ``create_llm`` 相同的模型/凭证/选项。"""
    from lampgo_livekit_agent.llm import _openai_credentials, _require_llm_component

    component = _require_llm_component(runtime.voice_agent.llm)
    provider = config.provider_for(
        component, path=f"voice_agents.{runtime.voice_agent.name}.llm"
    )
    if provider.type != "openai":
        return None
    creds = _openai_credentials(provider)
    options = dict(component.options or {})
    model = str(options.get("model") or "").strip()
    api_key = str(creds.get("api_key") or "").strip()
    base_url = str(creds.get("base_url") or "").strip()
    if not model or not api_key or not base_url:
        return None
    temperature = options.get("temperature")
    max_tokens = options.get("max_output_tokens", options.get("max_tokens"))
    return ArkLlmParams(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=float(temperature) if temperature is not None else None,
        max_output_tokens=int(max_tokens) if max_tokens is not None else None,
    )


def disable_thinking_on_chat_llm(llm: Any) -> Any:
    """非方舟的 Chat Completions 链路同样关闭深度思考，与 Core 的 chat 载荷一致（全部调用路径
    ``thinking: disabled``）。DeepSeek 这类混合思考模型默认开思考：思考模式下拒绝
    ``tool_choice=required``（主动陪伴催标结果就用它，2026-09-14 实测 400），而且每轮多几秒推理。
    就地包一层 ``chat``，只补 ``extra_body.thinking``，其它参数原样透传。"""
    chat = getattr(llm, "chat", None)
    if not callable(chat):
        return llm

    def _chat(*args: Any, **kwargs: Any):
        raw = kwargs.pop("extra_kwargs", None)
        extra = dict(raw) if isinstance(raw, dict) else {}
        body = dict(extra.get("extra_body") or {})
        body.setdefault("thinking", {"type": "disabled"})
        extra["extra_body"] = body
        kwargs["extra_kwargs"] = extra
        return chat(*args, **kwargs)

    try:
        llm.chat = _chat
    except Exception:  # noqa: BLE001 - 不可写属性的对象就原样返回
        return llm
    return llm


def install_lampgo_llm_adapter() -> bool:
    """把 LampGo 的 ``create_llm`` 换成方舟 Responses（仅当 LLM 指向方舟时生效）。"""

    global _installed
    if _installed:
        return True

    import lampgo_livekit_agent.llm as lampgo_llm
    import lampgo_livekit_agent.worker as lampgo_worker

    original_create_llm = lampgo_llm.create_llm

    def _create_llm(*, config: Any, runtime: Any):
        params = params_from_lampgo(config, runtime)
        if params is None or not web_search_enabled_for(params.base_url):
            os.environ.pop(ACTIVE_ENV, None)
            return disable_thinking_on_chat_llm(original_create_llm(config=config, runtime=runtime))
        try:
            llm = build_ark_responses_llm(params)
        except Exception as exc:  # 任何构造失败都退回原链路，不能让语音会话起不来
            os.environ.pop(ACTIVE_ENV, None)
            logger.warning("[rtc] Ark Responses LLM unavailable, falling back: %r", exc)
            return original_create_llm(config=config, runtime=runtime)
        os.environ[ACTIVE_ENV] = "1"
        logger.info(
            "[rtc] Ark Responses LLM active model=%s web_search(max_keyword=%d, limit=%d)",
            params.model,
            _WEB_SEARCH_MAX_KEYWORD,
            _WEB_SEARCH_LIMIT,
        )
        return llm

    # worker 在本模块接管前已经 ``from .llm import create_llm``，两处都要替换。
    lampgo_llm.create_llm = _create_llm
    lampgo_worker.create_llm = _create_llm
    _installed = True
    return True


__all__ = [
    "ACTIVE_ENV",
    "ArkLlmParams",
    "ark_web_search_active",
    "build_ark_responses_llm",
    "build_ark_web_search_tool",
    "install_lampgo_llm_adapter",
    "params_from_lampgo",
    "responses_base_url",
    "web_search_enabled_for",
]
