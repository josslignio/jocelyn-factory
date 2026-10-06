"""Single source of truth for the small IO helpers duplicated across factory/ and
factory_v2/: _now, _atomic_write, _read_json, _git.

FACTORY_IO_COMMON_V1 — the strictest semantics found among the pre-migration copies:

  _now            ISO8601 UTC, second precision, "%Y-%m-%dT%H:%M:%SZ" (time.gmtime),
                  the format used by the large majority of the duplicated copies.
  _atomic_write   write to a sibling .tmp file, fsync the file descriptor, os.replace
                  it into place, then fsync the containing directory's fd too (a
                  rename is atomic for the NAME but not durable for the DATA without
                  the directory fsync — this was the strictest of the three copies).
  _read_json      fail-closed: a missing file, unreadable file, corrupt JSON, or a
                  JSON value that is not an object all return None. Never raises for
                  those cases. Callers that want a default write `_read_json(p) or {}`.
  _git            subprocess.run(["git", "-C", repo, *args], capture_output=True,
                  text=True, timeout=timeout). Does not swallow exceptions and does
                  not raise on non-zero exit (the caller inspects .returncode) — a
                  subprocess.TimeoutExpired propagates so callers can decide.

Modules whose previous local copy used a genuinely different contract (extra
parameters, a different return shape, domain-specific validation, or exception
semantics) keep a differently-named local adapter that calls into this module
instead of redefining _now/_atomic_write/_read_json/_git.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import fcntl
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Optional
from uuid import uuid4

DEFAULT_GIT_TIMEOUT = 60


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """The one write-then-rename shape: unique tmp per call, fsync(file), os.replace, fsync(dir)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # L5_rest_1-003 (relecture 2026-09) : un tmp par APPEL. Le suffixe partagé
    # `<path>.tmp` faisait partager le même inode temporaire à deux écritures
    # concurrentes visant le même chemin — l'une remplaçait le tmp de l'autre
    # (FileNotFoundError pour la perdante, ou publication du contenu de
    # l'autre writer). Un nom unique par opération rend chaque paire
    # (écriture, remplacement) indépendante.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    with tmp.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    dfd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dfd)  # rename is atomic for the NAME, not durable for the DATA
    finally:
        os.close(dfd)


def _atomic_write_text(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))


def _atomic_write(path: Path, payload: dict) -> None:
    _atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def _parse_utc(value) -> Optional[_dt.datetime]:
    """Aware UTC datetime from an ISO-8601 string ('Z', any offset, or naive = UTC). None when unreadable."""
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        t = _dt.datetime.fromisoformat(v)
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=_dt.timezone.utc)
    return t.astimezone(_dt.timezone.utc)


def _utc_epoch(value) -> Optional[float]:
    t = _parse_utc(value)
    return None if t is None else t.timestamp()


@contextlib.contextmanager
def _file_lock(lock_path: Path):
    """Blocking exclusive fcntl.flock on `lock_path` (created if absent), released on exit or owner death."""
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield lf
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _read_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # ValueError couvre json.JSONDecodeError ET UnicodeDecodeError (un octet
        # UTF-8 invalide levait hors des exceptions capturées — L5_rest_1-004).
        return None
    return data if isinstance(data, dict) else None


def _git(repo: Path, *args: str, timeout: int = DEFAULT_GIT_TIMEOUT,
         env: Optional[dict] = None) -> subprocess.CompletedProcess:
    # env=None inherits the parent environment (subprocess default) — callers with a
    # genuinely different contract (e.g. resilience's GIT_OPTIONAL_LOCKS=0 checkpoints)
    # pass their env instead of redefining a _git copy.
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=timeout, env=env)
