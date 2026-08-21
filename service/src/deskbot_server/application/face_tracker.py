"""跨帧 face_id 跟踪 + 人脸 re-id（识别仅走 InsightFace embedding）。

跟踪（face_id 稳定性）只依赖空间线索：鼻尖距离 + bbox IoU，embedding 可用时
作为额外的关联证据。身份（person_id/person_name）只在检出脸带 embedding 时
与档案匹配；InsightFace 不可用时人脸仍被检测/跟踪，但识别明确降级为不匹配。
"""
from __future__ import annotations

import math
import os
import time
import weakref
from dataclasses import dataclass, field
from typing import Any, Optional

from deskbot_server.constants import FACE_PROFILES_FILE
from deskbot_server.device_data import resolve_json_path
from deskbot_server.face_identity import (
    _bbox_iou,
    _points_bbox,
    attach_descriptor,
    descriptor_cosine_similarity,
    ema_update_descriptor,
    is_embedding_vector,
)
from deskbot_server.face_profiles_store import (
    best_profile_similarity,
    load_face_profiles,
    resolve_profile_match,
)

_Bbox = tuple[float, float, float, float]


def _nose_xy(face: dict[str, Any]) -> Optional[tuple[float, float]]:
    points = face.get("points") or []
    for p in points:
        if isinstance(p, dict) and p.get("name") == "nose":
            try:
                return float(p["x"]), float(p["y"])
            except (TypeError, ValueError, KeyError):
                return None
    landmarks = face.get("landmarks") or []
    for p in landmarks:
        if isinstance(p, dict) and p.get("name") == "nose":
            try:
                return float(p["x"]), float(p["y"])
            except (TypeError, ValueError, KeyError):
                return None
    return None


@dataclass
class _Track:
    face_id: int
    nose: tuple[float, float]
    bbox: Optional[_Bbox] = None
    descriptor: Optional[list[float]] = None
    last_seen_monotonic: float = field(default_factory=lambda: time.monotonic())
    person_id: Optional[int] = None
    person_name: Optional[str] = None
    lost: int = 0


_active_trackers: weakref.WeakSet["FaceTracker"] = weakref.WeakSet()


def _register_tracker(tracker: "FaceTracker") -> None:
    _active_trackers.add(tracker)


def reload_all_trackers() -> None:
    for tracker in list(_active_trackers):
        tracker.reload_profiles()


