#!/usr/bin/env python3
"""Préflight OBLIGATOIRE : ce dépôt est-il bien celui qu'on croit ?

Décision D1 du Boss, 2026-08-07. À exécuter AVANT tout travail d'agent, avant la
première écriture.

## Pourquoi cette vérification, et pas une autre

Le 7 août 2026, un agent a travaillé pendant des heures dans un dépôt qui portait tous
les signes du bon dépôt. La branche s'appelait `main`. `git status` répondait « à jour ».
Un `remote` nommé `origin` existait. **Tout mentait.**

La branche `main` de ce dépôt est un ORPHELIN d'un seul commit, sans aucun ancêtre commun
avec la vraie ligne de développement (55 commits sur `origin/main`, 274 sur
`run7/live-product`). Le piège s'est armé quand un dépôt d'archive a été renommé `origin`
et le vrai `origin` déplacé : l'amont de `main` a suivi l'ancien nom sans jamais être
rattaché. Depuis, `git status` compare `main` à l'archive de l'orphelin — et le trouve
parfaitement à jour.

Toutes les vérifications habituelles ont échoué parce qu'elles reposent sur des NOMS :
nom de branche, nom de remote, message de `git status`. Or les noms sont exactement ce
qui a été échangé.

**Le commit racine, lui, ne se renomme pas.** Il est calculé sur le contenu et l'histoire.
Deux dépôts sans ancêtre commun ont des racines différentes, définitivement — même si
leurs fichiers sont identiques à l'octet près, ce qui est précisément le cas ici : les
deux racines portent la même empreinte d'arborescence `f4ec8737…` et ne diffèrent que par
l'étiquette de leur commit. C'est la seule grandeur que le renommage n'atteint pas.

## Fail-closed

Un dépôt inconnu du registre n'est pas « probablement bon » : il est REFUSÉ (rc=4). Il
faut l'inscrire explicitement. C'est la loi n°1 de la Factory — l'ambiguïté bloque.

Usage :
    python3 preflight_repo_identity.py --repo <chemin>     # rc 0 / 3 / 4 / 5
    python3 preflight_repo_identity.py --repo <chemin> --json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REGISTRY = HERE.parent / "config" / "repo_identity.json"

RC_OK = 0
RC_WRONG_ROOT = 3       # la racine ne correspond pas -> mauvais dépôt
RC_UNREGISTERED = 4     # dépôt inconnu du registre -> fail-closed
RC_NOT_A_REPO = 5       # pas un dépôt git, ou git indisponible


try:  # imported as a package module (rare) or run as a plain script
    from factory_v2._io import _git as _git_io
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from factory_v2._io import _git as _git_io  # type: ignore[no-redef]


def _git_rc(repo: Path, *args: str) -> tuple[int, str]:
    try:
        p = _git_io(repo, *args, timeout=60)
        return p.returncode, (p.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 128, f"GIT_ERROR: {exc}"


def head_roots(repo: Path) -> list[str]:
    """Racines atteignables depuis HEAD.

    Il peut y en avoir plusieurs quand des histoires sans lien ont été fusionnées — et
    c'est justement un cas qu'on veut voir plutôt qu'ignorer.
    """
    # `--no-replace-objects` est INDISPENSABLE, pas une précaution de style. `git replace` permet
    # de greffer une histoire sur une autre : la lecture ordinaire suit la greffe et rapporte
    # alors la racine du greffon. Un dépôt orphelin greffé sur la vraie ligne passerait ce
    # contrôle au vert tout en restant orphelin — la greffe ne voyage ni par `clone` ni par
    # `push`, si bien que le dépôt réparé « en apparence » redeviendrait faux ailleurs.
    # Autrement dit, sans ce drapeau, le garde-fou construit pour ce bug pourrait être aveuglé
    # par une commande d'une ligne. Trouvé par la relecture du plan D2, 2026-08-07.
    rc, out = _git_rc(repo, "--no-replace-objects", "rev-list", "--max-parents=0", "HEAD")
    if rc != 0:
        return []
    return [l.strip() for l in out.splitlines() if l.strip()]


def load_registry(path: Path = REGISTRY) -> dict:
    if not path.is_file():
        return {"repos": {}, "quarantined_roots": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve(repo: Path, reg: dict) -> tuple[str | None, dict | None]:
    """Trouve l'entrée du registre qui gouverne ce chemin.

    On associe par CHEMIN RÉEL (`resolve()`), pas par nom : un worktree ou un lien
    symbolique ne doit pas permettre d'échapper à la règle. L'entrée la plus spécifique
    (le préfixe le plus long) gagne, pour qu'un sous-dépôt puisse avoir sa propre règle.
    """
    real = repo.resolve()
    best_key, best_entry, best_len = None, None, -1
    for key, entry in (reg.get("repos") or {}).items():
        base = Path(key).expanduser().resolve()
        if real == base or base in real.parents:
            if len(str(base)) > best_len:
                best_key, best_entry, best_len = key, entry, len(str(base))
    return best_key, best_entry


def check(repo: Path, reg: dict | None = None) -> dict:
    reg = reg if reg is not None else load_registry()
    real = repo.resolve()

    rc, _ = _git_rc(repo, "rev-parse", "--git-dir")
    if rc != 0:
        return {"ok": False, "rc": RC_NOT_A_REPO, "repo": str(real),
                "verdict": "NOT_A_GIT_REPO",
                "message": f"{real} n'est pas un dépôt git — impossible de vérifier son identité."}

    roots = head_roots(repo)
    if not roots:
        return {"ok": False, "rc": RC_NOT_A_REPO, "repo": str(real),
                "verdict": "NO_ROOT_COMMIT",
                "message": "aucun commit racine lisible (dépôt vide, ou HEAD non né)."}

    quarantined = reg.get("quarantined_roots") or {}
    hit = [r for r in roots if r in quarantined]
    if hit:
        return {"ok": False, "rc": RC_WRONG_ROOT, "repo": str(real),
                "verdict": "QUARANTINED_ROOT", "head_roots": roots,
                "quarantined_hit": hit,
                "message": ("STOP — la racine de HEAD est une racine MISE EN QUARANTAINE : "
                            + "; ".join(f"{r[:7]} = {quarantined[r]}" for r in hit)
                            + ". Aucune écriture. Repartir de la vraie ligne.")}

    key, entry = _resolve(repo, reg)
    if entry is None:
        return {"ok": False, "rc": RC_UNREGISTERED, "repo": str(real),
                "verdict": "UNREGISTERED_REPO", "head_roots": roots,
                "message": (f"STOP — {real} n'est inscrit dans aucun registre d'identité. "
                            f"Un dépôt inconnu n'est pas présumé bon (fail-closed, loi n°1). "
                            f"Racine observée : {roots[0]}. L'inscrire dans {REGISTRY} "
                            f"si elle est légitime.")}

    expected = entry["expected_root"]
    if expected not in roots:
        return {"ok": False, "rc": RC_WRONG_ROOT, "repo": str(real),
                "verdict": "WRONG_ROOT", "registry_key": key,
                "expected_root": expected, "head_roots": roots,
                "message": (f"STOP — mauvais dépôt ou mauvaise branche. "
                            f"Attendu la racine {expected[:7]} ({entry.get('label', key)}), "
                            f"observé {', '.join(r[:7] for r in roots)}. "
                            f"Aucune écriture : le travail atterrirait sur une histoire "
                            f"sans lien avec la vraie ligne.")}

    return {"ok": True, "rc": RC_OK, "repo": str(real), "verdict": "REPO_IDENTITY_CONFIRMED",
            "registry_key": key, "expected_root": expected, "head_roots": roots,
            "message": f"identité confirmée : racine {expected[:7]} ({entry.get('label', key)})."}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=".", help="dépôt à vérifier (défaut : répertoire courant)")
    ap.add_argument("--json", action="store_true", help="sortie machine")
    ap.add_argument("--registry", default=None, help="registre d'identité alternatif")
    args = ap.parse_args(argv)

    reg = load_registry(Path(args.registry)) if args.registry else load_registry()
    res = check(Path(args.repo), reg)

    if args.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        mark = "OK  " if res["ok"] else "STOP"
        print(f"[{mark}] {res['verdict']}\n       {res['message']}", file=sys.stderr if not res["ok"] else sys.stdout)
    return res["rc"]


if __name__ == "__main__":
    raise SystemExit(main())
