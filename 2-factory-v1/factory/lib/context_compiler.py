"""CTO004 — ContextCompiler: builds the minimal relevant CTO context for a mission.

Read-only with respect to the CTO source files (CTO_STATE.json, CTO_STRATEGY.md,
CTO_ARCHITECTURE.json, CTO_DECISIONS.jsonl, CTO_LESSONS.jsonl,
CTO_MODEL_PERFORMANCE.json, candidate_registry.json) — this module never mutates
them. The only file it owns and writes is its own version cache
(CTO_CONTEXT_VERSION_CACHE.json), which is bookkeeping for `context_version`, not
one of the sources being compiled into context.

Selection is deliberate, not dump-everything: global_immutable and blockers are
always present (short, cheap); architecture/adrs/lessons are included only when
`component` or `touched_paths` narrows the mission down to something they can be
matched against.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Optional

from context_package import ContextPackage
from factory_v2._io import _read_json, _now, _now as _now_iso

logger = logging.getLogger(__name__)

AGENT_ID = "context_compiler/v1"

CTO_DIR_REL = "factory/cto"
APP007_RECEIPTS_DIR_REL = "factory/campaigns/app007/evidence"
CANDIDATE_REGISTRY_REL = "factory/projects/factory-control/evidence/factory-v1/candidate_registry.json"

# The version cache is bookkeeping the compiler OWNS, but it must never live
# inside repo_root: repo_root is a live mission repo (possibly mid-merge, mid-diff),
# and dropping a file into its working tree there would pollute `git status` for
# the mission and could be swept up by the Factory's own snapshot/capture. Keyed
# by repo_root so distinct repos (or distinct tmp_path fixtures in tests) never
# collide.
_VERSION_CACHE_BASE = Path(tempfile.gettempdir()) / "factory-context-version-cache"

_PRINCIPLE_RE = re.compile(r"^###\s*\d+\.\s*(.+)$", flags=re.MULTILINE)


def _read_jsonl(path: Path) -> list:
    out: list = []
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return out
    except OSError as exc:
        logger.warning("context_compiler: cannot read %s: %s", path, exc)
        return out
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            logger.warning("context_compiler: skipping malformed line %d in %s: %s",
                            lineno, path, exc)
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError as exc:
        logger.warning("context_compiler: cannot read %s: %s", path, exc)
        return ""


def _extract_principle_titles(strategy_text: str) -> list:
    return _PRINCIPLE_RE.findall(strategy_text)


def _normalize(s: str) -> str:
    return s.lower().replace("_", " ").replace("-", " ")


def _entry_matches(entry: dict, component: Optional[str], touched_paths: Optional[list]) -> bool:
    """True when `entry` (an ADR / lesson / architecture-component dict) is relevant
    to this mission. Structured fields win when present (`component`, `tags`,
    `paths`/`touched_paths`); otherwise falls back to a normalized substring match
    against the entry's own text fields — real CTO_DECISIONS.jsonl/CTO_LESSONS.jsonl
    entries predate this schema and carry no structured tags yet.
    """
    if not component and not touched_paths:
        return False

    if component:
        if entry.get("component") == component:
            return True
        if component in (entry.get("tags") or []):
            return True

    if touched_paths:
        entry_paths = entry.get("paths") or entry.get("touched_paths") or []
        for ep in entry_paths:
            for tp in touched_paths:
                if ep in tp or tp in ep:
                    return True

    text = _normalize(" ".join(
        str(v) for v in entry.values() if isinstance(v, (str, int, float))
    ))
    if component and len(component) >= 3 and _normalize(component) in text:
        return True
    if touched_paths:
        for tp in touched_paths:
            stem = Path(tp).stem
            if stem and len(stem) >= 3 and _normalize(stem) in text:
                return True
    return False


class ContextCompiler:
    def __init__(self, repo_root: Path, project_dir: "Path | None" = None):
        self.repo_root = Path(repo_root)
        self.cto_dir = self.repo_root / CTO_DIR_REL
        # P6 fix: V2 authoritative path. When project_dir is set, _strategy_text()
        # reads from project_dir/STRATEGY.md and FAILS HARD if missing.
        # STALE_CTO_CONTEXT_DISPATCH = 0, DISPATCH_WITHOUT_FRESH_CANONICAL_PROJECT_CONTEXT = 0.
        # The V1 fallback (cto_dir/CTO_STRATEGY.md) is ONLY reachable when project_dir=None
        # — i.e., from the legacy V1 execution path (autopilot.py without --project-dir).
        self._project_dir = Path(project_dir) if project_dir else None

    # ------------------------------------------------------------------ sources
    def _state(self) -> dict:
        return _read_json(self.cto_dir / "CTO_STATE.json") or {}

    def _strategy_text(self) -> str:
        if self._project_dir is not None:
            strategy_path = self._project_dir / "STRATEGY.md"
            if not strategy_path.is_file():
                raise FileNotFoundError(
                    f"V2 canonical strategy missing at {strategy_path}. "
                    "Cannot dispatch without fresh canonical project context. "
                    "DISPATCH_WITHOUT_FRESH_CANONICAL_PROJECT_CONTEXT invariant violated."
                )
            return strategy_path.read_text(encoding="utf-8")
        return _read_text(self.cto_dir / "CTO_STRATEGY.md")  # V1 legacy path only

    def _spec_text(self) -> str:
        """V2 path: SPEC.md fail-closed. V1 path: returns empty string (no spec concept)."""
        if self._project_dir is not None:
            spec_path = self._project_dir / "SPEC.md"
            if not spec_path.is_file():
                raise FileNotFoundError(
                    f"V2 canonical spec missing at {spec_path}. "
                    "DISPATCH_WITHOUT_FRESH_CANONICAL_PROJECT_CONTEXT invariant violated."
                )
            return spec_path.read_text(encoding="utf-8")
        return ""

    def _architecture(self) -> dict:
        return _read_json(self.cto_dir / "CTO_ARCHITECTURE.json") or {}

    def _decisions(self) -> list:
        return _read_jsonl(self.cto_dir / "CTO_DECISIONS.jsonl")

    def _lessons(self) -> list:
        return _read_jsonl(self.cto_dir / "CTO_LESSONS.jsonl")

    def _model_performance(self) -> dict:
        return _read_json(self.cto_dir / "CTO_MODEL_PERFORMANCE.json") or {}

    def _candidate_registry_path(self) -> Path:
        """FACTORY_STATE_OUT_OF_REPO_V1: candidate_registry.json is daemon runtime
        state, not repo state — it moved to the run-dir. FACTORY_CANDIDATE_REGISTRY_PATH
        (matching factoryd.py) wins outright; otherwise prefer the legacy in-repo path
        IFF it still exists on disk (pre-migration repos, and test fixtures that build
        a fake repo_root tree), falling back to the real run-dir location."""
        override = os.environ.get("FACTORY_CANDIDATE_REGISTRY_PATH", "").strip()
        if override:
            return Path(override)
        run_dir = (os.environ.get("FACTORY_RUN_DIR", "").strip()
                   or str(Path.home() / "Library/Application Support/JocelynFactory/run"))
        runtime = Path(run_dir) / "projects" / "factory-control" / "evidence" / "factory-v1" / "candidate_registry.json"
        if os.environ.get("FACTORY_RUN_DIR", "").strip() or runtime.exists():
            return runtime
        return self.repo_root / CANDIDATE_REGISTRY_REL

    def _candidate_registry(self) -> dict:
        return _read_json(self._candidate_registry_path()) or {}

    def _source_files(self) -> list:
        sources = [
            self.cto_dir / "CTO_STATE.json",
            self.cto_dir / "CTO_ARCHITECTURE.json",
            self.cto_dir / "CTO_DECISIONS.jsonl",
            self.cto_dir / "CTO_LESSONS.jsonl",
            self.cto_dir / "CTO_MODEL_PERFORMANCE.json",
            self._candidate_registry_path(),
        ]
        if self._project_dir is not None:
            sources.extend(self._project_dir / name
                           for name in ("STRATEGY.md", "SPEC.md", "ROADMAP.yaml"))
        else:
            sources.append(self.cto_dir / "CTO_STRATEGY.md")
        return sources

    def _version_cache_path(self) -> Path:
        key = hashlib.sha256(str(self.repo_root.resolve()).encode("utf-8")).hexdigest()[:16]
        return _VERSION_CACHE_BASE / f"{key}.json"

    def _context_version(self) -> int:
        """Monotonic counter, never random/timestamped. A SHA256 over the raw
        bytes of every source file (missing files contribute nothing) is compared
        against a cache this compiler owns; version increments only when that
        hash actually changes.
        """
        h = hashlib.sha256()
        for p in sorted(self._source_files()):
            try:
                h.update(p.read_bytes())
            except OSError:
                continue
        current_hash = h.hexdigest()

        cache_path = self._version_cache_path()
        cache = _read_json(cache_path) or {}
        if cache.get("source_hash") == current_hash and "version" in cache:
            return int(cache["version"])

        version = int(cache.get("version", 0)) + 1
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"source_hash": current_hash, "version": version}),
                            encoding="utf-8")
            os.replace(tmp, cache_path)
        except OSError as exc:
            logger.warning("context_compiler: cannot persist version cache %s: %s",
                            cache_path, exc)
        return version

    # ------------------------------------------------------------------ compile
    def compile(
        self,
        mission_id: str,
        component: Optional[str] = None,
        touched_paths: Optional[list] = None,
        dependency_mission_ids: Optional[list] = None,
        current_milestone: Optional[str] = None,
        contract_id: Optional[str] = None,
    ) -> ContextPackage:
        sources: list = []

        state = self._state()
        sources.append("CTO_STATE.json")

        strategy_text = self._strategy_text()
        if strategy_text:
            sources.append("STRATEGY.md" if self._project_dir else "CTO_STRATEGY.md")

        global_immutable = {
            "north_stars": state.get("north_stars", {}),
            "principles": _extract_principle_titles(strategy_text),
        }

        architecture_list: list = []
        adrs: list = []
        lessons: list = []

        if component or touched_paths:
            arch = self._architecture()
            if arch:
                sources.append("CTO_ARCHITECTURE.json")
            for domain, components in arch.items():
                if not isinstance(components, dict):
                    continue
                for name, meta in components.items():
                    if not isinstance(meta, dict):
                        continue
                    entry = {"id": name, "domain": domain, **meta}
                    if _entry_matches(entry, component, touched_paths):
                        architecture_list.append(entry)

            for d in self._decisions():
                if _entry_matches(d, component, touched_paths):
                    adrs.append(d)
                    if d.get("id"):
                        sources.append(d["id"])

            for l in self._lessons():
                if _entry_matches(l, component, touched_paths):
                    lessons.append(l)
                    if l.get("id"):
                        sources.append(l["id"])

        model_performance = self._model_performance()
        if model_performance:
            sources.append("CTO_MODEL_PERFORMANCE.json")

        dependency_receipts: list = []
        if dependency_mission_ids:
            registry = self._candidate_registry()
            candidates = registry.get("candidates", {}) if isinstance(registry, dict) else {}
            for dep_id in dependency_mission_ids:
                receipt_path = (self.repo_root / APP007_RECEIPTS_DIR_REL / dep_id
                                / f"receipt_{dep_id}.json")
                receipt = _read_json(receipt_path)
                entry: dict = {"mission_id": dep_id, "receipt": receipt}
                if receipt is None:
                    entry["note"] = "RECEIPT_NOT_FOUND"
                else:
                    sources.append(f"receipt_{dep_id}")
                if dep_id in candidates:
                    entry["candidate_registry_entry"] = candidates[dep_id]
                dependency_receipts.append(entry)

        blockers = [
            r for r in state.get("strategic_risks", [])
            if isinstance(r, dict) and r.get("status", "ACTIVE") != "RESOLVED"
        ]

        # V2 canonical context — only populated when project_dir is set (authoritative path).
        # Includes full authority identity so workers can prove freshness end-to-end.
        canonical_ctx: dict = {}
        if self._project_dir is not None:
            spec_text = self._spec_text()
            roadmap_text = ""
            try:
                roadmap_path = Path(self._project_dir) / "ROADMAP.yaml"
                if roadmap_path.is_file():
                    roadmap_text = roadmap_path.read_text(encoding="utf-8")
            except Exception:
                pass
            canonical_ctx = {
                "project_dir": str(self._project_dir),
                "strategy_sha": hashlib.sha256(strategy_text.encode()).hexdigest()[:12],
                "spec_sha": hashlib.sha256(spec_text.encode()).hexdigest()[:12],
                "roadmap_sha": hashlib.sha256(roadmap_text.encode()).hexdigest()[:12],
                "spec_text_preview": spec_text[:500],
                "current_milestone": current_milestone,
                "contract_id": contract_id,
            }
            if spec_text:
                sources.append("SPEC.md")

        # context_sha256 is computed AFTER filtering — only what actually made it
        # into the package, never the raw source files.
        # canonical_context is included so the SHA commits to the exact authority
        # identity (strategy_sha, spec_sha, roadmap_sha, contract_id) — otherwise
        # the same package_content with different authority context produces the
        # same sha256, making the hash useless for distinguishing "agent ignored
        # available context" vs "context injection gap".
        package_content = {
            "mission_id": mission_id,
            "canonical_context": canonical_ctx,  # authority identity; {} on legacy path
            "global_immutable": global_immutable,
            "architecture": architecture_list,
            "adrs": adrs,
            "lessons": lessons,
            "model_performance": model_performance,
            "dependency_receipts": dependency_receipts,
            "blockers": blockers,
        }
        context_sha256 = hashlib.sha256(
            json.dumps(package_content, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

        return ContextPackage(
            mission_id=mission_id,
            context_version=self._context_version(),
            generated_at=_now_iso(),
            agent=AGENT_ID,
            global_immutable=global_immutable,
            architecture=architecture_list,
            adrs=adrs,
            lessons=lessons,
            model_performance=model_performance,
            dependency_receipts=dependency_receipts,
            blockers=blockers,
            sources=sorted(set(sources)),
            context_sha256=context_sha256,
            canonical_context=canonical_ctx,
        )

    def save(self, package: ContextPackage, output_path: Path) -> None:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = output_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(package.to_dict(), indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
        os.replace(tmp, output_path)