class FaceTracker:
    """为每帧人脸分配 ``face_id``，并用 embedding 匹配已注册 ``person_id`` / 人名。

    - 鼻尖距离 + bbox IoU 关联 track；embedding 可用时联合判据更稳
    - ``person_id`` 绑定后使用**滞回阈值**（保持阈值 < 匹配阈值），减少闪烁
    - ``face_id`` 单调递增、不在 1–32 间循环复用
    - 档案向量仅在注册时写入文件；运行时**不** EMA 污染 ``face_profiles.json``
    """

    def __init__(
        self,
        *,
        device_id: Optional[str] = None,
        max_dist_px: float = 90.0,
        max_lost_frames: int = 18,
        max_ids: int = 32,
        identity_similarity_threshold: float = 0.40,
        descriptor_ema_alpha: float = 0.1,
        identity_keep_margin: float = 0.12,
        identity_rebind_margin: float = 0.05,
        max_idle_seconds: float = 3.0,
    ) -> None:
        self.max_dist_px = max(16.0, float(max_dist_px))
        self.max_lost_frames = max(3, int(max_lost_frames))
        self.max_ids = max(4, int(max_ids))
        self.identity_embedding_threshold = max(0.25, min(0.99, float(identity_similarity_threshold)))
        self.descriptor_ema_alpha = max(0.05, min(0.35, float(descriptor_ema_alpha)))
        self.identity_keep_margin = max(0.05, min(0.25, float(identity_keep_margin)))
        self.identity_rebind_margin = max(
            0.0, min(0.25, float(identity_rebind_margin))
        )
        self.max_idle_seconds = max(0.5, float(max_idle_seconds))
        self._device_id = str(device_id or "").strip() or None
        self._profiles_path = resolve_json_path(FACE_PROFILES_FILE)
        self._next_face_id = 1
        self._tracks: dict[int, _Track] = {}
        self._profiles_mtime: float = 0.0
        self._profiles: list[dict[str, Any]] = load_face_profiles()
        self._profiles_mtime = self._profiles_file_mtime()
        self._last_faces: list[dict[str, Any]] = []
        _register_tracker(self)

    def _match_threshold(self) -> float:
        return self.identity_embedding_threshold

    def _keep_threshold(self) -> float:
        return max(0.25, self._match_threshold() - self.identity_keep_margin)

    def _profiles_file_mtime(self) -> float:
        try:
            return os.path.getmtime(self._profiles_path)
        except OSError:
            return 0.0

    def _maybe_reload_profiles(self) -> None:
        mtime = self._profiles_file_mtime()
        if mtime == self._profiles_mtime:
            return
        self._profiles_mtime = mtime
        self._profiles = load_face_profiles()

    def reload_profiles(self) -> None:
        self._profiles_mtime = self._profiles_file_mtime()
        self._profiles = load_face_profiles()

    def _alloc_face_id(self) -> int:
        fid = self._next_face_id
        self._next_face_id += 1
        return fid

    def _scaled_max_dist(self, image_w: int) -> float:
        w = max(160, int(image_w or 320))
        return self.max_dist_px * (w / 320.0)

    def _track_match_cost(
        self,
        track: _Track,
        nose: tuple[float, float],
        bbox: Optional[_Bbox],
        desc: Optional[list[float]],
        *,
        max_dist_px: float,
    ) -> float:
        tx, ty = track.nose
        nd = math.hypot(nose[0] - tx, nose[1] - ty)
        if nd > max_dist_px:
            return float("inf")
        spatial = nd / max_dist_px
        if desc is not None and track.descriptor is not None:
            sim = descriptor_cosine_similarity(desc, track.descriptor)
            if sim < 0.20:
                return float("inf")
            feat = 1.0 - max(-1.0, sim)
            return spatial * 0.35 + feat * 0.65
        # 无 embedding（识别降级）：纯空间跟踪，bbox 重叠越大越可信。
        iou = (
            _bbox_iou(bbox, track.bbox)
            if bbox is not None and track.bbox is not None
            else 0.0
        )
        return spatial * 0.5 + (1.0 - iou) * 0.5

    def _resolve_person(
        self,
        desc: list[float],
        *,
        locked_person_id: Optional[int],
    ) -> tuple[Optional[dict[str, Any]], float]:
        return resolve_profile_match(
            self._profiles,
            desc,
            match_threshold=self._match_threshold(),
            keep_threshold=self._keep_threshold(),
            locked_person_id=locked_person_id,
        )

    def _bind_person(self, track: _Track, profile: dict[str, Any], descriptor: list[float]) -> None:
        track.person_id = int(profile["person_id"])
        track.person_name = str(profile["name"])
        track.descriptor = ema_update_descriptor(
            track.descriptor,
            descriptor,
            alpha=self.descriptor_ema_alpha,
        )

    def _best_unambiguous_profile(
        self,
        desc: list[float],
    ) -> tuple[Optional[dict[str, Any]], float]:
        """Return a threshold-passing best match separated from the runner-up."""

        scored: list[tuple[float, dict[str, Any]]] = []
        for profile in self._profiles:
            profile_desc = profile.get("descriptor")
            if not isinstance(profile_desc, list) or len(profile_desc) != len(desc):
                continue
            scored.append(
                (descriptor_cosine_similarity(desc, profile_desc), profile)
            )
        if not scored:
            return None, -1.0
        scored.sort(key=lambda item: item[0], reverse=True)
        best_sim, best = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else -1.0
        if best_sim < self._match_threshold():
            return None, best_sim
        if best_sim - runner_up < self.identity_rebind_margin:
            return None, best_sim
        return best, best_sim

    def _try_bind_profile(self, track: _Track, desc: list[float]) -> Optional[float]:
        if track.person_id is None:
            profile, sim = self._best_unambiguous_profile(desc)
            if profile is not None:
                self._bind_person(track, profile, desc)
                return sim
            return None

        profile, sim = self._resolve_person(
            desc,
            locked_person_id=track.person_id,
        )
        if profile is not None:
            self._bind_person(track, profile, desc)
            return sim

        # Never expose a stale name for a frame that failed the locked profile
        # threshold. The former multi-frame grace period made one person wear
        # another person's label while the tracker recovered.
        track.person_id = None
        track.person_name = None
        profile, sim = self._best_unambiguous_profile(desc)
        if profile is not None:
            self._bind_person(track, profile, desc)
            return sim
        return None

    def _expire_idle_tracks(self, now_monotonic: float) -> None:
        for tid, track in list(self._tracks.items()):
            if now_monotonic - track.last_seen_monotonic > self.max_idle_seconds:
                self._tracks.pop(tid, None)

    def _find_track_for_person(
        self,
        person_id: int,
        *,
        excluded_track_ids: set[int] | None = None,
    ) -> Optional[tuple[int, _Track]]:
        for tid, track in self._tracks.items():
            if excluded_track_ids and tid in excluded_track_ids:
                continue
            if track.person_id == person_id:
                return tid, track
        return None

    def _mark_all_lost(self) -> None:
        for tid, track in list(self._tracks.items()):
            track.lost += 1
            if track.lost > self.max_lost_frames:
                self._tracks.pop(tid, None)

    def _touch_track(
        self,
        track: _Track,
        nose: tuple[float, float],
        bbox: Optional[_Bbox],
        now_monotonic: float,
    ) -> None:
        track.nose = nose
        if bbox is not None:
            track.bbox = bbox
        track.lost = 0
        track.last_seen_monotonic = now_monotonic

    def _absorb_descriptor(self, track: _Track, desc: Optional[list[float]]) -> None:
        if desc is None:
            return
        track.descriptor = ema_update_descriptor(
            track.descriptor,
            desc,
            alpha=self.descriptor_ema_alpha,
        )

    def assign_ids(self, faces: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self._maybe_reload_profiles()
        now_monotonic = time.monotonic()
        self._expire_idle_tracks(now_monotonic)
        if not faces:
            self._mark_all_lost()
            self._last_faces = []
            return []

        detections: list[
            tuple[
                int,
                tuple[float, float],
                Optional[_Bbox],
                Optional[list[float]],
                dict[str, Any],
                int,
            ]
        ] = []
        for idx, face in enumerate(faces):
            desc = face.get("face_descriptor") or attach_descriptor(face)
            if desc is not None and not is_embedding_vector(desc):
                desc = None
            nose = _nose_xy(face)
            if nose is None:
                continue
            bbox = _points_bbox(face)
            image_w = int(face.get("image_w") or 0) or 320
            detections.append((idx, nose, bbox, desc, face, image_w))

        if not detections:
            self._mark_all_lost()
            self._last_faces = []
            return []

        max_dist = self._scaled_max_dist(max(d[5] for d in detections))

        assigned_track: dict[int, int] = {}
        used_tracks: set[int] = set()
        pairs: list[tuple[float, int, int]] = []
        for det_idx, nose, bbox, desc, _face, _iw in detections:
            for tid, track in self._tracks.items():
                cost = self._track_match_cost(
                    track,
                    nose,
                    bbox,
                    desc,
                    max_dist_px=max_dist,
                )
                if math.isfinite(cost):
                    pairs.append((cost, det_idx, tid))
        pairs.sort(key=lambda x: x[0])

        for _cost, det_idx, tid in pairs:
            if det_idx in assigned_track or tid in used_tracks:
                continue
            assigned_track[det_idx] = tid
            used_tracks.add(tid)

        out: list[dict[str, Any]] = []
        for det_idx, nose, bbox, desc, face, _iw in detections:
            tagged = dict(face)
            track: _Track

            if det_idx in assigned_track:
                tid = assigned_track[det_idx]
                track = self._tracks[tid]
                self._touch_track(track, nose, bbox, now_monotonic)
                self._absorb_descriptor(track, desc)
                if desc is not None:
                    sim = self._try_bind_profile(track, desc)
                    if sim is not None:
                        tagged["identity_score"] = round(sim, 3)
                        if track.person_id is not None:
                            tagged["match_source"] = "person_profile"
                tagged["face_id"] = tid
                tagged["face_id_source"] = "spatial_track"
            else:
                profile: Optional[dict[str, Any]] = None
                sim = -1.0
                if desc is not None:
                    profile, sim = self._resolve_person(
                        desc, locked_person_id=None
                    )
                reused_person: Optional[tuple[int, _Track]] = None
                if profile is not None:
                    reused_person = self._find_track_for_person(
                        int(profile["person_id"]),
                        excluded_track_ids=used_tracks,
                    )

                track_match: Optional[tuple[int, _Track, float]] = None
                if desc is not None:
                    best_tid: Optional[int] = None
                    best_track: Optional[_Track] = None
                    best_sim = -1.0
                    desc_thr = max(0.28, self._keep_threshold() - 0.05)
                    for tid, tr in self._tracks.items():
                        if tid in used_tracks or tr.descriptor is None:
                            continue
                        ds = descriptor_cosine_similarity(desc, tr.descriptor)
                        if ds >= desc_thr and ds > best_sim:
                            best_sim = ds
                            best_tid = tid
                            best_track = tr
                    if best_tid is not None and best_track is not None:
                        track_match = (best_tid, best_track, best_sim)

                if reused_person is not None:
                    tid, track = reused_person
                    self._touch_track(track, nose, bbox, now_monotonic)
                    if profile is not None and desc is not None:
                        self._bind_person(track, profile, desc)
                        tagged["identity_score"] = round(sim, 3)
                        tagged["match_source"] = "person_profile"
                    used_tracks.add(tid)
                elif track_match is not None:
                    tid, track, tsim = track_match
                    self._touch_track(track, nose, bbox, now_monotonic)
                    self._absorb_descriptor(track, desc)
                    if track.person_id is None and profile is not None and desc is not None:
                        self._bind_person(track, profile, desc)
                        tagged["identity_score"] = round(sim, 3)
                        tagged["match_source"] = "person_profile"
                    else:
                        tagged["identity_score"] = round(tsim, 3)
                    tagged["match_source"] = tagged.get("match_source") or "descriptor_track"
                    used_tracks.add(tid)
                elif profile is not None and desc is not None:
                    tid = self._alloc_face_id()
                    track = _Track(
                        face_id=tid,
                        nose=nose,
                        bbox=bbox,
                        descriptor=list(desc),
                        last_seen_monotonic=now_monotonic,
                    )
                    self._bind_person(track, profile, desc)
                    tagged["identity_score"] = round(sim, 3)
                    tagged["match_source"] = "person_profile"
                    self._tracks[tid] = track
                    used_tracks.add(tid)
                else:
                    tid = self._alloc_face_id()
                    track = _Track(
                        face_id=tid,
                        nose=nose,
                        bbox=bbox,
                        descriptor=list(desc) if desc is not None else None,
                        last_seen_monotonic=now_monotonic,
                    )
                    self._tracks[tid] = track
                    tagged["match_source"] = "new"
                    used_tracks.add(tid)
                tagged["face_id"] = tid

            if track.person_id is not None:
                tagged["person_id"] = track.person_id
                tagged["person_name"] = track.person_name
            elif desc is not None and self._profiles:
                _best, best_sim = best_profile_similarity(self._profiles, desc)
                if best_sim >= 0:
                    tagged["identity_score"] = round(best_sim, 3)

            if desc is not None:
                tagged["descriptor_dim"] = len(desc)
                tagged["descriptor_kind"] = "embedding"

            ms = tagged.get("match_source")
            if ms:
                tagged["face_id_source"] = ms
            out.append(tagged)

        # A person identity is exclusive within one frame.  Detector
        # duplicates or two similar people must never expose the same
        # ``person_id`` twice.  Keep the strongest observation and unlock the
        # losing track so it can be recognised independently next frame.
        by_person: dict[int, list[dict[str, Any]]] = {}
        for tagged in out:
            person_id = tagged.get("person_id")
            if person_id is None:
                continue
            by_person.setdefault(int(person_id), []).append(tagged)
        for duplicates in by_person.values():
            if len(duplicates) < 2:
                continue
            duplicates.sort(
                key=lambda row: float(row.get("identity_score") or -1.0),
                reverse=True,
            )
            for duplicate in duplicates[1:]:
                tid = int(duplicate.get("face_id") or 0)
                losing_track = self._tracks.get(tid)
                if losing_track is not None:
                    losing_track.person_id = None
                    losing_track.person_name = None
                duplicate.pop("person_id", None)
                duplicate.pop("person_name", None)
                duplicate["identity_suppressed"] = "duplicate_in_frame"

        for tid, track in list(self._tracks.items()):
            if tid in used_tracks:
                continue
            track.lost += 1
            if track.lost > self.max_lost_frames:
                self._tracks.pop(tid, None)

        if len(self._tracks) > self.max_ids:
            stale = sorted(
                ((tid, tr.lost) for tid, tr in self._tracks.items() if tid not in used_tracks),
                key=lambda x: -x[1],
            )
            for tid, _lost in stale:
                if len(self._tracks) <= self.max_ids:
                    break
                self._tracks.pop(tid, None)

        out.sort(key=lambda f: int(f.get("face_id") or 0))
        self._last_faces = out
        return out
