"""已注册人脸档案持久化（``data/local/face_profiles.json``）。"""
from __future__ import annotations

from typing import Any, Optional

from deskbot_server.constants import FACE_PROFILES_FILE
from deskbot_server.core.json_store import JsonDocumentStore
from deskbot_server.device_data import resolve_json_path
from deskbot_server.face_identity import (
    descriptor_cosine_similarity,
    ema_update_descriptor,
)
from deskbot_server.vision.face_embedding import (
    is_embedding_vector,
    is_legacy_geometric_vector,
)


def _normalize_profile(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("profile must be object")
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ValueError("name required")
    desc_raw = raw.get("descriptor")
    if not isinstance(desc_raw, list) or len(desc_raw) < 4:
        raise ValueError("descriptor must be a float vector")
    descriptor = [float(x) for x in desc_raw]
    kind = str(raw.get("descriptor_kind") or "").strip().lower()
    if not kind:
        kind = "embedding" if is_embedding_vector(descriptor) else "geometry"
    pid = int(raw.get("person_id", 0))
    if pid <= 0:
        raise ValueError("person_id must be positive")
    return {
        "person_id": pid,
        "name": name,
        "descriptor": descriptor,
        "descriptor_kind": kind,
    }


def _normalize_profiles_lenient(raw: object) -> list[dict[str, Any]]:
    """读取口径：跳过坏条目（写入口径见 ``_normalize_profiles_strict``）。"""
    if isinstance(raw, dict):
        items = raw.get("profiles") or []
    elif isinstance(raw, list):
        items = raw
    else:
        return []
    out: list[dict[str, Any]] = []
    for item in items:
        try:
            out.append(_normalize_profile(item))
        except (TypeError, ValueError):
            continue
    return out


def _normalize_profiles_strict(profiles: list[dict[str, Any]]) -> dict[str, Any]:
    return {"profiles": [_normalize_profile(p) for p in profiles]}


_STORE = JsonDocumentStore(
    lambda: resolve_json_path(FACE_PROFILES_FILE),
    normalize=_normalize_profiles_lenient,
    normalize_save=_normalize_profiles_strict,
    default=list,
)


def load_face_profiles() -> list[dict[str, Any]]:
    return _STORE.load()


def save_face_profiles(profiles: list[dict[str, Any]]) -> None:
    _STORE.save(profiles)


def next_person_id(profiles: list[dict[str, Any]]) -> int:
    if not profiles:
        return 1
    return max(int(p["person_id"]) for p in profiles) + 1


def _same_descriptor_space(a: list[float], b: list[float]) -> bool:
    ae = is_embedding_vector(a)
    be = is_embedding_vector(b)
    if ae or be:
        return ae and be
    return is_legacy_geometric_vector(a) and is_legacy_geometric_vector(b)


def best_profile_similarity(
    profiles: list[dict[str, Any]],
    descriptor: list[float],
) -> tuple[Optional[dict[str, Any]], float]:
    """返回最相似档案（不设阈值）；仅比较同类型向量。"""
    best: Optional[dict[str, Any]] = None
    best_sim = -1.0
    for p in profiles:
        pd = p.get("descriptor")
        if not isinstance(pd, list):
            continue
        if not _same_descriptor_space(descriptor, pd):
            continue
        sim = descriptor_cosine_similarity(descriptor, pd)
        if sim > best_sim:
            best_sim = sim
            best = p
    return best, best_sim


def find_profile_by_similarity(
    profiles: list[dict[str, Any]],
    descriptor: list[float],
    *,
    threshold: float,
) -> tuple[Optional[dict[str, Any]], float]:
    best, best_sim = best_profile_similarity(profiles, descriptor)
    if best is not None and best_sim >= threshold:
        return best, best_sim
    return None, best_sim


def resolve_profile_match(
    profiles: list[dict[str, Any]],
    descriptor: list[float],
    *,
    match_threshold: float,
    keep_threshold: float,
    locked_person_id: Optional[int] = None,
) -> tuple[Optional[dict[str, Any]], float]:
    """档案匹配：已锁定 person 时用更低阈值保持，避免转头时 person_id 闪烁。"""
    best, best_sim = best_profile_similarity(profiles, descriptor)
    if locked_person_id is not None:
        for p in profiles:
            if int(p["person_id"]) == int(locked_person_id):
                sim = descriptor_cosine_similarity(descriptor, p["descriptor"])
                if sim >= keep_threshold:
                    return p, sim
                return None, best_sim
    if best is not None and best_sim >= match_threshold:
        return best, best_sim
    return None, best_sim


def list_face_profiles_summary() -> list[dict[str, Any]]:
    """列表展示用：不含 descriptor 向量。"""
    return [
        {
            "person_id": int(p["person_id"]),
            "name": str(p["name"]),
            "descriptor_kind": str(p.get("descriptor_kind") or ""),
        }
        for p in load_face_profiles()
    ]


def delete_face_profile(person_id: int) -> bool:
    """按 ``person_id`` 删除已注册人脸档案。"""
    try:
        pid = int(person_id)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    with _STORE.lock():
        profiles = _STORE.load()
        kept = [p for p in profiles if int(p["person_id"]) != pid]
        if len(kept) == len(profiles):
            return False
        _STORE.save_unlocked(kept)
        return True


def update_face_profile_name(
    person_id: int, name: str
) -> Optional[dict[str, Any]]:
    """更新已注册人脸档案名称，返回摘要；档案不存在时返回 ``None``。"""
    try:
        pid = int(person_id)
    except (TypeError, ValueError):
        return None
    clean_name = str(name or "").strip()
    if pid <= 0:
        return None
    if not clean_name:
        raise ValueError("name required")
    with _STORE.lock():
        profiles = _STORE.load()
        for p in profiles:
            if int(p["person_id"]) == pid:
                p["name"] = clean_name
                _STORE.save_unlocked(profiles)
                return {
                    "person_id": int(p["person_id"]),
                    "name": str(p["name"]),
                    "descriptor_kind": str(p.get("descriptor_kind") or ""),
                }
        return None


def upsert_profile(
    profiles: list[dict[str, Any]],
    *,
    name: str,
    descriptor: list[float],
    person_id: Optional[int] = None,
    merge_threshold: float = 0.88,
) -> dict[str, Any]:
    """注册或合并同名/相似档案，返回最终 profile。"""
    name = str(name).strip()
    if not name:
        raise ValueError("name required")
    matched, sim = find_profile_by_similarity(
        profiles, descriptor, threshold=merge_threshold
    )
    kind = "embedding" if is_embedding_vector(descriptor) else "geometry"
    if matched is not None and matched.get("name") == name:
        matched["descriptor"] = ema_update_descriptor(matched["descriptor"], descriptor, alpha=0.35)
        matched["descriptor_kind"] = kind
        return matched
    if matched is not None and sim >= 0.95:
        matched["name"] = name
        matched["descriptor"] = ema_update_descriptor(matched["descriptor"], descriptor, alpha=0.35)
        matched["descriptor_kind"] = kind
        return matched
    pid = int(person_id) if person_id else next_person_id(profiles)
    profile = {
        "person_id": pid,
        "name": name,
        "descriptor": list(descriptor),
        "descriptor_kind": kind,
    }
    profiles.append(profile)
    return profile


def register_face_profile(
    *,
    name: str,
    descriptor: list[float],
    person_id: Optional[int] = None,
    merge_threshold: float = 0.88,
) -> dict[str, Any]:
    """Lock the complete PC-local load/merge/save transaction."""

    with _STORE.lock():
        profiles = _STORE.load()
        profile = upsert_profile(
            profiles,
            name=name,
            descriptor=descriptor,
            person_id=person_id,
            merge_threshold=merge_threshold,
        )
        _STORE.save_unlocked(profiles)
        return dict(profile)
