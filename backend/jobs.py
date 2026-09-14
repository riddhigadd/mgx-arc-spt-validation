"""Thread-safe background jobs with persisted status and step logs."""

from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass
from typing import Any, Callable

from backend.models import Job, JobLogStep, JobStatus, utc_now_iso
from backend.store import SnapshotStore

log = logging.getLogger(__name__)

JobFn = Callable[["JobContext"], Any]


@dataclass
class JobContext:
    """Per-run handle passed to job callables. Does not carry credentials."""

    job_id: str
    kind: str
    target_id: str | None
    _manager: "JobManager"

    def log(self, line: str, style: str = "out") -> JobLogStep:
        return self._manager.append_log(self.job_id, line, style=style)

    def update(self, **fields: Any) -> Job | None:
        return self._manager.update(self.job_id, **fields)

    def run_bounded(self, fn: Callable[[], Any], timeout: float) -> Any:
        """Run ``fn`` with a timeout. The worker thread is not killed on expiry."""
        return self._manager.run_bounded(fn, timeout)


class JobManager:
    """First-pass in-process job runner backed by ``SnapshotStore``."""

    def __init__(
        self,
        store: SnapshotStore,
        *,
        max_workers: int = 4,
        default_timeout: float = 300,
    ) -> None:
        self.store = store
        self.default_timeout = float(default_timeout)
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="mgx-arc-job",
        )
        self._timeout_pool = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="mgx-arc-job-bound",
        )
        self._inflight: dict[str, Any] = {}
        self._closed = False

    def submit(
        self,
        kind: str,
        fn: JobFn,
        *,
        target_id: str | None = None,
        timeout: float | None = None,
        extra: dict[str, Any] | None = None,
        message: str = "Waiting to start…",
    ) -> Job:
        if self._closed:
            raise RuntimeError("JobManager is shut down.")
        job = Job(
            id=str(uuid.uuid4()),
            kind=kind,
            target_id=target_id,
            status=JobStatus.QUEUED,
            message=message,
            extra=dict(extra or {}),
        )
        self.store.save_job(job)
        limit = self.default_timeout if timeout is None else float(timeout)
        with self._lock:
            future = self._executor.submit(self._run, job.id, kind, target_id, fn, limit)
            self._inflight[job.id] = future
            future.add_done_callback(lambda _f, job_id=job.id: self._forget(job_id))
        return job

    def get(self, job_id: str) -> Job | None:
        return self.store.get_job(job_id)

    def list_jobs(self, *, target_id: str | None = None, limit: int = 100) -> list[Job]:
        return self.store.list_jobs(target_id=target_id, limit=limit)

    def logs(self, job_id: str) -> list[JobLogStep]:
        return self.store.list_job_logs(job_id)

    def append_log(self, job_id: str, line: str, style: str = "out") -> JobLogStep:
        return self.store.append_job_log(job_id, line, style=style)

    def update(self, job_id: str, **fields: Any) -> Job | None:
        return self.store.update_job(job_id, **fields)

    def run_bounded(self, fn: Callable[[], Any], timeout: float) -> Any:
        future = self._timeout_pool.submit(fn)
        try:
            return future.result(timeout=timeout)
        except FuturesTimeout as exc:
            raise TimeoutError(f"Bounded command exceeded {timeout:.0f}s.") from exc

    def shutdown(self, wait: bool = True) -> None:
        self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=not wait)
        self._timeout_pool.shutdown(wait=wait, cancel_futures=not wait)

    def _forget(self, job_id: str) -> None:
        with self._lock:
            self._inflight.pop(job_id, None)

    def _run(
        self,
        job_id: str,
        kind: str,
        target_id: str | None,
        fn: JobFn,
        timeout: float,
    ) -> None:
        ctx = JobContext(job_id=job_id, kind=kind, target_id=target_id, _manager=self)
        self.store.update_job(
            job_id,
            status=JobStatus.RUNNING,
            message="Running",
            percent=1,
        )
        try:
            result = self.run_bounded(lambda: fn(ctx), timeout)
            extra = None
            current = self.store.get_job(job_id)
            if current is not None:
                extra = dict(current.extra)
                extra["result"] = _safe_result(result)
            self.store.update_job(
                job_id,
                status=JobStatus.COMPLETE,
                percent=100,
                message="Complete",
                error=None,
                extra=extra,
            )
        except TimeoutError as exc:
            log.warning("Job %s timed out: %s", job_id, exc)
            self.append_log(job_id, str(exc), style="err")
            self.store.update_job(
                job_id,
                status=JobStatus.FAILED,
                error=str(exc),
                message="Timed out",
                extra=_error_extra(self.store.get_job(job_id), str(exc)),
            )
        except Exception as exc:  # noqa: BLE001 - persist any worker failure
            log.exception("Job %s failed", job_id)
            self.append_log(job_id, str(exc), style="err")
            self.store.update_job(
                job_id,
                status=JobStatus.FAILED,
                error=str(exc),
                message="Failed",
                extra=_error_extra(self.store.get_job(job_id), str(exc)),
            )
        finally:
            job = self.store.get_job(job_id)
            if job is not None:
                job.updated_at = utc_now_iso()
                self.store.save_job(job)


def _safe_result(result: Any) -> Any:
    if result is None or isinstance(result, (str, int, float, bool)):
        return result
    if isinstance(result, dict):
        return {str(k): _safe_result(v) for k, v in result.items() if "password" not in str(k).lower()}
    if isinstance(result, (list, tuple)):
        return [_safe_result(v) for v in result]
    return str(result)


def _error_extra(job: Job | None, error: str) -> dict[str, Any] | None:
    if job is None:
        return {"error": error}
    extra = dict(job.extra)
    extra["error"] = error
    return extra
