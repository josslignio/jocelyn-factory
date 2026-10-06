"""L2 (02/10) — mini-factory : budget technique par travail (jamais renouvelé), noyau tué avec mf vivant réconcilié sur
FAITS (même reprise que l'orphelin), file durable (fsync du dossier). Hermétique : le « noyau » est un Python qui se tue."""
import importlib.util, json, os, shlex, stat, subprocess, sys, time  # noqa: E401
import pytest
from conftest import RUN_PY

MF = RUN_PY.parents[2] / "mini_factory" / "mf.py"
HC = "a" * 64


def _mf(tmp_path, monkeypatch, noyau=""):
    monkeypatch.setenv("MF_ETAT", str(tmp_path / "etat")); monkeypatch.setenv("MF_NOYAU", noyau or f"{sys.executable} {RUN_PY}")
    s = importlib.util.spec_from_file_location(f"mf_l2_{time.time_ns()}", MF); m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
    return m


def _item(etat, pannes, metier=0, **kw):
    dem = [{"t": "x", "technique": True} for _ in range(pannes)] + [{"t": "y"} for _ in range(metier)]
    return {"id": "T", "projet": "p", "jalon": "M1", "moteur": "glm", "essais": 2, "etat": etat, "reglages": [],
            "hash_contrat": HC, "demarrages": dem, **kw}


@pytest.mark.parametrize("pannes,metier,rc,etat", [(2, 0, 0, "pret"), (2, 1, 0, "pret"), (3, 0, 3, "bloque"), (4, 0, 3, "bloque")])
def test_regler_pret_ne_renouvelle_jamais_le_budget_technique(tmp_path, monkeypatch, pannes, metier, rc, etat):
    """3 pannes techniques AU TOTAL par travail : remettre en file un travail qui les a épuisées est refusé ; en deçà, la
    remise en file garde les démarrages (rien n'est remis à zéro) ; un essai métier ne consomme pas ce budget."""
    m = _mf(tmp_path, monkeypatch); m.ecrire({"T": _item("bloque", pannes, metier)})
    assert m.main(["regler", "T", "pret", "--raison", "reprise"]) == rc
    t = m.lire()["T"]
    assert (t["etat"], sum(1 for d in t["demarrages"] if d.get("technique"))) == (etat, pannes), t


NOYAU_TUE = (  # « noyau » : --hash-contrat → HC ; sinon écrit (ou non) un reçu puis se tue par SIGKILL (ou sort rc 3 sans reçu)
    "import json, os, signal, sys, time\n"
    "if '--hash-contrat' in sys.argv: print('" + HC + "'); sys.exit(0)\n"
    "mode, repo, u = os.environ['L2_MODE'], os.environ['L2_REPO'], 'u-' + str(os.getpid())\n"
    "now = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())\n"
    "R = {'schema': 'factory-v3-receipt/3', 'run_uuid': u, 'milestone': 'M1', 'hash_contrat': '" + HC + "', 'base_sha': 'b' * 40,\n"
    "     'verdict': 'VERT', 'rc': 0, 'commit': {'sha': 'c' * 40}, 'noyau': {'flags': []}, 'engine': {}, 'started_at': now, 'finished_at': now}\n"
    "if mode == 'running': R.update(verdict='RUNNING', rc=None, commit=None, finished_at=None)\n"
    "if mode in ('vert', 'running'):\n"
    "    os.makedirs(f'{repo}/.factory_v3/runs/{u}'); open(f'{repo}/.factory_v3/runs/{u}/RECEIPT.json', 'w').write(json.dumps(R))\n"
    "sys.exit(3) if mode == 'rc3' else os.kill(os.getpid(), signal.SIGKILL)\n")


@pytest.mark.parametrize("mode,etat,technique", [("running", "pret", True), ("rien", "pret", True), ("vert", "accepte", None),
                                                 ("rc3", "inconnu", None)])
def test_noyau_tue_mf_vivant_reconcilie(tmp_path, monkeypatch, mode, etat, technique):
    """Noyau tué par un signal pendant que mf vit : issue lue sur les FAITS (reçus, verrous) comme pour un orphelin — reçu
    final VERT → accepte ; aucun reçu final → panne technique, pret. Autre côté : rc 3 sans reçu (pas un signal) → inconnu."""
    repo, proj = tmp_path / "repo", tmp_path / "proj"
    repo.mkdir(); proj.mkdir()
    for c in (["init", "-q"], ["-c", "user.email=b@v", "-c", "user.name=b", "commit", "-q", "--allow-empty", "-m", "base"]):
        subprocess.run(["git", "-C", str(repo), *c], check=True, capture_output=True)
    (proj / "ROADMAP.yaml").write_text(f"product_repo: {repo}\nmilestones:\n  - id: M1\n")
    monkeypatch.setenv("L2_MODE", mode); monkeypatch.setenv("L2_REPO", str(repo))
    m = _mf(tmp_path, monkeypatch, noyau=f"{sys.executable} -c {shlex.quote(NOYAU_TUE)} {RUN_PY}")
    t = _item("pret", 0, projet=str(proj)); F = {"T": t}
    m.lancer(t, F, m.sha_noyau())
    t = m.lire()["T"]
    assert (t["etat"], t["demarrages"][-1].get("technique")) == (etat, technique), (mode, t)
    if mode == "vert":
        assert t["preuve"]["commit"] == "c" * 40, t


def test_ecrire_fsync_du_dossier_apres_remplacement(tmp_path, monkeypatch):
    """File durable : après os.replace, le DOSSIER est fsync (sinon le renommage peut disparaître à la coupure)."""
    m = _mf(tmp_path, monkeypatch)
    vu, vrai_fsync, vrai_replace = [], os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: vu.append(stat.S_ISDIR(os.fstat(fd).st_mode)) or vrai_fsync(fd))
    monkeypatch.setattr(os, "replace", lambda a, b: vu.append("replace") or vrai_replace(a, b))
    m.ecrire({"T": _item("pret", 0)})
    assert "replace" in vu and True in vu[vu.index("replace"):], vu
    assert json.loads((tmp_path / "etat" / "file.json").read_text())["T"]["etat"] == "pret"
