"""Banc des mutants v3 — un test par survivant classé TROU DU BANC.

Référence : .context/BUILD_V3_2026-09-25/MUTATION/CLASSEMENT.md. Chaque test porte
le numéro du survivant qu'il tue : VERT sur le noyau intact, ROUGE dès que la
mutation correspondante est appliquée à factory_v3/run.py. S1 (équivalent) et
S4 (supprimable) sont ignorés : aucun test pour une différence qui n'existe pas.
Aucun moteur réel (FACTORY_NO_REAL_ENGINE=1 posé par conftest.run_v3).
"""

import importlib.util
import json
import os
import pathlib
import subprocess
import sys

from conftest import RACINE, RUN_PY, _recu_le_plus_recent, make_roadmap, run_v3

_spec = importlib.util.spec_from_file_location("v3_noyau_pour_mutants", RUN_PY)
noyau = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(noyau)

HONNETE = "honnete_livrable"


def lancer(produit_repo, tmp_path, overrides=None, env=None, fake=HONNETE):
    contexte = make_roadmap(tmp_path, milestone_overrides=overrides, repo=produit_repo)
    args = ["--project", contexte["projet"], "--milestone", contexte["milestone"]]
    rc, out, err, recu = run_v3(args, fake=fake, env=env)
    return rc, out, err, recu, contexte


def test_s3_ajouter_ligne_jamais_collee_a_une_ligne_tronquee(tmp_path):
    """S3 : une ligne JSON n'est jamais collée à une dernière ligne tronquée."""
    journal = tmp_path / "journal.jsonl"
    journal.write_bytes(b'{"seq": 1}')
    noyau.ajouter_ligne(journal, {"seq": 2})
    assert noyau.lire_jsonl(journal) == [{"seq": 1}, {"seq": 2}]


def test_s5_lire_usage_cumul_somme_les_evenements(tmp_path):
    """S5 : spec cumul → les valeurs des événements se somment, pas d'TypeError."""
    journal = tmp_path / "engine.jsonl"
    journal.write_bytes(b'{"type": "e", "n": 3}\n{"type": "e", "n": 4}\n')
    spec = {"type": "e", "cumul": True, "champs": {"input": "n"}}
    usage, _cout = noyau.lire_usage(spec, journal, True)
    assert usage["input"] == 7 and usage["turns"] == 2


def test_s6_element_non_dict_dans_milestones_est_saute(produit_repo, tmp_path):
    """S6 : un élément non-dict de milestones est sauté, pas un crash."""
    contexte = make_roadmap(tmp_path, repo=produit_repo)  # v9 : roadmap_en_dure appelait hash_contrat (retiré)
    rm = pathlib.Path(contexte["projet"]) / "ROADMAP.yaml"
    rm.write_text(rm.read_text(encoding="utf-8").replace("milestones:\n", "milestones:\n- 42\n"), encoding="utf-8")
    rc, out, err, recu = run_v3(["--project", contexte["projet"], "--milestone", contexte["milestone"]],
                                fake=HONNETE)
    assert rc == 0, (rc, out[-800:], err[-800:])
    assert recu is not None and recu["verdict"] == "VERT"


def test_s8_livrable_chemin_absolu_refuse(produit_repo, tmp_path):
    """S8 : la liste blanche des livrables refuse un chemin absolu."""
    rc, out, err, recu, _ = lancer(produit_repo, tmp_path,
                                    overrides={"livrables": ["/etc/hosts"]})
    assert rc == 3, (rc, out[-500:], err[-500:])
    assert recu is not None and "liste blanche" in recu["motif"]


def test_s9_test_cmd_sans_junit_refuse(produit_repo, tmp_path):
    """S9 : un test_cmd sans --junitxml {junit} est refusé sur son vrai motif."""
    overrides = {"test_cmd": ["python3", "-m", "pytest", "-q",
                              "tests/test_mod.py", "tests/test_ok.py"]}
    rc, out, err, recu, _ = lancer(produit_repo, tmp_path, overrides=overrides)
    assert rc == 3, (rc, out[-500:], err[-500:])
    assert recu is not None and "test_cmd" in recu["motif"]


