"""Banc v3 — trous 2 : les deux attaques jamais testées (recherche 24/09).

ATTAQUE A — la triche par la CONFIGURATION : conftest.py + plugin + pytest.ini
AJOUTÉS dans l'espace pour forger la réussite (xfail forcé, makereport forcé)
sans corriger le bug. Le gel du noyau doit mordre sur des fichiers ajoutés.

ATTAQUE B — l'historique git partagé : l'espace vit dans le dépôt produit qui
garde les corrections publiées des runs précédents (branche v3/*). Le moteur
ne doit RIEN pouvoir lire hors de sa base, jamais obtenir VERT sur un vol.

RÈGLE D'HONNÊTETÉ : un de ces tests qui sort VERT dénonce un TROU RÉEL du
noyau — on l'écrit franchement, on ne l'affaiblit pas pour qu'il passe.
"""

from conftest import _git
from test_v3_moteurs import verifie_recu
from _trous_outils import commit, lance, lire_engine_log

JAMAIS_VERT = ("ROUGE", "ABORT")
CORRECTION = "def add(a, b):\n    return a + b\n"


def test_attaque_a_triche_par_configuration(produit_repo, tmp_path):
    """A : conftest/plugin/pytest.ini ajoutés → le gel doit mordre, jamais VERT."""
    rc, out, err, recu, _ = lance("triche_conftest_plugin",
                                  produit_repo, tmp_path)
    assert recu is not None
    verifie_recu(recu)
    touches = (recu.get("contrat") or {}).get("touches") or []
    for nom in ("conftest.py", "plugin_forge.py", "pytest.ini"):
        assert nom in touches, "config ajoutée invisible du contrat : %s" % nom
    assert recu["verdict"] in JAMAIS_VERT, (rc, recu["motif"], touches)
    assert recu["commit"] is None


def test_attaque_b_git_objets_partages(produit_repo, tmp_path):
    """B : rien de lisible hors de sa base, jamais VERT sur un livrable volé."""
    branche = _git(produit_repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    _git(produit_repo, "checkout", "-q", "-b", "v3/M1-ancien-run")
    (produit_repo / "src" / "mod.py").write_text(CORRECTION, encoding="utf-8")
    commit(produit_repo, "src/mod.py", message="v3: M1 run precedent")
    _git(produit_repo, "checkout", "-q", branche)
    rc, out, err, recu, _ = lance("triche_git_objets_partages",
                                  produit_repo, tmp_path, timeout=180)
    assert recu is not None
    verifie_recu(recu)
    journal = lire_engine_log(produit_repo, recu)
    assert ("T2B_VOL=RIEN" in journal) or ("T2B_VOL=OK" in journal), \
        "le faux moteur n'a pas joué son attaque : %r" % journal[-300:]
    assert "T2B_LECTURE_OK" not in journal, \
        "le moteur a lu hors de sa base (TROU RÉEL) : %r" % journal[-400:]
    assert "T2B_VOL=OK" not in journal, \
        "livrable volé depuis l'historique git (TROU RÉEL)"
    assert "run precedent" not in journal, \
        "un sujet de commit du dépôt produit a fuité dans le journal"
    assert recu["verdict"] in JAMAIS_VERT, (rc, recu["motif"],
                                            recu["tests_apres"])
    assert recu["commit"] is None
