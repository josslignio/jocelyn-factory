"""Primitives système du noyau V3, sans dépendance sur factory_v2."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from uuid import uuid4


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def read_json(path: Path) -> dict | None:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def ecrire_octets(path: Path, data: bytes) -> None:
    """Publication durable : fichier temporaire unique, fsync, replace, fsync du dossier."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
    try:
        with tmp.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        tmp.unlink(missing_ok=True)


def atomic_write(path: Path, value: dict) -> None:
    ecrire_octets(path, (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode())


@contextlib.contextmanager
def file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / float(1 << 30)


def _get(url: str, headers: dict) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "factory-v3/1", **headers})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode())


QUOTA_CACHE = Path.home() / ".cache" / "factory_v3_quota"  # 03/10 : API Claude en 429 sous la charge → toutes constructions refusées


def quota(provider: str) -> dict:
    """Lecture partagée : < 120 s = cache ; API en échec (429…) = dernière mesure de < 15 min, jamais inventée ; sinon échec."""
    f = QUOTA_CACHE / f"{provider}.json"
    with contextlib.suppress(Exception):
        c = json.loads(f.read_text())
        if time.time() - c["t"] < 120:
            return c["w"]
    with contextlib.suppress(Exception):  # mesure partagée de quota_live (même API) : un seul appel pour toute la factory
        r = json.loads((Path.home() / ".cache" / "factory_quota_live" / f"{provider}.json").read_text())
        if r["r"].get("ok") and time.time() - r["t"] < 120:
            return {k: v["utilisation"] for k, v in r["r"]["windows"].items()}
    try:
        w = _quota_api(provider)
    except Exception:
        if (c := json.loads(f.read_text()) if f.exists() else None) and time.time() - c["t"] < 900:
            return c["w"]
        raise
    QUOTA_CACHE.mkdir(parents=True, exist_ok=True)
    (t := f.with_suffix(".tmp")).write_text(json.dumps({"t": time.time(), "w": w}))
    t.replace(f)
    return w


def _quota_api(provider: str) -> dict:
    """Retourne les fenêtres sous la forme {nom: fraction_utilisée}; l'appelant échoue fermé sur erreur."""
    if provider == "claude":
        raw = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        auth = json.loads(raw)["claudeAiOauth"]
        data = _get("https://api.anthropic.com/api/oauth/usage", {
            "Authorization": f"Bearer {auth['accessToken']}", "anthropic-beta": "oauth-2025-04-20"})
        out = {name: float((data.get(key) or {})["utilization"]) / 100
               for key, name in (("five_hour", "five_hour"), ("seven_day", "seven_day"))
               if (data.get(key) or {}).get("utilization") is not None}
        for limit in data.get("limits") or []:
            model = ((limit.get("scope") or {}).get("model") or {}).get("display_name")
            if limit.get("kind") == "weekly_scoped" and model and limit.get("percent") is not None:
                out[f"weekly_{model.lower()}"] = float(limit["percent"]) / 100
        return out
    if provider == "glm":
        key = os.environ["ZAI_API_KEY"]
        limits = _get("https://api.z.ai/api/monitor/usage/quota/limit", {"Authorization": f"Bearer {key}"})["data"]["limits"]
        return {("five_hour" if x.get("unit") == 3 and x.get("number") == 5 else
                 "weekly" if x.get("unit") == 6 else f"unit{x.get('unit')}x{x.get('number')}"):
                float(x["percentage"]) / 100 for x in limits if x.get("type") == "TOKENS_LIMIT" and x.get("percentage") is not None}  # C07
    raise ValueError(f"fournisseur inconnu : {provider}")
