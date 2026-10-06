#!/usr/bin/env python3
"""Quotas EN DIRECT des trois fournisseurs — lus aux mêmes sources que leurs CLI, jamais estimés.

  claude : GET https://api.anthropic.com/api/oauth/usage  (jeton OAuth de Claude Code, trousseau macOS
           « Claude Code-credentials ») → fenêtres 5 h et 7 jours, % utilisé, date de reset.
  codex  : GET https://chatgpt.com/backend-api/wham/usage (jetons de ~/.codex/auth.json) → fenêtre
           principale (hebdo), % utilisé, reset ; fenêtre secondaire (5 h) quand le plan en a une.
  glm    : GET https://api.z.ai/api/monitor/usage/quota/limit (clé zai-coding-plan d'opencode) →
           TOKENS_LIMIT 5 h et hebdo, % utilisé, reset.

`python3 factory_v2/quota_live.py` affiche le tableau ; `--write` fusionne chaque fenêtre dans
QUOTA_SNAPSHOT.json (source "api", horodatée) pour que le routage et le garde-quota lisent la vérité.
Aucune estimation : un fournisseur injoignable est affiché INJOIGNABLE, pas deviné.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BKK = _dt.timezone(_dt.timedelta(hours=7))


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get(url: str, headers: dict, timeout: float = 20) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "factory-quota-live/1", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _iso(ts) -> str | None:
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return _dt.datetime.fromtimestamp(ts / (1000 if ts > 1e11 else 1), _dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(ts)[:19] + "Z" if "T" in str(ts) else str(ts)


def claude() -> dict:
    tok = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                         capture_output=True, text=True, timeout=10).stdout.strip()
    cred = json.loads(tok)["claudeAiOauth"]
    d = _get("https://api.anthropic.com/api/oauth/usage",
             {"Authorization": f"Bearer {cred['accessToken']}", "anthropic-beta": "oauth-2025-04-20"})
    out = {"plan": cred.get("subscriptionType") or cred.get("rateLimitTier"), "windows": {}}
    for key, name in (("five_hour", "five_hour"), ("seven_day", "seven_day")):
        w = d.get(key) or {}
        if w.get("utilization") is not None:
            out["windows"][name] = {"utilisation": float(w["utilization"]) / 100.0, "resets_at": _iso(w.get("resets_at"))}
    for lim in d.get("limits") or []:      # limite hebdo propre à un modèle (ex. Fable)
        scope = ((lim.get("scope") or {}).get("model") or {}).get("display_name")
        if lim.get("percent") is not None and lim.get("kind") == "weekly_scoped" and scope:
            out["windows"][f"weekly_{scope.lower()}"] = {
                "utilisation": float(lim["percent"]) / 100.0, "resets_at": _iso(lim.get("resets_at"))}
    return out


def codex() -> dict:
    a = json.loads((Path.home() / ".codex" / "auth.json").read_text())["tokens"]
    d = _get("https://chatgpt.com/backend-api/wham/usage",
             {"Authorization": f"Bearer {a['access_token']}", "ChatGPT-Account-Id": a["account_id"]})
    rl = d.get("rate_limit") or {}
    out = {"plan": d.get("plan_type"), "windows": {}, "allowed": rl.get("allowed")}
    for key, w in (("primary_window", rl.get("primary_window")), ("secondary_window", rl.get("secondary_window"))):
        if w:
            secs = int(w.get("limit_window_seconds") or 0)
            name = "weekly" if secs >= 6 * 86400 else ("five_hour" if secs <= 6 * 3600 else f"{secs // 3600}h")
            out["windows"][name] = {"utilisation": float(w.get("used_percent") or 0) / 100.0,
                                    "resets_at": _iso(w.get("reset_at"))}
    return out


def glm() -> dict:
    auth = json.loads((Path.home() / ".local" / "share" / "opencode" / "auth.json").read_text())
    key = (auth.get("zai-coding-plan") or auth.get("zai") or {}).get("key")
    d = _get("https://api.z.ai/api/monitor/usage/quota/limit", {"Authorization": f"Bearer {key}"})["data"]
    out = {"plan": f"GLM Coding {d.get('level')}", "windows": {}}
    for lim in d.get("limits") or []:
        if lim.get("type") != "TOKENS_LIMIT":
            continue
        unit, n = lim.get("unit"), lim.get("number")
        name = "five_hour" if (unit == 3 and n == 5) else ("weekly" if unit == 6 else f"unit{unit}x{n}")
        out["windows"][name] = {"utilisation": float(lim.get("percentage") or 0) / 100.0,
                                "resets_at": _iso(lim.get("nextResetTime"))}
    return out


def read_all() -> dict:
    res = {}
    for name, fn in (("claude", claude), ("codex", codex), ("glm", glm)):
        try:
            res[name] = {"ok": True, "observed_at": _now(), **fn()}
        except Exception as exc:  # INJOIGNABLE = affiché, jamais deviné
            res[name] = {"ok": False, "observed_at": _now(), "error": f"{type(exc).__name__}: {exc}"[:200]}
    return res


def _fmt_reset(iso: str | None) -> str:
    if not iso:
        return "?"
    try:
        t = _dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.UTC)
    except ValueError:
        return iso
    left = t - _dt.datetime.now(_dt.UTC)
    h = int(left.total_seconds() // 3600)
    return f"{t.astimezone(BKK).strftime('%a %d/%m %H:%M')} Bangkok (dans {h // 24} j {h % 24} h)"


def table(res: dict) -> str:
    lines = [f"QUOTAS EN DIRECT — lu à {_dt.datetime.now(BKK).strftime('%d/%m %H:%M')} Bangkok (sources : API des fournisseurs)"]
    for p in ("claude", "codex", "glm"):
        r = res[p]
        if not r["ok"]:
            lines.append(f"  {p:<7} INJOIGNABLE — {r['error']}")
            continue
        lines.append(f"  {p:<7} plan={r.get('plan')}")
        for w, v in r["windows"].items():
            used = v["utilisation"] * 100
            lines.append(f"      {w:<11} utilisé {used:5.1f} %   reste {100 - used:5.1f} %   reset {_fmt_reset(v.get('resets_at'))}")
    return "\n".join(lines)


def write_snapshot(res: dict, run_dir=None) -> Path:
    from factory_v2.quota_observer import load_snapshot, snapshot_path
    import os
    run_dir = run_dir or str(Path(__file__).resolve().parents[1])
    path = snapshot_path(run_dir)
    os.environ["FACTORY_QUOTA_NO_API"] = "1"      # pas de rafraîchissement récursif pendant l'écriture
    try:
        cur = load_snapshot(run_dir)
    finally:
        os.environ.pop("FACTORY_QUOTA_NO_API", None)
    data = {"schema": cur.get("schema") or "quota-snapshot/v1", "observed_at": cur.get("observed_at") or _now(),
            "source": cur.get("source") or "owner_screenshots", "providers": dict(cur.get("providers") or {})}
    for p, r in res.items():
        if not r["ok"]:
            continue
        pdata = dict(data["providers"].get(p) or {})
        # l'API est la vérité : les fenêtres capturées/estimées plus anciennes du même fournisseur sont retirées
        # (une vieille fenêtre « EXHAUSTED » de capture bloquerait un moteur en réalité disponible)
        windows = {}
        for w, v in r["windows"].items():
            u = v["utilisation"]
            windows[w] = {"utilisation": u, "resets_at": v.get("resets_at"), "source": "api",
                          "observed_at": r["observed_at"],
                          "status": "EXHAUSTED" if u >= 1.0 else ("NEAR_LIMIT" if u >= 0.9 else "OK")}
        pdata["windows"] = windows
        pdata["plan"] = r.get("plan") or pdata.get("plan")
        data["providers"][p] = pdata
    data["api_last_refreshed_at"] = _now()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    return path


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", action="store_true", help="fusionner dans QUOTA_SNAPSHOT.json")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    res = read_all()
    print(json.dumps(res, ensure_ascii=False, indent=1) if a.json else table(res))
    if a.write:
        print(f"→ écrit dans {write_snapshot(res)}")
    return 0 if all(r["ok"] for r in res.values()) else 2


if __name__ == "__main__":
    sys.exit(main())
