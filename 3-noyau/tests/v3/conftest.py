"""Conftest du banc v3 : dépôt produit temporaire, roadmap conforme, lanceur.

Stdlib seule. Chaque test construit son décor via la fixture produit_repo puis
make_roadmap (hash_contrat calculé par le noyau lui-même via --hash-contrat),
et lance run.py en sous-processus via run_v3. Aucun moteur réel n'est appelé :
FACTORY_NO_REAL_ENGINE=1 partout.
"""

import datetime
import json
import os
import pathlib
import re
import subprocess
import sys

import pytest

RACINE = pathlib.Path(__file__).resolve().parents[2]
RUN_PY = RACINE / "factory_v3" / "run.py"

MOD_BUGUE = "def add(a, b):\n    return a - b\n"
TEST_MOD_ROUGE = (  # v9 : juge isolé (F1) — le livrable s'exécute dans un sous-processus, jamais dans le juge
    "import os.path, subprocess, sys\n"
    "_p = os.path.join(os.path.dirname(__file__), '..', 'src', 'mod.py')\n"
    "_c = 'import runpy, sys; print(runpy.run_path(sys.argv[1])[\"add\"](1, 2))'\n\n"
    "def test_add():\n"
    "    r = subprocess.run([sys.executable, '-c', _c, _p], capture_output=True, text=True)\n"
    "    assert r.returncode == 0 and r.stdout.strip() == '3'\n"  # Q1(2) : rc exigé, pas seulement stdout
)
TEST_OK_VERT = "def test_toujours_vert():\n    assert 1 + 1 == 2\n"
GITIGNORE = "__pycache__/\n*.pyc\n"


