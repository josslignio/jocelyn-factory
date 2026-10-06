"""v17 — reprise d'un « en_cours » orphelin sur FAITS (mf/chaîne tués, Mac redémarré).

Règles éprouvées : verrou de vie (hérité par le noyau) ou flock noyau (dépôt, jalon) TENU → en_cours ; LIBRE → le reçu
FINAL le plus récent du contrat épinglé fait foi, un VERT prime toujours ; aucun reçu final → panne technique (plafond 3) ;
ambigu → inconnu, jamais de relance. Tout passe par l'API existante (suivant / lancer) : un rouge sur la copie v14 est un
rouge d'ASSERTION, jamais un import cassé. Hermétique : aucun moteur, aucun run.py réel (le « noyau » de bout en bout est
un sommeil Python qui ne prend aucun flock — la fenêtre avant flock, rendue arbitrairement longue)."""
import fcntl, importlib.util, json, os, shlex, signal, subprocess, sys, time, types  # noqa: E401
from pathlib import Path
import pytest
from conftest import RUN_PY

MF = RUN_PY.parents[2] / "mini_factory" / "mf.py"
HC, T0 = "a" * 64, "2026-10-02T10:00:00Z"                      # contrat épinglé ; démarrage (UTC, format du reçu)


def _mf(tmp_path, monkeypatch, relies=""):
    """mf chargé sur le VRAI run.py (résolution dépôt/verrous/reçus du noyau lui-même), lancements espionnés."""
    monkeypatch.setenv("MF_ETAT", str(tmp_path / "etat")); monkeypatch.setenv("MF_NOYAU", f"{sys.executable} {RUN_PY}")
    monkeypatch.setenv("MF_MOTEURS_RELIES", relies); monkeypatch.delenv("MF_MOTEUR_FORCE", raising=False)
    s = importlib.util.spec_from_file_location(f"mf_v17_{time.time_ns()}", MF); m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
    lances = []
    monkeypatch.setattr(m, "subprocess", types.SimpleNamespace(run=lambda a, **k: lances.append(a) or types.SimpleNamespace(
        returncode=0, stdout=HC if "--hash-contrat" in a else ""), TimeoutExpired=subprocess.TimeoutExpired))
    return m, lances


def _decor(tmp_path):
    repo, proj = tmp_path / "repo", tmp_path / "proj"
    repo.mkdir(); proj.mkdir()
    for c in (["init", "-q"], ["-c", "user.email=b@v", "-c", "user.name=b", "commit", "-q", "--allow-empty", "-m", "base"]):
        subprocess.run(["git", "-C", str(repo), *c], check=True, capture_output=True)
    (proj / "ROADMAP.yaml").write_text(f"product_repo: {repo}\nmilestones:\n  - id: M1\n")
    return repo, proj


def _item(m, proj, etat="en_cours", **d):
    dem = {"t": "2026-10-02 17:00:00", "utc": T0, "noyau": m.sha_noyau(), "mf": m.MF_SHA, "moteur": "glm", **d}
    return {"id": "T", "projet": str(proj), "jalon": "M1", "moteur": "glm", "essais": 2, "etat": etat, "reglages": [],
            "hash_contrat": HC, "demarrages": [dem]}


def _recu(repo, u, **kw):
    R = {"schema": "factory-v3-receipt/3", "run_uuid": u, "milestone": "M1", "hash_contrat": HC, "base_sha": "b" * 40,
         "verdict": "VERT", "rc": 0, "commit": {"sha": "c" * 40}, "noyau": {"flags": []}, "engine": {},
         "started_at": "2026-10-02T10:00:05Z", "finished_at": "2026-10-02T10:10:00Z"} | kw
    (d := repo / ".factory_v3" / "runs" / u).mkdir(parents=True)
    (d / "RECEIPT.json").write_text(json.dumps(R))


def _suivant(m, t):
    F = {"T": t}; m.ecrire(F)
    rc = m.suivant(m.lire())
    return rc, m.lire()["T"]


def test_flock_noyau_tenu_reste_en_cours(tmp_path, monkeypatch):
    """Noyau vivant sous son flock (dépôt, jalon) : l'orphelin reste en_cours, rien n'est relancé ni décompté."""
    repo, proj = _decor(tmp_path); m, lances = _mf(tmp_path, monkeypatch, relies="glm")
    (p := repo / ".git" / "factory_v3" / "locks" / "M1.lock").parent.mkdir(parents=True)
    fd = os.open(p, os.O_CREAT | os.O_RDONLY); fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        rc, t = _suivant(m, _item(m, proj))
    finally:
        os.close(fd)
    assert (rc, t["etat"], lances) == (2, "en_cours", []), (rc, t["etat"], lances)
    assert "technique" not in t["demarrages"][0]


