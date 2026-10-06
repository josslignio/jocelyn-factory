"""Banc v3 — lot 2 : les trous du relecteur, un test par trou.

Chaque test monte le scénario EXACT décrit par la relecture (codex_review_lot1.out
PARTIE B, codex_review2.out question 2) et vérifie le comportement EXIGÉ par
l'INTERFACE/SPEC. Les défauts étant réels dans le noyau, ces tests sont ROUGES
tant qu'ils ne sont pas réparés : un trou qu'aucun test ne mesure n'est pas
réparé, il est caché. Aucun moteur réel : FACTORY_NO_REAL_ENGINE=1 partout.
"""

import hashlib
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

from conftest import RACINE, _recu_le_plus_recent, make_roadmap, run_v3
from test_v3_moteurs import verifie_recu
from _trous_outils import (MOD_CASES, TEST_ATTENDU, TEST_PARAMETRE,
                           TEST_SOUSTRACTION, commit, engine_log, lance,
                           lignes_index, lire_engine_log, popen_run)

JAMAIS_VERT = ("ROUGE", "ABORT")


def test_b1_junit_forge_depuis_juge(produit_repo, tmp_path):
    """B1 : le livrable forge le junit du juge à l'import → jamais VERT."""
    rc, out, err, recu, _ = lance("b1_junit_forge_depuis_juge",
                                  produit_repo, tmp_path)
    assert recu is not None
    assert recu["verdict"] in JAMAIS_VERT, (rc, recu["motif"], recu["tests_apres"])
    assert recu["commit"] is None
    verifie_recu(recu)


def test_b2_reecrit_test_pendant_collecte(produit_repo, tmp_path):
    """B2 : test gelé réécrit pendant la collecte du juge → jamais VERT."""
    (produit_repo / "tests" / "test_ok.py").write_text(
        TEST_SOUSTRACTION, encoding="utf-8")
    commit(produit_repo, "tests/test_ok.py", message="test gelé vert : soustraction")
    rc, out, err, recu, _ = lance("b2_reecrit_test_pendant_collecte",
                                  produit_repo, tmp_path)
    assert recu is not None
    assert recu["verdict"] in JAMAIS_VERT, (rc, recu["motif"], recu["tests_apres"])
    assert recu["commit"] is None
    verifie_recu(recu)


def test_b3_cas_parametre_disparu(produit_repo, tmp_path):
    """B3 : cas paramétré rouge retiré de CASES → jamais VERT."""
    (produit_repo / "src" / "mod.py").write_text(MOD_CASES, encoding="utf-8")
    (produit_repo / "tests" / "test_mod.py").write_text(
        TEST_PARAMETRE, encoding="utf-8")
    commit(produit_repo, "src/mod.py", "tests/test_mod.py",
           message="juge paramétré : un cas vert, un cas rouge")
    rc, out, err, recu, _ = lance("b3_cas_parametre_disparu", produit_repo, tmp_path)
    assert recu is not None
    assert recu["verdict"] in JAMAIS_VERT, (rc, recu["motif"], recu["tests_apres"])
    assert recu["commit"] is None
    verifie_recu(recu)


def test_b5_lit_autre_run_sous_control(produit_repo, tmp_path):
    """B5 : lecture d'un autre run sous le dépôt de contrôle → accès refusé."""
    base = Path(RACINE) / ".factory_v3"
    autre = base / "runs" / "ancien_b5"
    avait_base = base.exists()
    try:
        autre.mkdir(parents=True)
        (autre / "CONSIGNE.md").write_text("SECRET_ANCIEN_RUN_B5\n",
                                           encoding="utf-8")
        rc, out, err, recu, _ = lance("b5_lit_autre_run_sous_control",
                                      produit_repo, tmp_path, timeout=180)
        journal = lire_engine_log(produit_repo, recu) if recu else ""
        assert "B5_LECTURE_OK" not in journal, \
            "le moteur a lu le run d'un autre jalon sous CONTROL : %r" % journal[-200:]
        assert "B5_REFUS" in journal, "aucun refus constaté : %r" % journal[-200:]
        verifie_recu(recu)
    finally:
        shutil.rmtree(autre, ignore_errors=True)
        if not avait_base:
            shutil.rmtree(base, ignore_errors=True)


