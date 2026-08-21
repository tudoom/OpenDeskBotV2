"""PC-local LLM model configuration (``data/local/llm_models.json``)."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from deskbot_server.core.json_store import JsonDocumentStore
from deskbot_server.device_data import local_data_dir
from deskbot_server.safe_fetch import validate_provider_http_url

LLM_MODELS_FILENAME = "llm_models.json"

SUPPORTED_PROTOCOLS = (
    "ark",
    "ark_responses",
    "doubao",
    "volcengine",
    "openai",
    "dashscope",
    "anthropic",
    "azure",
    "gemini",
    "ollama",
)


@dataclass(frozen=True)
class LlmModelEntry:
    id: str
    name: str
    model_name: str
    protocol: str
    base_url: str
    api_key: str

    def to_dict(self, *, mask_key: bool = False) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "model_name": self.model_name,
            "protocol": self.protocol,
            "base_url": self.base_url,
            "api_key": mask_api_key(self.api_key) if mask_key else self.api_key,
            "api_key_set": bool(str(self.api_key or "").strip()),
        }


def mask_api_key(key: str) -> str:
    k = str(key or "").strip()
    if not k:
        return ""
    if len(k) <= 8:
        return "****"
    return f"{k[:4]}...{k[-4:]}"


def _empty_document() -> dict[str, Any]:
    return {"active_model_id": None, "models": []}


def _parse_entry(raw: dict[str, Any]) -> LlmModelEntry | None:
    if not isinstance(raw, dict):
        return None
    model_id = str(raw.get("id") or "").strip()
    name = str(raw.get("name") or "").strip()
    model_name = str(raw.get("model_name") or "").strip()
    protocol = str(raw.get("protocol") or "openai").strip().lower() or "openai"
    base_url = str(raw.get("base_url") or "").strip()
    api_key = str(raw.get("api_key") or "").strip()
    if not model_id or not name or not model_name:
        return None
    if protocol not in SUPPORTED_PROTOCOLS:
        protocol = "openai"
    return LlmModelEntry(
        id=model_id,
        name=name,
        model_name=model_name,
        protocol=protocol,
        base_url=base_url,
        api_key=api_key,
    )


def _normalize_llm_models_document(data: object) -> dict[str, Any]:
    if not isinstance(data, dict):
        return _empty_document()
    active = data.get("active_model_id")
    active_model_id = str(active).strip() if active else None
    models_raw = data.get("models")
    models: list[dict[str, Any]] = []
    if isinstance(models_raw, list):
        for item in models_raw:
            entry = _parse_entry(item if isinstance(item, dict) else {})
            if entry:
                models.append(
                    {
                        "id": entry.id,
                        "name": entry.name,
                        "model_name": entry.model_name,
                        "protocol": entry.protocol,
                        "base_url": entry.base_url,
                        "api_key": entry.api_key,
                    }
                )
    return {"active_model_id": active_model_id or None, "models": models}


def _canonical_llm_models_document(doc: dict[str, Any]) -> dict[str, Any]:
    active = doc.get("active_model_id")
    active_model_id = str(active).strip() if active else None
    models_out: list[dict[str, Any]] = []
    for item in doc.get("models") or []:
        entry = _parse_entry(item if isinstance(item, dict) else {})
        if entry:
            models_out.append(
                {
                    "id": entry.id,
                    "name": entry.name,
                    "model_name": entry.model_name,
                    "protocol": entry.protocol,
                    "base_url": entry.base_url,
                    "api_key": entry.api_key,
                }
            )
    if active_model_id and not any(m["id"] == active_model_id for m in models_out):
        active_model_id = None
    return {"active_model_id": active_model_id, "models": models_out}


_STORE = JsonDocumentStore(
    lambda: local_data_dir() / LLM_MODELS_FILENAME,
    normalize=_normalize_llm_models_document,
    normalize_save=_canonical_llm_models_document,
    default=_empty_document,
    corrupt_to_default=True,
    mode=0o600,
)


def load_llm_models_document() -> dict[str, Any]:
    return _STORE.load()




def list_llm_models(*, mask_key: bool = True) -> list[dict[str, Any]]:
    doc = load_llm_models_document()
    out: list[dict[str, Any]] = []
    for item in doc.get("models") or []:
        entry = _parse_entry(item)
        if entry:
            out.append(entry.to_dict(mask_key=mask_key))
    return out


def get_active_model_id() -> str | None:
    doc = load_llm_models_document()
    active = doc.get("active_model_id")
    return str(active).strip() if active else None


def get_llm_model(model_id: str) -> LlmModelEntry | None:
    doc = load_llm_models_document()
    for item in doc.get("models") or []:
        entry = _parse_entry(item)
        if entry and entry.id == model_id:
            return entry
    return None


def get_active_llm_model() -> LlmModelEntry | None:
    active_id = get_active_model_id()
    if not active_id:
        return None
    return get_llm_model(active_id)


def add_llm_model(
    *,
    name: str,
    model_name: str,
    protocol: str = "openai",
    base_url: str = "",
    api_key: str = "",
) -> dict[str, Any]:
    name = str(name or "").strip()
    model_name = str(model_name or "").strip()
    if not name:
        raise ValueError("模型名称不能为空")
    if not model_name:
        raise ValueError("模型 ID 不能为空")
    protocol = str(protocol or "openai").strip().lower() or "openai"
    if protocol not in SUPPORTED_PROTOCOLS:
        raise ValueError(f"不支持的协议: {protocol}")
    clean_base_url = str(base_url or "").strip()
    if clean_base_url:
        validate_provider_http_url(clean_base_url)
    with _STORE.lock():
        doc = _STORE.load()
        entry = LlmModelEntry(
            id=uuid.uuid4().hex[:12],
            name=name,
            model_name=model_name,
            protocol=protocol,
            base_url=clean_base_url,
            api_key=str(api_key or "").strip(),
        )
        doc.setdefault("models", []).append(
            {
                "id": entry.id,
                "name": entry.name,
                "model_name": entry.model_name,
                "protocol": entry.protocol,
                "base_url": entry.base_url,
                "api_key": entry.api_key,
            }
        )
        _STORE.save_unlocked(doc)
    return entry.to_dict(mask_key=True)


def update_llm_model(
    model_id: str,
    *,
    name: str | None = None,
    model_name: str | None = None,
    protocol: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
) -> dict[str, Any] | None:
    with _STORE.lock():
        doc = _STORE.load()
        models = doc.get("models") or []
        updated: dict[str, Any] | None = None
        for item in models:
            if not isinstance(item, dict) or str(item.get("id") or "") != model_id:
                continue
            if name is not None:
                n = str(name).strip()
                if not n:
                    raise ValueError("模型名称不能为空")
                item["name"] = n
            if model_name is not None:
                mn = str(model_name).strip()
                if not mn:
                    raise ValueError("模型 ID 不能为空")
                item["model_name"] = mn
            if protocol is not None:
                p = str(protocol).strip().lower() or "openai"
                if p not in SUPPORTED_PROTOCOLS:
                    raise ValueError(f"不支持的协议: {p}")
                item["protocol"] = p
            if base_url is not None:
                clean_base_url = str(base_url).strip()
                if clean_base_url:
                    validate_provider_http_url(clean_base_url)
                item["base_url"] = clean_base_url
            if api_key is not None:
                raw = str(api_key).strip()
                if raw and not raw.startswith("****") and "..." not in raw:
                    item["api_key"] = raw
            entry = _parse_entry(item)
            if entry:
                updated = entry.to_dict(mask_key=True)
            break
        if updated is None:
            return None
        _STORE.save_unlocked(doc)
        return updated


def delete_llm_model(model_id: str) -> bool:
    with _STORE.lock():
        doc = _STORE.load()
        models = doc.get("models") or []
        new_models = [
            m
            for m in models
            if isinstance(m, dict) and str(m.get("id") or "") != model_id
        ]
        if len(new_models) == len(models):
            return False
        doc["models"] = new_models
        if str(doc.get("active_model_id") or "") == model_id:
            doc["active_model_id"] = None
        _STORE.save_unlocked(doc)
        return True


def set_active_llm_model(model_id: str | None) -> str | None:
    with _STORE.lock():
        doc = _STORE.load()
        if model_id:
            model_id = str(model_id).strip()
            if not any(
                isinstance(m, dict) and str(m.get("id") or "") == model_id
                for m in doc.get("models") or []
            ):
                raise ValueError("模型不存在")
            doc["active_model_id"] = model_id
        else:
            doc["active_model_id"] = None
        _STORE.save_unlocked(doc)
        return doc.get("active_model_id")