def test_vert_puis_refus_doublon_le_vert_gagne(tmp_path, monkeypatch):
    """Contre-exemple Codex B5 : VERT puis REFUS doublon POSTÉRIEUR du même contrat → accepte, jamais bloque."""
    repo, proj = _decor(tmp_path); m, lances = _mf(tmp_path, monkeypatch)
    _recu(repo, "u-vert")
    _recu(repo, "u-doublon", verdict="REFUS", rc=3, commit=None, started_at="2026-10-02T10:20:00Z", finished_at="2026-10-02T10:20:01Z")
    _, t = _suivant(m, _item(m, proj))
    assert t["etat"] == "accepte", t["etat"]
    assert t["preuve"]["commit"] == "c" * 40 and t["preuve"]["run_uuid"] == "u-vert" and lances == []


@pytest.mark.parametrize("verdict,rc,motif,etat,technique", [("ROUGE", 1, None, "pret", False), ("REFUS", 3, None, "bloque", False),
                                                              ("ABORT", 2, "panne_moteur", "pret", True)])
def test_dernier_recu_final_fait_foi(tmp_path, monkeypatch, verdict, rc, motif, etat, technique):
    """Sans VERT : le reçu final le plus récent du contrat décide (ROUGE → essai métier, REFUS → bloque, panne → technique)."""
    repo, proj = _decor(tmp_path); m, lances = _mf(tmp_path, monkeypatch)
    _recu(repo, "u-ancien", verdict="ROUGE", rc=1, commit=None, finished_at="2026-10-02T10:01:00Z")
    _recu(repo, "u-dernier", verdict=verdict, rc=rc, commit=None, motif_echec=motif, finished_at="2026-10-02T10:30:00Z")
    _, t = _suivant(m, _item(m, proj))
    assert (t["etat"], bool(t["demarrages"][0].get("technique")), lances) == (etat, technique, []), t
    assert t["dernier"]["recu"].endswith("u-dernier/RECEIPT.json")


@pytest.mark.parametrize("pannes,etat", [(0, "pret"), (2, "bloque")])
def test_aucun_recu_final_panne_technique(tmp_path, monkeypatch, pannes, etat):
    """Mac redémarré : reçu resté RUNNING, verrous libres → panne technique ; la 3e panne au total bloque."""
    repo, proj = _decor(tmp_path); m, lances = _mf(tmp_path, monkeypatch)
    _recu(repo, "u-tue", verdict="RUNNING", rc=None, commit=None, finished_at=None)
    t0 = _item(m, proj); t0["demarrages"][:0] = [{"t": "x", "technique": True} for _ in range(pannes)]
    _, t = _suivant(m, t0)
    assert (t["etat"], t["demarrages"][-1].get("technique"), lances) == (etat, True, []), t


def test_recus_d_autres_runs_ignores(tmp_path, monkeypatch):
    """Reçus VERT d'un autre contrat, d'un autre jalon, ou d'un essai ANTÉRIEUR au démarrage : aucun n'est l'issue."""
    repo, proj = _decor(tmp_path); m, lances = _mf(tmp_path, monkeypatch)
    _recu(repo, "u-contrat", hash_contrat="d" * 64)
    _recu(repo, "u-jalon", milestone="M2")
    _recu(repo, "u-avant", started_at="2026-10-02T09:00:00Z", finished_at="2026-10-02T09:30:00Z")
    _, t = _suivant(m, _item(m, proj))
    assert (t["etat"], t["demarrages"][-1].get("technique")) == ("pret", True), t
    assert "preuve" not in t and lances == []


@pytest.mark.parametrize("cas", ["rc_false", "dossier_autre_uuid", "flags_entier", "bases_melees", "noyau_change", "demarrage_v14"])
def test_ambigu_inconnu_jamais_relance(tmp_path, monkeypatch, cas):
    """TÉMOIN (vert sur v14, qui met tout orphelin en inconnu) : il garde le correctif d'accepter ou de relancer sur un fait
    douteux. demarrage_v14 = démarrage sans verrou de vie : la fenêtre avant flock n'est pas exclue → inconnu, pas de relance."""
    repo, proj = _decor(tmp_path); m, lances = _mf(tmp_path, monkeypatch, relies="glm")
    R = {"rc_false": {"rc": False}, "dossier_autre_uuid": {"run_uuid": "u-autre"}, "flags_entier": {"noyau": {"flags": 42}},
         "demarrage_v14": {"verdict": "RUNNING", "rc": None}}.get(cas, {})
    _recu(repo, "u1", **R)
    if cas == "bases_melees":
        _recu(repo, "u2", verdict="ROUGE", rc=1, commit=None, base_sha="e" * 40, finished_at="2026-10-02T10:40:00Z")
    t0 = _item(m, proj, **({"noyau": "0" * 64} if cas == "noyau_change" else {}))
    if cas == "demarrage_v14":
        del t0["demarrages"][0]["utc"]
    rc, t = _suivant(m, t0)
    assert (rc, t["etat"], lances) == (2, "inconnu", []), (cas, t["etat"], lances)


