"""Hygiène du dépôt — FACTORY_REPO_HYGIENE_V1 (22/09) : le code d'un côté, la comptabilité de l'autre.

Trois règles, toutes index-only (jamais une suppression sur disque) :
  1. Les checkpoints de mission (PLAN.md, REPORT.json, .factory_worktree_meta.json) restent COMMITTÉS dans la
     branche candidate (le dispatcher lit REPORT.json au SHA du candidat, fail-closed) ; c'est la fusion qui les
     retire du résultat : ils n'entrent jamais sur la branche dorée (`strip_from_index`).
  2. La comptabilité runtime (projects/*/evidence/, evidence/, reçus d'allocation/validation, observations
     hebdomadaires, archives mortes) est ignorée par git (.gitignore) ; tout fichier suivi qui tombe sous une
     règle d'ignore est retiré de l'index au moment de la fusion (`tracked_ignored` + `strip_from_index`).
  3. Une fusion ne fait jamais disparaître un fichier de comptabilité du disque : ce que la fusion a supprimé et que
     le dépôt ignore désormais est réécrit, non suivi, depuis le commit d'avant (`restore_ignored_deletions`) ;
     `heal_recent_merges` rejoue cette restauration sur les dernières fusions (idempotent) pour une fusion faite
     par un daemon d'avant ce jalon.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path

MISSION_CHECKPOINTS = ("PLAN.md", "REPORT.json", ".factory_worktree_meta.json")
PARKED_CHECKPOINTS_DIR = Path("factory/run/parked_checkpoints")   # factory/run/* est ignoré par git


def _git(repo: Path, *args: str, timeout: int = 120) -> tuple[int, str, str]:
    try:
        r = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, "", f"{type(exc).__name__}: {exc}"
    return r.returncode, r.stdout, r.stderr


def tracked_ignored(repo: Path) -> list[str]:
    """Fichiers SUIVIS qui tombent sous une règle d'ignore (.gitignore / info/exclude) : de la comptabilité qui n'a
    plus sa place dans l'index. Vide si git est indisponible."""
    rc, out, _ = _git(repo, "ls-files", "-ci", "--exclude-standard")
    return [p for p in out.splitlines() if p.strip()] if rc == 0 else []


def strip_from_index(repo: Path, paths) -> list[str]:
    """`git rm --cached` (le disque n'est jamais touché) sur les chemins donnés ; retourne ceux effectivement
    retirés. Fonctionne aussi sur une entrée en conflit (add/add) : la résolution est « absent du résultat »."""
    paths = [str(p) for p in paths if str(p).strip()]
    if not paths:
        return []
    rc, out, _ = _git(repo, "ls-files", "--", *paths)
    tracked = [p for p in out.splitlines() if p.strip()] if rc == 0 else []
    if not tracked:
        return []
    rc, _, _ = _git(repo, "rm", "-q", "-r", "--cached", "--ignore-unmatch", "--", *sorted(set(tracked)))
    return sorted(set(tracked)) if rc == 0 else []


def park_untracked_checkpoints(repo: Path, mission_id: str) -> list[str]:
    """Copies NON SUIVIES et non ignorées des checkpoints à la racine du dépôt de contrôle (héritées d'une fusion
    précédente) : une fusion qui apporte PLAN.md refuserait de les écraser (« untracked working tree files would be
    overwritten »). Elles sont déplacées — jamais effacées — sous factory/run/parked_checkpoints/<ts>_<mission>/."""
    parked: list[str] = []
    for name in MISSION_CHECKPOINTS:
        src = Path(repo) / name
        if not src.is_file():
            continue
        rc_tracked, _, _ = _git(repo, "ls-files", "--error-unmatch", "--", name)
        if rc_tracked == 0:
            continue                                   # suivie : la fusion la traite comme une entrée d'index
        rc_ignored, _, _ = _git(repo, "check-ignore", "-q", "--", name)
        if rc_ignored == 0:
            continue                                   # ignorée : git l'écrase sans refuser
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in mission_id)
        stamp = f"{int(time.time())}_{safe}"
        dest_dir, n = Path(repo) / PARKED_CHECKPOINTS_DIR / stamp, 1
        while (dest_dir / name).exists():      # deux parcages dans la même seconde (avant/après fusion) :
            n += 1                             # jamais écraser une copie déjà parquée (essai 3)
            dest_dir = Path(repo) / PARKED_CHECKPOINTS_DIR / f"{stamp}_{n}"
        dest_dir.mkdir(parents=True, exist_ok=True)
        src.replace(dest_dir / name)
        parked.append(str((dest_dir / name).relative_to(repo)))
    return parked


