"""TEST_2 — M12 VERROU (DECISION §2a/§8) : flock tenu pendant tout le run ; détenteur tué → verrou libéré.

ROUGE sur le noyau actuel (O_EXCL jamais purgé : le fichier d'un détenteur tué refuse les runs suivants),
VERT après CORR_2, ROUGE sur le mutant « retour O_EXCL » (les deux côtés). Hors ligne, faux moteurs
uniquement. À déposer dans tests/v3/ : python3 -m pytest tests/v3/TEST_2.py -q -p no:cacheprovider
"""

import importlib.util
import os
import signal
import subprocess
import sys
import time

import pytest

from conftest import RACINE, RUN_PY, make_roadmap, run_v3

_spec = importlib.util.spec_from_file_location("v3_noyau_test2", RUN_PY)
noyau = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(noyau)


def _repo_git(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@x", "-c", "user.name=t",
                    "commit", "--allow-empty", "-qm", "base"], check=True)
    return repo


def _run_nu(repo, jalon):
    r = noyau.Run.__new__(noyau.Run)
    r.repo, r.jalon, r.boot, r.uuid, r.R = repo, jalon, "b", "u", {"machine_id": "m"}
    return r


def test_flock_tenu_puis_libere_a_la_fermeture(tmp_path):
    """Tenu → un concurrent sur le même jalon est REFUSÉ rc 3 ; fd fermé (= détenteur tué) → accepté."""
    repo = _repo_git(tmp_path)
    r1 = _run_nu(repo, "M1")
    r1.verrou()
    os.fstat(r1.lockfd)  # le fd VIT : c'est lui qui détient le flock pendant tout le run
    r2 = _run_nu(repo, "M1")
    with pytest.raises(noyau.Fin) as exc:
        r2.verrou()
    assert exc.value.rc == 3 and "verrou tenu" in exc.value.motif
    os.close(r1.lockfd)  # détenteur tué : le verrou est rendu par la fermeture du descripteur
    r3 = _run_nu(repo, "M1")
    r3.verrou()  # plus jamais de fichier à purger à la main
    os.close(r3.lockfd)


def test_m12_detenteur_tue_libere_le_verrou(produit_repo, tmp_path):
    """M12 bout en bout : un run SIGKILLé en pleine détention ne bloque PAS le run suivant du jalon."""
    contexte = make_roadmap(tmp_path, repo=produit_repo)
    args = ["--project", contexte["projet"], "--milestone", contexte["milestone"]]
    verrou = produit_repo / ".git" / "factory_v3" / "locks" / (contexte["milestone"] + ".lock")
    env = dict(os.environ, FACTORY_NO_REAL_ENGINE="1", FACTORY_V3_MACHINE_ID="testmachine",
               FACTORY_V3_FAKE_ENGINE="honnete_livrable")
    p = subprocess.Popen([sys.executable, str(RUN_PY), *args], cwd=str(RACINE), env=env,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        limite = time.time() + 30
        while time.time() < limite and not verrou.is_file():
            time.sleep(0.2)
        assert verrou.is_file(), "préparation morte : le détenteur n'a jamais créé le verrou"
        time.sleep(1.0)  # laisser le flock être pris : tuer EN pleine détention
        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        p.wait(timeout=30)
    finally:
        if p.poll() is None:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
            p.wait(timeout=10)
    rc, out, err, recu = run_v3(args, fake="honnete_livrable")
    assert rc == 0, (rc, out[-500:], err[-500:])  # noyau actuel : REFUS rc 3 « verrou tenu » → ROUGE ici
    assert recu is not None and recu["verdict"] == "VERT"
