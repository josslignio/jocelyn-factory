"""quota_observer — merge owner-reported and CLI-observed subscription quotas.

Two independent sources of truth about how much of a flat CLI subscription is
left, and neither is complete on its own:

  * `factory/run/QUOTA_SNAPSHOT.json` (schema `quota-snapshot/v1`) — the owner
    reading Claude Max / Codex / Z.ai's own UI and writing down what they saw.
    Accurate the moment it is written, then goes stale.
  * The CLI's OWN rate-limit telemetry, already extracted by
    `factory_v2.telemetry.adapters` (Claude's `rate_limit_event`, codex's
    `rate_limits` inside `token_count`) and already copied onto each
    dispatched attempt by `factory_v2.launch_gate._project_observed` as
    `quota_scope` / `quota_status` / `quota_utilisation` / `quota_observed_at`
    on `factory_v2.resource_accounting.AttemptAccount`, persisted one JSON line
    per attempt in `<project_dir>/evidence/attempt_accounts.jsonl`.

This module does not parse any provider log itself and does not duplicate
adapters.py's QuotaSnapshot extraction — it reads what the supervisor already
recorded, on both sides, and picks whichever is fresher.

`unknown` never means `unlimited` and never means `refuse`: an engine this
module has no data for is `allowed=True` — the same "unknown is not zero"
posture as resource_accounting.py's `UNKNOWN` sentinel. It also means an
import failure or a missing file must never propagate into route()'s
callers; every public function here is fail-open by construction.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .resource_accounting import UNKNOWN, fold, read_rows
from factory_v2._io import _utc_epoch as _parse_timestamp

SNAPSHOT_SCHEMA = "quota-snapshot/v1"
SNAPSHOT_REL = ("factory", "run", "QUOTA_SNAPSHOT.json")
SNAPSHOT_NAME = "QUOTA_SNAPSHOT.json"


def snapshot_path(run_dir) -> Path:
    """FACTORY_STATE_OUT_OF_REPO_V1 (2026-09-13): resolution order —
    1. FACTORY_RUN_DIR/QUOTA_SNAPSHOT.json when the env var is set (daemon, tests);
    2. <run_dir>/factory/run/QUOTA_SNAPSHOT.json if it exists (older checkouts, hermetic
       fixtures that write under a tmp control root);
    3. ~/Library/Application Support/JocelynFactory/run/QUOTA_SNAPSHOT.json (live default).
    The in-repo copy was removed from the control checkout, so production resolves to 3."""
    import os
    env = os.environ.get("FACTORY_RUN_DIR", "").strip()
    if env:
        return Path(env) / SNAPSHOT_NAME
    legacy = Path(run_dir).joinpath(*SNAPSHOT_REL)
    if legacy.is_file():
        return legacy
    # hermetic tests point the observer at a tmp control root (test-only, guarded by
    # FACTORY_NO_REAL_ENGINE like model_router does): never fall through to the live file
    if os.environ.get("FACTORY_QUOTA_SNAPSHOT_ROOT", "").strip() and os.environ.get("FACTORY_NO_REAL_ENGINE") == "1":
        return legacy
    return Path.home() / "Library" / "Application Support" / "JocelynFactory" / "run" / SNAPSHOT_NAME


ATTEMPT_LEDGER_REL = ("evidence", "attempt_accounts.jsonl")

#: canonical engine id (as stored on AttemptAccount / used by model_router) ->
#: provider bucket used in the snapshot file and in this module's views. All
#: three happen to already share the same name.
PROVIDERS = ("claude", "codex", "glm")

SOURCE_SCREENSHOT = "screenshot"
SOURCE_CLI = "cli"

#: Owner policy (2026-09-12 quota context): refuse new launches at/above this
#: utilisation on ANY observed window; between SLOW_AT and REFUSE_AT, still
#: allowed but with reduced headroom (a caller may choose to throttle/ration).
REFUSE_AT = 0.90
SLOW_AT = 0.75


# ---------------------------------------------------------------------------
# Snapshot (owner screenshots)
# ---------------------------------------------------------------------------

def _empty_snapshot(note: str) -> dict:
    return {"schema": SNAPSHOT_SCHEMA, "providers": {}, "observed": False,
            "source": "", "observed_at": "", "note": note}


API_STALE_S = 600   # 22/09 : le propriétaire ne fournit plus de captures — l'API des fournisseurs est la source


def _refresh_from_api_if_stale(path: Path) -> None:
    """Before any quota decision, re-read the three providers' APIs when the snapshot is older
    than API_STALE_S (a GLM reviewer was launched at 100 % because the file said 2 %). Hermetic
    runs (FACTORY_NO_REAL_ENGINE=1) never touch the network; any failure leaves the file as is."""
    import os
    if os.environ.get("FACTORY_NO_REAL_ENGINE") == "1" or os.environ.get("FACTORY_QUOTA_NO_API") == "1":
        return
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            last = str(data.get("api_last_refreshed_at") or "")
            if last:
                age = (datetime.now(timezone.utc)
                       - datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc))
                if age.total_seconds() < API_STALE_S:
                    return
        from . import quota_live
        quota_live.write_snapshot(quota_live.read_all(), str(path.parents[2]) if len(path.parents) > 2 else None)
    except Exception:
        return


def load_snapshot(run_dir) -> dict:
    """Read `<run_dir>/factory/run/QUOTA_SNAPSHOT.json`.

    `run_dir` is a control/repo root (the same root `factory/run/` hangs off
    of), not the `run/` directory itself. Tolerant of absence, unreadable
    JSON, or an unexpected schema: each returns the same well-shaped "not
    observed" dict rather than raising, so a factory that has never had a
    snapshot written still gets an answer a caller can use unconditionally.
    """
    path = snapshot_path(run_dir)
    _refresh_from_api_if_stale(path)
    if not path.is_file():
        return _empty_snapshot(f"no snapshot at {path}")
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        return _empty_snapshot(f"unreadable snapshot at {path}: {exc}")
    if not isinstance(data, dict):
        return _empty_snapshot(f"snapshot at {path} is not a JSON object")
    if data.get("schema") != SNAPSHOT_SCHEMA:
        return _empty_snapshot(
            f"snapshot at {path} has schema {data.get('schema')!r}, "
            f"expected {SNAPSHOT_SCHEMA!r}")
    data = dict(data)
    data.setdefault("providers", {})
    data.setdefault("observed_at", "")
    data.setdefault("source", "")
    data["observed"] = True
    return data


def refresh_from_cli(run_dir, provider: str, *, utilisation: float,
                     status: str = UNKNOWN, scope: str = UNKNOWN,
                     observed_at: str = "") -> bool:
    """Merge one CLI-observed reading into `<run_dir>/factory/run/QUOTA_SNAPSHOT.json`.

    Companion to `load_snapshot`/`latest_cli_signals`: where those two only
    READ (the owner's screenshot file, and the per-attempt ledger), this is
    the write side a dispatcher can call right after a launch that actually
    carried a provider `rate_limit_event`/`rate_limits` reading — the same
    `QuotaSnapshot` shape `factory_v2.telemetry.adapters` already extracts.

    Writes ONLY the touched provider/window; every other provider and window
    already in the file (including the owner's own screenshot entries) is
    left untouched. The touched window is tagged `"source": "cli"` so a
    reader can tell a live CLI reading apart from an owner screenshot in the
    same file. Creates the file (and `factory/run/`) if absent.

    Best-effort by design: a failure to persist a quota observation must
    never fail the launch that produced it, so every error is swallowed and
    this returns False rather than raising. Returns True on a successful
    write. NOT safe against concurrent writers from independent processes —
    callers with more than one dispatcher touching the same control_root
    need their own serialization; there is none here today.
    """
    try:
        if not isinstance(utilisation, (int, float)) or isinstance(utilisation, bool):
            return False
        path = snapshot_path(run_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        current = load_snapshot(run_dir)
        data = {
            "schema": SNAPSHOT_SCHEMA,
            "observed_at": current.get("observed_at") or observed_at,
            "source": current.get("source") or "owner_screenshots",
            "providers": dict(current.get("providers") or {}),
        }
        pdata = dict(data["providers"].get(provider) or {})
        windows = dict(pdata.get("windows") or {})
        window_key = scope if (scope and scope != UNKNOWN) else "cli_observed"
        windows[window_key] = {
            "utilisation": float(utilisation),
            "resets_at": None,
            "status": status,
            "source": SOURCE_CLI,
            "observed_at": observed_at,
        }
        pdata["windows"] = windows
        data["providers"][provider] = pdata
        data["cli_last_refreshed_at"] = observed_at
        tmp_path = path.with_name(path.name + ".tmp")
        tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False,
                                       sort_keys=True), encoding="utf-8")
        tmp_path.replace(path)
        return True
    except Exception:                                        # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# CLI-observed signals (already extracted, already persisted per attempt)
# ---------------------------------------------------------------------------

#: AttemptAccount.engine (canonical, post model_router.normalize_engine_id) ->
#: provider bucket. Identical today; kept as an explicit map rather than an
#: assumption so a future provider rename here does not silently misfile.
_ENGINE_TO_PROVIDER = {"claude": "claude", "codex": "codex", "glm": "glm"}


def _numeric(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def latest_cli_signals(project_dirs, control_root=None) -> dict:
    """Latest provider-observed quota reading per canonical engine.

    Folds each project's `evidence/attempt_accounts.jsonl` ledger
    (`factory_v2.resource_accounting.read_rows` + `.fold`) and keeps, per
    provider, the row with the latest non-empty `quota_observed_at` whose
    `quota_utilisation` is an actual number (never UNKNOWN/None). This
    function parses no provider log itself — the numbers already exist in the
    ledger because `factory_v2.launch_gate._project_observed` already copied
    them out of `factory_v2.telemetry.adapters`' QuotaSnapshot (Claude's
    `rate_limit_event`, codex's `rate_limits`) at launch-observation time.

    `control_root` is accepted for signature symmetry with `load_snapshot`
    and `merged_view`'s other callers (model_router resolves both from the
    same repo root); it is not itself scanned here, since `project_dirs`
    already names the exact ledgers to read. Reserved for a future default
    project-discovery fallback when `project_dirs` is empty.

    Returns {provider: {"utilisation": float, "resets_at": None, "status":
    str, "scope": str, "observed_at": str, "source": "cli"}} — only for
    providers with at least one usable row. `resets_at` is always None today:
    the per-attempt ledger does not persist a reset timestamp (only the
    provider's utilisation/status/scope/observed_at are copied), so this is
    left explicitly absent rather than invented.
    """
    del control_root  # not scanned directly; see docstring
    out: dict = {}
    for project_dir in (project_dirs or []):
        ledger = Path(project_dir).joinpath(*ATTEMPT_LEDGER_REL)
        rows = fold(read_rows(ledger))
        for row in rows:
            if not isinstance(row, dict):
                continue
            engine = row.get("engine") or ""
            provider = _ENGINE_TO_PROVIDER.get(engine)
            if provider is None:
                continue
            utilisation = _numeric(row.get("quota_utilisation", UNKNOWN))
            if utilisation is None:
                continue
            observed_at = row.get("quota_observed_at") or ""
            scope = row.get("quota_scope") or UNKNOWN
            entry = {
                "utilisation": utilisation,
                "resets_at": None,
                "status": row.get("quota_status") or UNKNOWN,
                "scope": scope,
                "observed_at": observed_at,
                "source": SOURCE_CLI,
            }
            # L4_resilience_review-007: the latest signal is kept PER WINDOW
            # (`windows[scope]`) — a fresh five_hour reading no longer erases a
            # saturated seven_day reading of the same provider. The top-level
            # fields stay the latest overall for callers that read one signal.
            prior = out.get(provider)
            windows = dict((prior or {}).get("windows") or {})
            prior_w = windows.get(scope)
            # instants compared as UTC epochs (mixed offsets), never as ISO strings
            seen = _parse_timestamp(observed_at) or 0.0
            if prior_w is None or (_parse_timestamp(prior_w.get("observed_at")) or 0.0) < seen:
                windows[scope] = entry
            if prior is not None and (_parse_timestamp(prior.get("observed_at")) or 0.0) >= seen:
                prior["windows"] = windows
                continue
            out[provider] = {**entry, "windows": windows}
    return out


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def merged_view(snapshot: dict, cli_signals: dict, *, now=None) -> dict:
    """{provider: {window: {utilisation, resets_at, source, observed_at,
    freshness_s}}}, taking whichever source is fresher for each window.

    `snapshot` is `load_snapshot()`'s return; `cli_signals` is
    `latest_cli_signals()`'s return. The screenshot names one window per
    provider window key (e.g. "session_5h", "weekly"); the CLI signal names
    one window via its own `quota_scope` (falling back to "cli_observed" when
    the scope itself is unknown). When BOTH sources report the exact same
    window key for a provider, the fresher `observed_at` wins outright; when
    they name different windows (the common case today — the owner's
    screenshot windows and a provider's own quota_scope strings rarely match
    verbatim), both are kept side by side so a caller scanning for "any
    window over threshold" sees everything either source has observed.

    `now` (datetime, tz-aware or naive-UTC) is the freshness_s reference
    point; defaults to `datetime.now(timezone.utc)`. `freshness_s` is None
    when a window's `observed_at` cannot be parsed as a timestamp — an
    unparseable/absent observation time is not "very stale", it is unknown.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    now_epoch = now.timestamp() if hasattr(now, "timestamp") else None

    snapshot = snapshot or {}
    providers_in = snapshot.get("providers") or {}
    snapshot_observed_at = snapshot.get("observed_at") or ""

    out: dict = {p: {} for p in PROVIDERS}

    for provider, pdata in providers_in.items():
        if provider not in out:
            out[provider] = {}
        # Owner order, time-boxed: "spend what is left before the reset" (2026-09-14,
        # GLM at 7 % the day before its reset). Carried as a non-window key so the
        # allowance can raise its refuse line until `until`, then falls back to the
        # normal 0.90 line by itself — the observed utilisation is never altered.
        order = (pdata or {}).get("owner_order") or {}
        if isinstance(order, dict) and order.get("spend_until"):
            out[provider]["_owner_order"] = dict(order)
        windows = (pdata or {}).get("windows") or {}
        for window, wdata in windows.items():
            util = _numeric((wdata or {}).get("utilisation"))
            if util is None:
                continue
            # L4_resilience_review-006: a window refreshed by `refresh_from_cli`
            # keeps its own source and observed_at; the snapshot's global
            # metadata is only the default for genuine screenshot windows.
            out[provider][window] = {
                "utilisation": util,
                "resets_at": (wdata or {}).get("resets_at"),
                "source": (wdata or {}).get("source") or SOURCE_SCREENSHOT,
                "observed_at": (wdata or {}).get("observed_at") or snapshot_observed_at,
            }

    for provider, sig in (cli_signals or {}).items():
        if provider not in out:
            out[provider] = {}
        # L4_resilience_review-007: merge EVERY observed window of the provider,
        # not only the latest signal (legacy single-signal dicts still work).
        per_window = sig.get("windows") or {(sig.get("scope") or UNKNOWN): sig}
        for _scope, wsig in per_window.items():
            window = wsig.get("scope") or _scope or UNKNOWN
            if window == UNKNOWN:
                window = "cli_observed"
            existing = out[provider].get(window)
            cli_entry = {
                "utilisation": wsig.get("utilisation"),
                "resets_at": wsig.get("resets_at"),
                "source": SOURCE_CLI,
                "observed_at": wsig.get("observed_at") or "",
            }
            if existing is None:
                out[provider][window] = cli_entry
            else:
                existing_ts = _parse_timestamp(existing.get("observed_at") or "")
                cli_ts = _parse_timestamp(cli_entry["observed_at"])
                # A source with no parseable timestamp never displaces one that
                # has one; between two parseable timestamps the later wins; two
                # unparseable timestamps keep whichever was already there (the
                # screenshot merge pass above ran first).
                if cli_ts is not None and (existing_ts is None or cli_ts > existing_ts):
                    out[provider][window] = cli_entry

    for provider, windows in out.items():
        for window, entry in windows.items():
            ts = _parse_timestamp(entry.get("observed_at") or "")
            entry["freshness_s"] = (now_epoch - ts) if (ts is not None and now_epoch is not None) else None

    return out


# ---------------------------------------------------------------------------
# Allowance decision
# ---------------------------------------------------------------------------

#: how long a dispatcher-observed quota cut without a known reset instant
#: keeps refusing the provider (Claude session windows are 5 h; a cut is
#: usually observed late in the window)
OBSERVED_CUT_TTL_S = 90 * 60


def engine_allowance(provider: str, view: dict, policy: dict | None = None) -> dict:
    """{"allowed": bool, "reason": str, "headroom": float | None}.

    `view` is `merged_view(...)[provider]` — a mapping of window -> {utilisation,
    ...}. Refuses (allowed=False) when ANY window's utilisation is >= the
    refuse threshold (default REFUSE_AT=0.90); allowed but with reduced
    headroom when any window is >= the slow threshold (default SLOW_AT=0.75);
    otherwise allowed with full headroom. A provider with no numeric window at
    all is allowed with headroom=None ("unknown" is never "exhausted").

    `policy` may override {"refuse_at": float, "slow_at": float}.
    """
    policy = policy or {}
    refuse_at = policy.get("refuse_at", REFUSE_AT)
    slow_at = policy.get("slow_at", SLOW_AT)

    windows = dict(view or {})
    order = windows.pop("_owner_order", None)
    order_note = ""
    if isinstance(order, dict):
        try:
            until = datetime.fromisoformat(str(order.get("spend_until")).replace("Z", "+00:00"))
            if datetime.now(timezone.utc) < until:
                refuse_at = max(refuse_at, float(order.get("refuse_at", 0.99)))
                order_note = f" [ordre propriétaire jusqu'à {order.get('spend_until')}: {order.get('reason', '')}]"
        except (TypeError, ValueError):
            pass
    # 2026-09-18: an "observed_cut" window (written by the dispatcher on a
    # QUOTA_CUT, utilisation=1.0, no resets_at) must EXPIRE — a 5-hour Claude
    # session limit that reset at 15:50 would otherwise refuse Claude forever.
    # Expiry: `resets_at` when known, else OBSERVED_CUT_TTL_S after observed_at.
    _now_e = datetime.now(timezone.utc).timestamp()
    _live: dict = {}
    for w, e in windows.items():
        if not isinstance(e, dict):
            continue
        # 19/09: a screenshot window whose `resets_at` has passed is stale too
        # (the weekly limit reset at 12:51Z; 0.89 must not linger) — expire it
        # like a CLI reading; the next screenshot/CLI reading replaces it.
        _ra_any = None
        try:
            _ra_any = datetime.fromisoformat(str(e.get("resets_at") or "").replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            _ra_any = None
        if _ra_any is not None and _now_e >= _ra_any and w not in ("observed_cut",):
            continue                              # window reset since the reading
        if w == "observed_cut" or str(e.get("source") or "") == "cli":
            _exp = None
            for _k in ("resets_at", "observed_at"):
                try:
                    _t = datetime.fromisoformat(str(e.get(_k) or "").replace("Z", "+00:00")).timestamp()
                except (TypeError, ValueError):
                    continue
                # a CLI reading of a 5-hour window without resets_at cannot be
                # older than that window (a 0.93 read on the 18th refused Claude
                # a day later)
                _ttl = OBSERVED_CUT_TTL_S if w == "observed_cut" else max(OBSERVED_CUT_TTL_S, 5 * 3600)
                _exp = _t if _k == "resets_at" else _t + _ttl
                break
            if _exp is not None and _now_e >= _exp:
                continue                          # expired reading
        _live[w] = e
    windows = _live
    numeric = [(w, e.get("utilisation")) for w, e in windows.items()
              if isinstance(e, dict) and isinstance(e.get("utilisation"), (int, float))
              and not isinstance(e.get("utilisation"), bool)]
    if not numeric:
        return {"allowed": True, "reason": f"{provider}: aucune donnée de quota "
                "(ni screenshot, ni CLI) — inconnu n'est jamais un refus",
                "headroom": None}

    window, util = max(numeric, key=lambda item: item[1])
    headroom = round(1.0 - util, 6)

    if util >= refuse_at:
        return {"allowed": False,
                "reason": (f"ENGINE_QUOTA_EXHAUSTED: {provider} fenêtre "
                          f"{window!r} utilisation={util:.2f} >= {refuse_at:.2f}"),
                "headroom": headroom}
    if util >= slow_at:
        return {"allowed": True,
                "reason": (f"{provider} fenêtre {window!r} utilisation={util:.2f} "
                          f">= {slow_at:.2f} — ralentir (marge faible){order_note}"),
                "headroom": headroom}
    return {"allowed": True,
            "reason": f"{provider} fenêtre {window!r} utilisation={util:.2f} — ok{order_note}",
            "headroom": headroom}


# ---------------------------------------------------------------------------
# CLI (français — lu par le propriétaire et par l'orchestrateur)
# ---------------------------------------------------------------------------

def _default_control_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _discover_project_dirs(control_root: Path) -> list:
    projects_root = Path(control_root) / "projects"
    if not projects_root.is_dir():
        return []
    return sorted(p for p in projects_root.iterdir()
                 if p.is_dir() and (p / "evidence").is_dir())


def build_report(control_root=None, *, now=None) -> dict:
    control_root = Path(control_root) if control_root else _default_control_root()
    snapshot = load_snapshot(control_root)
    cli_signals = latest_cli_signals(_discover_project_dirs(control_root),
                                     control_root=control_root)
    view = merged_view(snapshot, cli_signals, now=now)
    allowances = {p: engine_allowance(p, view.get(p, {})) for p in PROVIDERS}
    return {"control_root": str(control_root), "snapshot_observed": snapshot.get("observed", False),
            "snapshot_note": snapshot.get("note", ""), "view": view, "allowances": allowances}


def _fmt_util(u) -> str:
    return f"{u * 100:5.1f}%" if isinstance(u, (int, float)) else "  ??  "


def format_report(report: dict) -> str:
    lines = ["=== Observateur de quota — état par provider ==="]
    if not report.get("snapshot_observed"):
        lines.append(f"(pas de snapshot propriétaire: {report.get('snapshot_note', '')})")
    for provider in PROVIDERS:
        windows = report["view"].get(provider) or {}
        allowance = report["allowances"].get(provider) or {}
        lines.append(f"\n[{provider}]")
        if not windows:
            lines.append("  aucune donnée (ni screenshot, ni CLI)")
        for window in sorted(windows):
            w = windows[window]
            fresh = w.get("freshness_s")
            fresh_s = f"{int(fresh)}s" if isinstance(fresh, (int, float)) else "?"
            lines.append(
                f"  fenêtre={window:<14s} utilisation={_fmt_util(w.get('utilisation'))} "
                f"source={w.get('source', '?'):<10s} observé_il_y_a={fresh_s}")
        verdict = "AUTORISÉ" if allowance.get("allowed") else "REFUSÉ"
        headroom = allowance.get("headroom")
        headroom_s = f"{headroom * 100:.0f}%" if isinstance(headroom, (int, float)) else "?"
        lines.append(f"  -> {verdict}  marge={headroom_s}  ({allowance.get('reason', '')})")
    return "\n".join(lines)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Observateur de quota (Claude/Codex/GLM)")
    ap.add_argument("--control-root", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    report = build_report(args.control_root)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
