"""AI 生成的后台任务账本（进程内）。

生成一套卡通表情要 3～4 分钟，工具不能同步等着：先开一个后台线程立刻返回 ``job_id``，
模型告诉主人"在做了"；做完由任务自己落库 / 换到设备，语音 Agent 在线时再让它主动说一句
（见 ``agent_generation_tools``）；主人追问时用 ``generation_status`` 工具查。

账本按进程存：语音链路的工具在 Core 进程执行，文字对话的在 Flask 进程执行，各自查各自的。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any, Callable

logger = logging.getLogger("deskbot-server")

MAX_JOBS = 20
Progress = Callable[[str], None]

_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}
_order: list[str] = []


def _snapshot(job: dict[str, Any]) -> dict[str, Any]:
    out = dict(job)
    out["elapsed_seconds"] = int((out.get("finished_at") or time.time()) - float(out.get("started_at") or time.time()))
    return out


def start_job(
    kind: str,
    fn: Callable[[Progress], dict[str, Any]],
    *,
    summary: str,
    device_id: str = "",
    on_done: Callable[[dict[str, Any]], None] | None = None,
    thread: bool = True,
) -> dict[str, Any]:
    """起一个后台任务：``fn(progress)`` 在线程里跑，返回值作为 ``result``；异常 → ``failed``。

    ``thread=False`` 只给测试用：同步跑完再返回。"""
    job_id = uuid.uuid4().hex[:10]
    job: dict[str, Any] = {
        "job_id": job_id,
        "kind": str(kind),
        "summary": str(summary or "")[:200],
        "device_id": str(device_id or ""),
        "status": "running",
        "progress": "已开始",
        "started_at": time.time(),
        "finished_at": None,
        "result": None,
        "error": None,
    }
    with _lock:
        _jobs[job_id] = job
        _order.append(job_id)
        while len(_order) > MAX_JOBS:
            _jobs.pop(_order.pop(0), None)

    def _run() -> None:
        try:
            result = fn(lambda text: set_progress(job_id, text))
            _finish(job_id, result=result if isinstance(result, dict) else {"value": result})
        except Exception as exc:  # noqa: BLE001 —— 失败原因要能回给主人
            logger.warning("[generation_jobs] %s failed job_id=%s", kind, job_id, exc_info=True)
            _finish(job_id, error=str(exc)[:300] or type(exc).__name__)
        if on_done is not None:
            try:
                on_done(get_job(job_id) or {})
            except Exception:  # noqa: BLE001
                logger.warning("[generation_jobs] on_done failed job_id=%s", job_id, exc_info=True)

    if thread:
        threading.Thread(target=_run, name=f"deskbot-gen-{kind}", daemon=True).start()
    else:
        _run()
    return get_job(job_id) or _snapshot(job)


def set_progress(job_id: str, text: str) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is not None and job["status"] == "running":
            job["progress"] = str(text or "")[:200]


def _finish(job_id: str, *, result: dict[str, Any] | None = None, error: str | None = None) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return
        job["finished_at"] = time.time()
        if error is None:
            job["status"] = "done"
            job["result"] = result or {}
            job["progress"] = "完成"
        else:
            job["status"] = "failed"
            job["error"] = error
            job["progress"] = "失败"


def get_job(job_id: str) -> dict[str, Any] | None:
    with _lock:
        job = _jobs.get(str(job_id or "").strip())
        return _snapshot(job) if job is not None else None


def latest_job(kind: str | None = None) -> dict[str, Any] | None:
    with _lock:
        for job_id in reversed(_order):
            job = _jobs.get(job_id)
            if job is not None and (kind is None or job["kind"] == kind):
                return _snapshot(job)
    return None


def list_jobs() -> list[dict[str, Any]]:
    with _lock:
        return [_snapshot(_jobs[j]) for j in _order if j in _jobs]


def _reset_for_tests() -> None:
    with _lock:
        _jobs.clear()
        _order.clear()


__all__ = ["MAX_JOBS", "get_job", "latest_job", "list_jobs", "set_progress", "start_job"]