def restore_ignored_deletions(repo: Path, before_sha: str, after_sha: str = "HEAD") -> list[str]:
    """Réécrit sur disque, non suivi, chaque fichier supprimé entre before_sha et after_sha que le dépôt ignore
    désormais. Idempotent (un fichier présent n'est jamais réécrit) ; ne lève jamais."""
    repo = Path(repo)
    restored: list[str] = []
    try:
        rc, out, _ = _git(repo, "diff", "--name-only", "--diff-filter=D", before_sha, after_sha)
        deleted = [p for p in out.splitlines() if p.strip()] if rc == 0 else []
        if not deleted:
            return restored
        r = subprocess.run(["git", "check-ignore", "--stdin"], cwd=str(repo), capture_output=True, text=True,
                           input="\n".join(deleted) + "\n", timeout=120)
        for rel in (p for p in r.stdout.splitlines() if p.strip()):
            dst = repo / rel
            if dst.is_symlink() or (dst.exists() and not rel.endswith(".jsonl")):
                continue                       # présent : le daemon a réécrit un état frais, on ne l'écrase jamais
            blob = subprocess.run(["git", "show", f"{before_sha}:{rel}"], cwd=str(repo), capture_output=True,
                                  timeout=120)
            if blob.returncode != 0:
                continue
            if dst.exists():                   # journal .jsonl recréé (append) entre la fusion et le heal :
                if not _merge_jsonl_history(dst, blob.stdout):   # lignes historiques + lignes nouvelles, rien perdu
                    continue
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(blob.stdout)
            restored.append(rel)
    except Exception:   # noqa: BLE001 — best-effort après une fusion déjà faite
        pass
    return restored


def _merge_jsonl_history(dst: Path, history: bytes) -> bool:
    """Un journal `.jsonl` supprimé par une fusion (ancien code) puis recréé par le daemon (append) entre-temps :
    réécrit `history` (dans l'ordre) suivi des lignes actuelles qui n'y figurent pas. False si rien ne manque
    (idempotent). Une ligne est une entrée : jamais de doublon, jamais de perte."""
    current = dst.read_bytes()
    if current.startswith(history):
        return False
    hist_lines = history.splitlines(keepends=True)
    if hist_lines and not hist_lines[-1].endswith(b"\n"):
        hist_lines[-1] += b"\n"
    cur_lines = current.splitlines(keepends=True)
    if cur_lines and not cur_lines[-1].endswith(b"\n"):
        cur_lines[-1] += b"\n"
    known = set(hist_lines)
    if all(ln in set(cur_lines) for ln in hist_lines):
        return False
    dst.write_bytes(b"".join(hist_lines) + b"".join(ln for ln in cur_lines if ln not in known))
    return True


HEALED_MERGES_MARKER = Path("factory/run/repo_hygiene/healed_merges.txt")   # factory/run/* est ignoré par git


def _healed_merges(repo: Path) -> set[str]:
    f = Path(repo) / HEALED_MERGES_MARKER
    try:
        return {ln.strip() for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()} if f.is_file() else set()
    except OSError:
        return set()


def _mark_healed(repo: Path, sha: str) -> None:
    f = Path(repo) / HEALED_MERGES_MARKER
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a", encoding="utf-8") as fh:
            fh.write(sha + "\n")
    except OSError:
        pass


def heal_recent_merges(repo: Path, max_merges: int = 5) -> list[str]:
    """Rejoue `restore_ignored_deletions` sur les `max_merges` dernières fusions de la première lignée de HEAD :
    une fusion faite par un daemon d'avant ce jalon a pu supprimer du disque de la comptabilité que le dépôt
    ignore désormais. Chaque fusion n'est soignée qu'UNE fois (marqueur `factory/run/repo_hygiene/healed_merges.txt`,
    hors git) : en régime établi la passe coûte un `git rev-list`, jamais un `git show` par fichier (essai 3 :
    le daemon bloquait sa boucle sur des milliers d'appels git). Idempotent, jamais d'exception."""
    restored: list[str] = []
    rc, out, _ = _git(repo, "rev-list", "--first-parent", "--merges", f"--max-count={int(max_merges)}", "HEAD")
    if rc != 0:
        return restored
    done = _healed_merges(repo)
    for sha in (s for s in out.split() if s.strip()):
        if sha in done:
            continue
        restored.extend(restore_ignored_deletions(repo, f"{sha}^1", sha))
        _mark_healed(repo, sha)
    return restored


def clean_merge_result(repo: Path) -> dict:
    """Pendant une fusion `--no-commit` (ou juste avant tout commit) : retire de l'index les checkpoints de mission
    et tout fichier suivi qui est désormais ignoré. Retourne {"checkpoints": [...], "bookkeeping": [...]}."""
    checkpoints = strip_from_index(repo, MISSION_CHECKPOINTS)
    bookkeeping = strip_from_index(repo, [p for p in tracked_ignored(repo) if p not in MISSION_CHECKPOINTS])
    return {"checkpoints": checkpoints, "bookkeeping": bookkeeping}
