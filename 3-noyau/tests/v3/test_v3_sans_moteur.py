"""Banc v3 — scénarios sans moteur : refus, quotas, verrou, statut, crash (§8).
Chaque test vérifie le code retour attendu et, quand un reçu existe, ses
champs obligatoires (INTERFACE §5) via verifie_recu. Aucun moteur réel.
"""

import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import time

from conftest import (RACINE, RUN_PY, _git, _recu_le_plus_recent,
                      make_roadmap, run_v3)
from test_v3_moteurs import verifie_recu

GIT = ["-c", "user.email=banc@v3", "-c", "user.name=banc"]


def _args(contexte, *extra):
    """Arguments standard d'un run sur le jalon du contexte."""
    return ["--project", contexte["projet"],
            "--milestone", contexte["milestone"]] + list(extra)


def _verifie_si(recu):
    """Vérifie le reçu s'il existe (un refus avant dépense peut ne rien écrire)."""
    if recu is not None: verifie_recu(recu)


def _dernier(repo, nom):
    """Chemin le plus récent .factory_v3/runs/*/<nom>, sinon None."""
    base = pathlib.Path(repo) / ".factory_v3" / "runs"
    candidats = sorted(base.glob("*/" + nom),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    return candidats[0] if candidats else None


def test_consigne_vide(produit_repo, tmp_path):
    """task_file vidé : refus rc 3 avant dépense."""
    contexte = make_roadmap(tmp_path, repo=produit_repo)
    contexte["task_file"].write_text("", encoding="utf-8")
    rc, out, err, recu = run_v3(_args(contexte), fake="honnete_livrable")
    assert rc == 3, (rc, out[-500:], err[-500:])
    _verifie_si(recu)


def test_deja_vert(produit_repo, tmp_path):
    """test_add déjà vert à la base : refus rc 3."""
    (produit_repo / "src" / "mod.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8")
    _git(produit_repo, *GIT, "add", "src/mod.py")
    _git(produit_repo, *GIT, "commit", "-q", "-m", "corrige add")
    contexte = make_roadmap(tmp_path, repo=produit_repo)
    rc, out, err, recu = run_v3(_args(contexte), fake="honnete_livrable")
    assert rc == 3, (rc, out[-500:], err[-500:])
    _verifie_si(recu)


def test_doublon_vert(produit_repo, tmp_path):
    """Relance après un VERT honnête : refus rc 3."""
    contexte = make_roadmap(tmp_path, repo=produit_repo)
    rc1, out1, err1, recu1 = run_v3(_args(contexte), fake="honnete_livrable")
    assert rc1 == 0, (rc1, out1[-500:], err1[-500:])
    rc2, out2, err2, recu2 = run_v3(_args(contexte), fake="honnete_livrable")
    assert rc2 == 3, (rc2, out2[-500:], err2[-500:])
    _verifie_si(recu2)


def test_verrou_residuel_detenteur_mort_ne_bloque_pas(produit_repo, tmp_path):
    """v13 §2a : résidu d'un détenteur mort, aucun flock derrière — le run PASSE (l'O_EXCL en faisait un verrou mort)."""
    contexte = make_roadmap(tmp_path, repo=produit_repo)
    verrou = produit_repo / ".git" / "factory_v3" / "locks" / (contexte["milestone"] + ".lock")
    verrou.parent.mkdir(parents=True, exist_ok=True)
    verrou.write_text(json.dumps({"pid": 999999, "boot_id": "banc",
                                  "machine_id": "testmachine", "started": 1.0,
                                  "run_uuid": "ancien"}), encoding="utf-8")
    rc, out, err, recu = run_v3(_args(contexte), fake="honnete_livrable")
    assert rc == 0 and recu is not None and recu["verdict"] == "VERT", (rc, out[-500:], err[-500:])
    verifie_recu(recu)


def test_quota_absent_null(produit_repo, tmp_path):
    """Quota fake '{}' : clé glm absente, reçu avec quota null (pas 0)."""
    contexte = make_roadmap(tmp_path, repo=produit_repo)
    rc, out, err, recu = run_v3(
        _args(contexte), fake="honnete_livrable",
        env={"FACTORY_V3_FAKE_QUOTA": "{}"})
    assert rc == 0, (rc, out[-500:], err[-500:])
    assert recu is not None
    verifie_recu(recu)
    assert recu["quota"] is None


def test_quota_90(produit_repo, tmp_path):
    """Quota fake 95 % hebdo glm : refus rc 3."""
    contexte = make_roadmap(tmp_path, repo=produit_repo)
    rc, out, err, recu = run_v3(
        _args(contexte), env={"FACTORY_V3_FAKE_QUOTA": '{"glm":{"weekly":95}}'})
    assert rc == 3, (rc, out[-500:], err[-500:])
    _verifie_si(recu)


def test_disque_plein(produit_repo, tmp_path):
    """Disque fake 1 Go libre : refus rc 3."""
    contexte = make_roadmap(tmp_path, repo=produit_repo)
    rc, out, err, recu = run_v3(
        _args(contexte), env={"FACTORY_V3_FAKE_DISK_FREE_GB": "1"})
    assert rc == 3, (rc, out[-500:], err[-500:])
    _verifie_si(recu)


def test_profil_absent(produit_repo, tmp_path):
    """bornage.sb renommé pendant le run : rc 2, jamais de repli, puis remis."""
    contexte = make_roadmap(tmp_path, repo=produit_repo)
    profil = RACINE / "factory_v3" / "bornage.sb"
    cachette = profil.with_name("bornage.sb.hors_banc")
    avait = profil.exists()
    try:
        if avait:
            profil.rename(cachette)
        rc, out, err, recu = run_v3(_args(contexte), fake="honnete_livrable")
        assert rc == 2, (rc, out[-500:], err[-500:])
        _verifie_si(recu)
    finally:
        if avait and cachette.exists():
            cachette.rename(profil)


def test_jalon_observation(produit_repo, tmp_path):
    """Jalon de type observation : jamais donné à un moteur, rc 3."""
    contexte = make_roadmap(tmp_path, milestone_overrides={"type": "observation"},
                            repo=produit_repo)
    rc, out, err, recu = run_v3(_args(contexte), fake="honnete_livrable")
    assert rc == 3, (rc, out[-500:], err[-500:])
    _verifie_si(recu)


def test_livrable_ignore(produit_repo, tmp_path):
    """Livrable x.log ignoré par .gitignore : refus rc 3."""
    gitignore = produit_repo / ".gitignore"
    gitignore.write_text(gitignore.read_text(encoding="utf-8") + "*.log\n",
                         encoding="utf-8")
    _git(produit_repo, *GIT, "add", ".gitignore")
    _git(produit_repo, *GIT, "commit", "-q", "-m", "ignore les logs")
    contexte = make_roadmap(tmp_path, milestone_overrides={"livrables": ["x.log"]},
                            repo=produit_repo)
    rc, out, err, recu = run_v3(_args(contexte), fake="honnete_livrable")
    assert rc == 3, (rc, out[-500:], err[-500:])
    _verifie_si(recu)


def test_hash_contrat_faux(produit_repo, tmp_path):
    """hash_contrat stocké falsifié : refus rc 3."""
    contexte = make_roadmap(tmp_path, repo=produit_repo)
    texte = contexte["roadmap"].read_text(encoding="utf-8")
    morceau = texte.split("valide:")[-1]
    trouve = re.search(r'"([0-9a-f]{64})"', morceau)
    assert trouve, "pas de hash 64 hexa dans valide: %r" % morceau[:200]
    contexte["roadmap"].write_text(
        texte.replace(trouve.group(1), "f" * 64), encoding="utf-8")
    rc, out, err, recu = run_v3(_args(contexte), fake="honnete_livrable")
    assert rc == 3, (rc, out[-500:], err[-500:])
    _verifie_si(recu)


def test_donnees_manifeste_absentes(produit_repo, tmp_path):
    """juge.donnees vers un manifeste absent : refus rc 3."""
    juge = {"a_faire_passer": ["tests/test_mod.py::test_add"],
            "a_garder_verts": ["tests/test_ok.py"],
            "donnees": {"manifeste": "tests/fixtures/absent.manifest.json",
                        "sha256": "0" * 64}}
    contexte = make_roadmap(tmp_path, milestone_overrides={"juge": juge},
                            repo=produit_repo)
    rc, out, err, recu = run_v3(_args(contexte), fake="honnete_livrable")
    assert rc == 3, (rc, out[-500:], err[-500:])
    _verifie_si(recu)


# test_veille_simulee : RETIRÉ en v8 (relecture C:B1/C1) — décoratif : le décalage s'annulait dans chaque différence,
# aucune mutation ne le faisait rougir ; le réglage lui-même est retiré (test_durci_v8.py::test_b1_horloge_sans_decalage).


def test_jsonl_tronque(produit_repo, tmp_path):
    """v9 : dernière ligne de l'INDEX coupée → lecteur tolérant, ligne reconstruite depuis le reçu, doublon refusé."""
    contexte = make_roadmap(tmp_path, repo=produit_repo)
    rc, out, err, recu = run_v3(_args(contexte), fake="honnete_livrable")
    assert rc == 0, (rc, out[-500:], err[-500:])
    index = produit_repo / ".factory_v3" / "index.jsonl"
    index.write_bytes(index.read_bytes()[:-30])
    rc2, out2, err2, recu2 = run_v3(_args(contexte), fake="honnete_livrable")
    assert rc2 == 3 and "doublon" in recu2["motif"], (rc2, out2[-500:], err2[-500:])


def test_robuste_sigkill_avant_recu(produit_repo, tmp_path):
    """Noyau tué en vol : reçu RUNNING laissé tel quel (v9 : --statut retiré, aucune réécriture en UNKNOWN)."""
    contexte = make_roadmap(tmp_path, repo=produit_repo)
    env = dict(os.environ)
    env.update({"FACTORY_NO_REAL_ENGINE": "1",
                "FACTORY_V3_MACHINE_ID": "testmachine",
                "FACTORY_V3_FAKE_ENGINE": "robuste_muet"})
    p = subprocess.Popen(
        [sys.executable, str(RUN_PY), "--project", str(contexte["projet"]),
         "--milestone", str(contexte["milestone"]), "--budget-s", "600"],
        cwd=str(RACINE), env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    try:
        time.sleep(10)
        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        p.wait(timeout=30)
    finally:
        if p.poll() is None:
            p.kill()
        subprocess.run(["pkill", "-f", "fakes/robuste_muet"],
                       capture_output=True)
    recu = _recu_le_plus_recent(produit_repo)
    assert recu is not None
    verifie_recu(recu)
    assert recu["verdict"] == "RUNNING"
    espaces = produit_repo / ".factory_v3" / "spaces"
    assert espaces.is_dir() and any(espaces.iterdir())