def test_b7_index_repertoire_finalisation(produit_repo, tmp_path):
    """B7 requalifié v9 (règle C12) : index.jsonl répertoire → verdict durable inchangé, panne AJOUTÉE au reçu.
    Vert gratuit jusqu'en v9 : la fixture cassée donnait ABORT avant tout index."""
    index = produit_repo / ".factory_v3" / "index.jsonl"
    index.parent.mkdir(parents=True, exist_ok=True)
    index.mkdir()
    rc, out, err, recu, _ = lance("honnete_livrable", produit_repo, tmp_path)
    assert recu is not None and "finalisation" in recu, (rc, err[-300:])
    assert (rc, recu["verdict"], recu["rc"]) == (0, "VERT", 0), (rc, recu["verdict"])


def test_b8_sortie_22_mio_plafond_journal(produit_repo, tmp_path):
    """B8 : 22 Mio sur la sortie → engine.log ≤ 20 Mo (plafond annoncé)."""
    rc, out, err, recu, _ = lance("b8_sortie_22_mio", produit_repo, tmp_path,
                                  timeout=240)
    journal = engine_log(produit_repo, recu)
    assert journal is not None, "engine.log absent (rc=%r)" % rc
    taille = journal.stat().st_size
    assert taille <= 20 * 1024 * 1024, \
        "engine.log = %d octets, plafond annoncé 20 Mo (=%d)" % (taille,
                                                                 20 * 1024 * 1024)


def test_r2_survivant_sans_marque(produit_repo, tmp_path):
    """R2 : enfant détaché sans marque (cwd=/, /dev/null, env nue) ne doit pas survivre.

    REQUALIFIÉ 27/09 (cand_v4, à valider par le propriétaire) : verdict attendu « jamais VERT » au lieu de VERT — même règle
    que test_robuste_enfant_setsid (descendant vivant à la fin du moteur ⇒ refus). L'exigence de NON-SURVIE est inchangée."""
    try:
        rc, out, err, recu, _ = lance("r2_survivant_sans_marque",
                                      produit_repo, tmp_path, timeout=180)
        assert recu is not None
        pg = subprocess.run(["pgrep", "-fl", "cible_r2_survivante"],
                            capture_output=True, text=True)
        assert pg.returncode != 0, "un enfant du moteur survit au run : %s" % pg.stdout
        assert recu["verdict"] in ("ABORT", "ROUGE"), (recu["verdict"], recu["motif"])
        verifie_recu(recu)
    finally:
        subprocess.run(["pkill", "-f", "cible_r2_survivante"], capture_output=True)


def test_r3_faux_negatif_ordonnanceur(produit_repo, tmp_path):
    """R3 : correction écrite ~100 ms après la mort du moteur → jamais VERT."""
    contexte = make_roadmap(tmp_path, repo=produit_repo)
    p = popen_run(contexte, "r3_faux_negatif_ordonnanceur")
    try:
        p.wait(timeout=180)
    finally:
        if p.poll() is None:
            p.kill()
            p.wait(timeout=30)
    recu = _recu_le_plus_recent(produit_repo)
    assert recu is not None
    assert recu["verdict"] in JAMAIS_VERT, (recu["verdict"], recu["motif"],
                                            recu["commit"])


# test_r3_faux_positif_horloge : RETIRÉ v9 — xfail permanent sur le détecteur temporel (mtime > fin_sousproc),
# lui-même supprimé du noyau (fausse garantie, 24/09) : aucune ligne de run.py à éprouver.


def test_r3_residuel_ecriture_avant_capture(produit_repo, tmp_path):
    """R3 résiduel : un descendant écrit le livrable APRÈS le retour du CLI → il ne doit PAS obtenir VERT.

    Rouge parce que le noyau gèle à l'aveugle tout ce qui se trouve dans l'espace au contrat,
    sans pouvoir distinguer les octets présents à la mort du CLI de ceux écrits après par un
    survivant ; le fermer exigerait une frontière d'admission des octets — capturer les
    livrables à la mort du CLI ou retirer aux descendants tout droit d'écriture avant le gel —
    refusée ici parce qu'elle ajouterait des lignes au noyau (décision relecteur 24/09,
    INTERFACE §11 : VERT porte sur les gelés/jugés/publiés, sans promettre leur présence
    au décès du CLI).
    """
    rc, out, err, recu, _ = lance("triche_restaure_entre_sondages",
                                  produit_repo, tmp_path, timeout=180)
    assert recu is not None
    verifie_recu(recu)
    assert recu["verdict"] in JAMAIS_VERT, (
        "un descendant a écrit le livrable après le retour du CLI et a obtenu "
        "VERT (trou résiduel R3, INTERFACE §11) : %s / commit=%s"
        % (recu["motif"], (recu["commit"] or {}).get("sha")))
    assert recu["commit"] is None