def test_s10_juge_ids_non_chaines_refuse(produit_repo, tmp_path):
    """S10 : un id non-chaîne dans a_garder_verts → REFUS « juge », pas un crash."""
    overrides = {"juge": {"a_faire_passer": ["tests/test_mod.py::test_add"],
                          "a_garder_verts": [42]}}
    rc, out, err, recu, _ = lancer(produit_repo, tmp_path, overrides=overrides)
    assert rc == 3, (rc, out[-500:], err[-500:])
    assert recu is not None and "juge" in recu["motif"]


def test_s11_budget_moteur_s_est_le_plafond_effectif(produit_repo, tmp_path):
    """S11 : budget_moteur_s du jalon (sans --budget-s) fixe budget_mur_s du reçu."""
    rc, out, err, recu, _ = lancer(produit_repo, tmp_path,
                                    overrides={"budget_moteur_s": 300})
    assert rc == 0, (rc, out[-500:], err[-500:])
    assert recu["engine"]["plafonds_effectifs"]["budget_mur_s"] == 300


def test_s12_version_moteur_relue_en_texte(produit_repo, tmp_path):
    """S12 : --version du CLI relu en texte → REFUS ≠ ENGINES.lock, pas TypeError."""
    contexte = make_roadmap(tmp_path, repo=produit_repo)
    binaires = tmp_path / "bin"
    binaires.mkdir()
    stub = binaires / "opencode"
    stub.write_text("#!/bin/sh\necho 'stub 0.0.1'\n", encoding="utf-8")
    stub.chmod(0o755)
    env = {k: v for k, v in os.environ.items()
           if k not in ("FACTORY_NO_REAL_ENGINE", "FACTORY_V3_FAKE_ENGINE")}
    env["PATH"] = str(binaires) + os.pathsep + env.get("PATH", "")
    env["FACTORY_V3_MACHINE_ID"] = "testmachine"
    p = subprocess.run([sys.executable, str(RUN_PY), "--project",
                        str(contexte["projet"]), "--milestone", "M1"],
                       cwd=str(RACINE), env=env, stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=120)
    assert p.returncode == 3, (p.returncode, p.stderr.decode("utf-8", "replace")[-500:])
    recu = _recu_le_plus_recent(produit_repo)
    assert recu is not None and "ENGINES.lock" in recu["motif"]


def test_s13_un_refus_nest_pas_un_essai(produit_repo, tmp_path):
    """S13 : un reçu REFUS (quota) n'incrémente pas « essai » du run suivant."""
    rc, out, err, recu, _ = lancer(produit_repo, tmp_path,
                                    env={"FACTORY_V3_FAKE_QUOTA":
                                         '{"glm": {"weekly": 95}}'})
    assert rc == 3 and recu is not None and "quota" in recu["motif"]
    rc, out, err, recu, _ = lancer(produit_repo, tmp_path,
                                    env={"FACTORY_V3_FAKE_QUOTA": ""})
    assert rc == 0, (rc, out[-800:], err[-800:])
    assert recu["essai"] == 1


def test_s14_deux_echecs_identiques_attente_humaine(produit_repo, tmp_path):
    """S14 : 2 échecs au même motif_echec → REFUS ATTENTE_HUMAINE, pas un 3e essai."""
    for _ in range(2):
        rc, out, err, recu, _ = lancer(produit_repo, tmp_path,
                                        fake="triche_modifie_test")
        assert rc == 2 and recu is not None and recu["motif_echec"] == "triche"
    rc, out, err, recu, _ = lancer(produit_repo, tmp_path,
                                   fake="triche_modifie_test")
    assert rc == 3, (rc, out[-800:], err[-800:])
    assert recu is not None and "ATTENTE_HUMAINE" in recu["motif"]


def test_s15_disque_a_la_borne_exacte_passe(produit_repo, tmp_path):
    """S15 : 10.0 Gio libres pile → jalon déclarable (contrat strictement <)."""
    env = {"FACTORY_V3_FAKE_DISK_FREE_GB": "10.0", "FACTORY_MIN_FREE_GB": "10"}
    rc, out, err, recu, _ = lancer(produit_repo, tmp_path, env=env)
    assert rc == 0, (rc, out[-500:], err[-500:])
    assert recu["verdict"] == "VERT" and recu["disk_free_gb"] == 10.0


# S16/S17 (geles_presents, ecart_gele) : RETIRÉS en v8 avec la machinerie ecart_gele (relectures B:4, C:B4) ;
# requalifiés dans test_durci_v8.py::test_b4_machinerie_ecart_gele_retiree.