def test_lancer_recu_vert_rc_false_jamais_accepte(tmp_path, monkeypatch):
    """Contre-exemple Codex B5 : `False == 0` en Python ; un reçu VERT au rc booléen n'est pas un VERT."""
    repo, _ = _decor(tmp_path); m, _ = _mf(tmp_path, monkeypatch)
    _recu(repo, "u1", rc=False)
    p = repo / ".factory_v3" / "runs" / "u1" / "RECEIPT.json"
    monkeypatch.setattr(m, "subprocess", types.SimpleNamespace(run=lambda a, **k: types.SimpleNamespace(
        returncode=0, stdout=f"[v3] reçu {p}\n"), TimeoutExpired=subprocess.TimeoutExpired))
    t = _item(m, tmp_path, etat="pret"); t["demarrages"] = []
    m.lancer(t, {"T": t}, m.sha_noyau())
    assert t["etat"] == "inconnu", t["etat"]


def test_lancer_refuse_si_un_noyau_precedent_vit(tmp_path, monkeypatch):
    """Verrou de vie tenu (noyau d'un lancement précédent vivant) : aucun second lancement, aucun démarrage décompté."""
    _, proj = _decor(tmp_path); m, lances = _mf(tmp_path, monkeypatch)
    (tmp_path / "etat").mkdir()
    fd = os.open(tmp_path / "etat" / "vie.lock", os.O_CREAT | os.O_RDONLY); fcntl.flock(fd, fcntl.LOCK_EX)
    t = _item(m, proj, etat="pret"); t["demarrages"] = []
    try:
        rc = m.lancer(t, {"T": t}, m.sha_noyau())
    finally:
        os.close(fd)
    assert (rc, lances, t["demarrages"]) == (2, [], []), (rc, lances, t["demarrages"])


def test_bout_en_bout_mf_tue_noyau_vivant_avant_son_flock(tmp_path, monkeypatch):
    """Vrai mf en sous-processus, tué par SIGKILL pendant que son « noyau » vit SANS flock (fenêtre avant flock) :
    la reprise ne relance pas (en_cours) ; une fois le noyau mort sans reçu final : panne technique, pret."""
    _, proj = _decor(tmp_path); pid_f = tmp_path / "noyau.pid"
    code = (f"import os,sys,time; print('{HC}') if '--hash-contrat' in sys.argv else "
            f"(open({str(pid_f)!r},'w').write(str(os.getpid())), time.sleep(120))")
    env = {**os.environ, "MF_ETAT": str(tmp_path / "etat"), "MF_MOTEURS_RELIES": "glm", "FACTORY_NO_REAL_ENGINE": "1",
           "MF_NOYAU": f"{sys.executable} -c {shlex.quote(code)} {RUN_PY}"}
    env.pop("MF_MOTEUR_FORCE", None)
    m, lances = _mf(tmp_path, monkeypatch)
    t = _item(m, proj, etat="pret"); t["demarrages"] = []; m.ecrire({"T": t})
    pilote = subprocess.Popen([sys.executable, str(MF), "suivant"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    noyau = None
    try:
        for _ in range(200):
            if pid_f.exists() and pid_f.read_text():
                break
            time.sleep(0.05)
        noyau = int(pid_f.read_text())
        pilote.send_signal(signal.SIGKILL); pilote.wait(10)
        assert m.lire()["T"]["etat"] == "en_cours"            # décor : démarrage compté avant l'appel
        rc = m.suivant(m.lire()); t = m.lire()["T"]
        assert (rc, t["etat"], lances) == (2, "en_cours", []), ("noyau vivant avant flock", t["etat"], lances)
        os.kill(noyau, signal.SIGKILL)
        for _ in range(200):
            try:
                os.kill(noyau, 0); time.sleep(0.05)
            except ProcessLookupError:
                break
        m.suivant(m.lire()); t = m.lire()["T"]
        assert (t["etat"], t["demarrages"][-1].get("technique"), lances) == ("pret", True, []), t
    finally:
        pilote.kill()
        if noyau:
            with __import__("contextlib").suppress(ProcessLookupError):
                os.kill(noyau, signal.SIGKILL)
