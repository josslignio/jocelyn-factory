"""Banc des mutants v3 — passe B : les six « TROU À ÉCRIRE » du CLASSEMENT du 25/09.

Complément de tests/v3/test_v3_mutants.py (passe A : 15 tueurs, numérotation de
l'ancien run) — ce fichier couvre les six survivants classés « TROU À ÉCRIRE »
par .context/BUILD_V3_2026-09-25/MUTATION/CLASSEMENT.md (numérotation du run du
24/09 20:10) : S2, S13, S20, S22, S23, S24. Chaque test est VERT sur le noyau
intact et ROUGE dès que la mutation correspondante est appliquée à
factory_v3/run.py. Rien pour S1/S17/S21 (équivalents prouvés) ni S5
(supprimable) : aucun test pour une différence qui n'existe pas.
Aucun moteur réel (FACTORY_NO_REAL_ENGINE=1 posé par conftest.run_v3).
"""

import hashlib
import importlib.util
import json
import subprocess

from conftest import RUN_PY, make_roadmap, run_v3
from _trous_outils import commit

_spec = importlib.util.spec_from_file_location("v3_noyau_mutants_b", RUN_PY)
noyau = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(noyau)

HONNETE = "honnete_livrable"


def lancer(produit_repo, tmp_path, overrides=None):
    """Décor standard + run honnête : (rc, out, err, recu, contexte)."""
    contexte = make_roadmap(tmp_path, milestone_overrides=overrides, repo=produit_repo)
    rc, out, err, recu = run_v3(["--project", contexte["projet"],
                                 "--milestone", contexte["milestone"]], fake=HONNETE)
    return rc, out, err, recu, contexte


def _runs(produit_repo):
    return produit_repo / ".factory_v3" / "runs"


# ---- S2 · run.py:50 · or_vers_and ----------------------------------------------------------------------------------------


# ---- S13 · run.py:300 · or_vers_and --------------------------------------------------------------------------------------


# ---- S20 · run.py:366 · vrai_vers_faux -----------------------------------------------------------------------------------


class _EspaceMinimal:
    """Juste de quoi appeler Run.effacer sans construire un run entier."""

    def __init__(self, chemin):
        self.espace = chemin


def test_s20_effacer_un_chemin_deja_disparu_ne_leve_pas(tmp_path):
    """S20 : effacer() supporte un chemin déjà disparu (missing_ok) — et efface bien les présents.

    Le mutant (missing_ok=False) lève FileNotFoundError dans remettre_a_base dès
    qu'un chemin listé a disparu entre-temps : un run entier perdu pour un reste.
    """
    (tmp_path / "present.txt").write_text("x", encoding="utf-8")
    noyau.Run.effacer(_EspaceMinimal(tmp_path), ["deja_disparu.txt", "present.txt"])
    assert not (tmp_path / "present.txt").exists()


# ---- S22 · run.py:681 · and_vers_or --------------------------------------------------------------------------------------


# ---- S23 · run.py:693 · or_vers_and --------------------------------------------------------------------------------------


# ---- S24 · run.py:714 · or_vers_and --------------------------------------------------------------------------------------


def test_s24_project_sans_milestone_refuse_avant_toute_depense(produit_repo, tmp_path):
    """S24 : --project sans --milestone → REFUS rc 3 sur stderr, rien créé.

    Le mutant (or→and) ne refuse que si les DEUX manquent : un seul manquant
    passe la porte et finit en échec avec reçu/verrou créés — la promesse
    « REFUS avant dépense » (rc 3, stderr, rien créé) serait morte.
    """
    contexte = make_roadmap(tmp_path, repo=produit_repo)
    rc, out, err, recu = run_v3(["--project", contexte["projet"]])
    assert rc == 3, (rc, out[-500:], err[-500:])
    assert "obligatoires" in err, err[-500:]
    assert recu is None
