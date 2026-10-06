"""Outils du banc des trous (lot 2) : décors spéciaux et lanceurs. Stdlib seule.

Les scénarios B1-B5/R2/R3 du relecteur demandent des juges différents du
décor par défaut de conftest (tests paramétrés, test gelé dépendant du
livrable, donnée manifestée). Chaque décor est commité dans le dépôt produit
temporaire avant le calcul du hash_contrat, puis le faux moteur correspondant
joue le trou. Aucun moteur réel.
"""

import json
import os
import pathlib
import subprocess
import sys

from conftest import RACINE, RUN_PY, _git, make_roadmap, run_v3

GIT = ["-c", "user.email=banc@v3", "-c", "user.name=banc"]

CHARGEUR_MOD = (
    "import importlib.util, os.path\n"
    "_p = os.path.join(os.path.dirname(__file__), '..', 'src', 'mod.py')\n"
    "_s = importlib.util.spec_from_file_location('mod', _p)\n"
    "mod = importlib.util.module_from_spec(_s); _s.loader.exec_module(mod)\n"
)

MOD_CASES = (
    "CASES = [(0, 0, 0), (1, 2, 3)]\n"
    "\n"
    "\n"
    "def add(a, b):\n"
    "    return a - b\n"
)

TEST_PARAMETRE = CHARGEUR_MOD + (
    "\n"
    "import pytest\n"
    "\n"
    "\n"
    "@pytest.mark.parametrize(\"a,b,attendu\", mod.CASES)\n"
    "def test_add(a, b, attendu):\n"
    "    assert mod.add(a, b) == attendu\n"
)

TEST_SOUSTRACTION = CHARGEUR_MOD + (
    "\n"
    "\n"
    "def test_soustraction():\n"
    "    assert mod.add(2, 1) == 1\n"
)

TEST_ATTENDU = CHARGEUR_MOD + (
    "import os.path\n"
    "\n"
    "\n"
    "def test_add():\n"
    "    chemin = os.path.join(os.path.dirname(__file__), 'fixtures', 'expected.txt')\n"
    "    with open(chemin, encoding='utf-8') as f:\n"
    "        attendu = int(f.read().strip())\n"
    "    assert mod.add(1, 2) == attendu\n"
)


def commit(repo, *chemins, message="decor trou"):
    _git(repo, *GIT, "add", "--", *chemins)
    _git(repo, *GIT, "commit", "-q", "-m", message)


def lance(fake, produit_repo, tmp_path, overrides=None, **kw):
    """Monte le décor standard puis lance run.py avec le faux moteur <fake>."""
    contexte = make_roadmap(tmp_path, milestone_overrides=overrides,
                            repo=produit_repo)
    rc, out, err, recu = run_v3(
        ["--project", contexte["projet"], "--milestone", contexte["milestone"]],
        fake=fake, **kw)
    return rc, out, err, recu, contexte


def env_run(fake):
    env = dict(os.environ)
    env.update({"FACTORY_NO_REAL_ENGINE": "1",
                "FACTORY_V3_MACHINE_ID": "testmachine",
                "FACTORY_V3_FAKE_ENGINE": fake})
    return env


def popen_run(contexte, fake, **kw):
    """Lance run.py en fond (pour les scénarios qui agissent pendant le run)."""
    return subprocess.Popen(
        [sys.executable, str(RUN_PY), "--project", str(contexte["projet"]),
         "--milestone", str(contexte["milestone"])],
        cwd=str(RACINE), env=env_run(fake), stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)


def engine_log(repo, recu):
    p = pathlib.Path(repo) / ".factory_v3" / "runs" / recu["run_uuid"] / "engine.log"
    return p if p.is_file() else None


def lire_engine_log(repo, recu):
    p = engine_log(repo, recu)
    return p.read_text(encoding="utf-8", errors="replace") if p else ""


def lignes_index(repo):
    p = pathlib.Path(repo) / ".factory_v3" / "index.jsonl"
    out = []
    if p.is_file():
        for ligne in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                out.append(json.loads(ligne))
            except ValueError:
                pass
    return [d for d in out if isinstance(d, dict)]