def _git(repo, *args):
    """Exécute git dans le dépôt, échoue bruyamment le test sinon."""
    p = subprocess.run(
        ["git", "-C", str(repo)] + list(args), stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, "git %s: %s" % (" ".join(args), p.stderr)
    return p.stdout


@pytest.fixture
def produit_repo(tmp_path):
    """Dépôt produit minimal : git init, user, .gitignore, 2 commits, tests."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    (repo / "src" / "mod.py").write_text(MOD_BUGUE, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.email=banc@v3", "-c", "user.name=banc", "add", ".")
    _git(repo, "-c", "user.email=banc@v3", "-c", "user.name=banc",
         "commit", "-q", "-m", "base: src bogue + gitignore")
    (repo / "tests" / "test_mod.py").write_text(TEST_MOD_ROUGE, encoding="utf-8")
    (repo / "tests" / "test_ok.py").write_text(TEST_OK_VERT, encoding="utf-8")
    _git(repo, "-c", "user.email=banc@v3", "-c", "user.name=banc", "add", "tests")
    _git(repo, "-c", "user.email=banc@v3", "-c", "user.name=banc",
         "commit", "-q", "-m", "tests: un rouge un vert")
    return repo


def _jaml(valeur):
    """Rend une valeur Python en YAML valable (JSON strict est du YAML)."""
    return json.dumps(valeur, ensure_ascii=False)


def _rendre_roadmap(jalon, hash_contrat, product_repo):
    """Écrit le bloc YAML du jalon, ordre fixe conforme à INTERFACE §4."""
    return (
        "product_repo: %s\nmilestones:\n- id: %s\n  produit: %s\n  type: %s\n"
        "  spec_ref: %s\n  but: %s\n  task_file: %s\n  livrables: %s\n"
        "  geles: %s\n  juge: %s\n  test_cmd: %s\n  timeout_s: %s\n"
        "  budget_moteur_s: %s\n  reseau_tests: %s\n  juge_isole: %s\n  valide: %s\n" % (
            _jaml(str(product_repo)), _jaml(jalon["id"]), _jaml(jalon["produit"]),
            jalon["type"], _jaml(jalon["spec_ref"]), _jaml(jalon["but"]),
            _jaml(jalon["task_file"]), _jaml(jalon["livrables"]),
            _jaml(jalon["geles"]), _jaml(jalon["juge"]), _jaml(jalon["test_cmd"]),
            jalon["timeout_s"], jalon["budget_moteur_s"],
            "true" if jalon["reseau_tests"] else "false",
            "true" if jalon.get("juge_isole") else "false",   # lot O (28/09 pilote : décor resté d'avant l'obligation)
            _jaml({"par": jalon["valide"]["par"], "hash_contrat": hash_contrat,
                   "date": jalon["valide"]["date"]})))


def make_roadmap(tmp, milestone_overrides=None, repo=None):
    """Écrit projet/ROADMAP.yaml + TACHE.md et retourne les chemins.

    Le hash_contrat est calculé par le noyau (--hash-contrat) ; s'il échoue,
    le test ÉCHOUE avec la sortie (jamais xfail : une preuve indisponible reste visible).
    """
    tmp = pathlib.Path(tmp)
    repo = pathlib.Path(repo) if repo else tmp / "repo"
    projet = tmp / "projet"
    projet.mkdir(parents=True, exist_ok=True)
    tache = tmp / "TACHE.md"
    jalon = {
        "id": "M1", "produit": "calcul", "type": "construction",
        "spec_ref": "SPEC.md#m1", "but": "corriger add pour retourner la somme",
        "task_file": str(tache), "livrables": ["src/mod.py"], "geles": [],
        "juge": {"a_faire_passer": ["tests/test_mod.py::test_add"],
                 "a_garder_verts": ["tests/test_ok.py"]},
        "test_cmd": ["python3", "-m", "pytest", "-q", "--junitxml", "{junit}",
                     "tests/test_mod.py", "tests/test_ok.py"],
        "timeout_s": 600, "budget_moteur_s": 2700, "reseau_tests": False, "juge_isole": True,  # lot O : obligatoire
        "valide": {"par": "banc", "date": datetime.date.today().isoformat()},
    }
    jalon.update(milestone_overrides or {})
    contenu = (
        "# Tache M1\n\nLe fichier src/mod.py contient add(a, b) qui retourne"
        " a - b. Fais-en la somme pour que tests/test_mod.py passe,\n"
        "sans toucher aux fichiers geles.\n")
    tache.write_text(contenu, encoding="utf-8")
    cible = pathlib.Path(jalon["task_file"])
    if cible != tache and not cible.exists():
        try:
            cible.parent.mkdir(parents=True, exist_ok=True)
            cible.write_text(contenu, encoding="utf-8")
        except OSError:
            pass
    roadmap = projet / "ROADMAP.yaml"
    roadmap.write_text(_rendre_roadmap(jalon, "0" * 64, repo), encoding="utf-8")
    p = subprocess.run(
        [sys.executable, str(RUN_PY), "--hash-contrat", "--project", str(projet),
         "--milestone", str(jalon["id"])], cwd=str(RACINE), stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        pytest.fail("préparation impossible, preuve indisponible (jamais neutralisée) : --hash-contrat rc=%d stdout=%r stderr=%r" % (
            p.returncode, p.stdout, p.stderr))
    roadmap.write_text(
        _rendre_roadmap(jalon, p.stdout.strip(), repo), encoding="utf-8")
    return {"projet": projet, "repo": repo, "roadmap": roadmap,
            "task_file": cible, "milestone": jalon["id"]}


def _repo_depuis_args(args):
    """Retrouve le dépôt produit : --repo, sinon product_repo de la roadmap."""
    args = [str(a) for a in args]
    for drapeau, suivi in (("--repo", True), ("--project", False)):
        if drapeau in args:
            valeur = args[args.index(drapeau) + 1]
            if suivi:
                return pathlib.Path(valeur)
            texte = pathlib.Path(valeur, "ROADMAP.yaml").read_text(encoding="utf-8")
            m = re.search(r"(?m)^product_repo:\s*[\"']?([^\"'\s#]+)", texte)
            if m:
                return pathlib.Path(m.group(1))
    return None


def _recu_le_plus_recent(repo, runs_dir=None):
    """Lit le RECEIPT.json le plus récent du dépôt (mtime), sinon None."""
    base = pathlib.Path(runs_dir) if runs_dir else repo / ".factory_v3" / "runs"
    if not base.is_dir():
        return None
    candidats = sorted(
        (p for p in base.glob("*/RECEIPT.json") if p.is_file()),
        key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidats:
        return None
    return json.loads(candidats[0].read_text(encoding="utf-8"))


def run_v3(args, env=None, fake=None, timeout=120):
    """Lance run.py en sous-processus, retourne (rc, stdout, stderr, recu)."""
    args = [str(a) for a in args]
    complet = dict(os.environ)
    complet.update({
        "FACTORY_NO_REAL_ENGINE": "1",
        "FACTORY_V3_MACHINE_ID": "testmachine",
    })
    complet.update(env or {})
    if fake is not None:
        complet["FACTORY_V3_FAKE_ENGINE"] = str(fake)
    try:
        p = subprocess.run(
            [sys.executable, str(RUN_PY)] + args, cwd=str(RACINE), env=complet,
            stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
        rc, out, err = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as exc:
        rc = None
        out = exc.stdout or b""
        err = exc.stderr or b""
    repo = _repo_depuis_args(args)
    recu = None
    if repo is not None:
        runs_dir = None
        if "--runs-dir" in args:
            runs_dir = args[args.index("--runs-dir") + 1]
        recu = _recu_le_plus_recent(repo, runs_dir)
    return (rc, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), recu)
