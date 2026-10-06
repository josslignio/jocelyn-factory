"""Phase 3: Worker-side heartbeat — runs inside the worker process.

The heartbeat is independent of factoryd. When factoryd receives SIGKILL,
the worker process (and its heartbeat thread) continues running.

Architecture:
    WorkerHeartbeat runs as a daemon thread inside the worker process.
    It calls WorkerLease.tick_heartbeat() every `interval_s` seconds.
    If the lease disappears or becomes EXPIRED/FENCED, the heartbeat stops.

Proof requirement (destructive):
    SIGKILL factoryd → worker heartbeat_seq continues incrementing
    factoryd B restarts → reads lease → heartbeat_seq > 0 → ADOPT same generation

Usage (inside worker process / AutopilotExecutor):
    hb = WorkerHeartbeat(mission_id, project_dir, interval_s=30)
    hb.start()
    # ... do work ...
    hb.stop()
"""
from __future__ import annotations

import threading
import time
import sys
from pathlib import Path

from .worker_lease import WorkerLease


class WorkerHeartbeat:
    """Daemon thread that renews WorkerLease.heartbeat_seq inside the worker process."""

    def __init__(
        self,
        mission_id: str,
        project_dir: Path,
        interval_s: float = 30.0,
        generation=None,
        fencing_token=None,
    ):
        self.mission_id = mission_id
        self.project_dir = Path(project_dir)
        self.interval_s = interval_s
        # L5_factory_v2_rest_3-015: identify the owner on every tick.
        self.generation = generation
        self.fencing_token = fencing_token
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_seq: int = 0
        self._last_error: Exception | None = None
        self._consecutive_failures: int = 0
        self._lock = threading.Lock()

    def start(self) -> None:
        """Start the heartbeat daemon thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"worker-heartbeat-{self.mission_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the heartbeat thread to stop and wait for it."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=max(5.0, self.interval_s * 2))

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._last_seq

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def last_error(self) -> Exception | None:
        with self._lock:
            return self._last_error

    @property
    def consecutive_failures(self) -> int:
        with self._lock:
            return self._consecutive_failures

    def _run(self) -> None:
        while not self._stop_event.wait(timeout=self.interval_s):
            try:
                updated = WorkerLease.tick_heartbeat(
                    self.mission_id, self.project_dir,
                    generation=self.generation, fencing_token=self.fencing_token)
                if updated is None:
                    # Lease gone or fenced — stop heartbeat.
                    break
                with self._lock:
                    self._last_seq = updated.heartbeat_seq
                    self._last_error = None
                    self._consecutive_failures = 0
            except Exception as exc:
                # Keep retrying, but expose UNKNOWN instead of silently presenting
                # the previous sequence as a healthy observation. Log once per streak.
                with self._lock:
                    self._last_error = exc
                    self._consecutive_failures += 1
                    first_failure = self._consecutive_failures == 1
                if first_failure:
                    print(f"WORKER_HEARTBEAT_UNKNOWN mission={self.mission_id}: "
                          f"{type(exc).__name__}: {str(exc)[:160]}", file=sys.stderr)


