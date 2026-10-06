"""TEST_8 — CORR_8 : 3 pannes au total puis bloque ; usage None ≠ 0 ; HOME moteur conservé sur panne.

Fermeture NO-GO B6 (CODEX_B6.txt fin), style test_b6_6/7 : in-process, FACTORY_NO_REAL_ENGINE=1,
aucune fixture appelée directement, échec sur l'assertion métier ; chemins par Path(__file__)
(cand_v8 = parents[2], racine X_CAND_V16 = parents[3]). Rouge sur le code pré-CORR_8 (tests 1, 2,
4) ; test 3 témoin (vert avant et après : le fait mesuré reste panne)."""
import importlib.util, json, time, types
from pathlib import Path
import pytest
from conftest import RUN_PY

_noy = importlib.util.module_from_spec(_s := importlib.util.spec_from_file_location("v3_noyau_corr8", RUN_PY))
_s.loader.exec_module(_noy)
MF = Path(__file__).resolve().parents[3] / "mini_factory" / "mf.py"  # CORR_8 : racine X_CAND = parents[3]
MOTEUR_GLM = {"id": "glm", "argv": ["/bin/true"], "arret": {}, "defauts": {}, "format_sortie": "opencode_json", "cli_version": "x",
              "config_sha256": "c", "plafonds": {"mecanisme": "aucun", "drapeaux": []}, "usage": {"type": "step_finish", "cumul": True, "champs": {"output": "part.tokens.output"}}}

def _run(tmp_path, **kw):
    r = _noy.Run.__new__(_noy.Run)
    (run := tmp_path / "run").mkdir(exist_ok=True)
    r.run, r.seq, r.m0, r.w0, r.uuid, r.jalon, r.enfant, r.lock, r.lockfd = run, 0, _noy.mono(), time.time(), "u", "M1", None, None, None
    r.espace = r.espace_moteur = None
    r.R = {"phases": dict.fromkeys(_noy.PHASES), "etape": None, "quota": None, "base_sha": "b" * 40, "hash_contrat": "h"}
    r.a, r.index = types.SimpleNamespace(keep=False, engine="glm", budget_s=None), tmp_path / "index.jsonl"
    for k, v in kw.items(): setattr(r, k, v)
    return r

def _apres_moteur(tmp_path, monkeypatch, journal):
    """Dépôt jetable + lancer simulé (pose <journal>, rc 1) : le VRAI lire_usage remplit R["engine"]."""
    sp = tmp_path / "espace"; (sp / "src").mkdir(parents=True); (sp / "src" / "mod.py").write_text("a\n")
    for c in (("init", "-q", "--template="), ("add", "-A"), ("commit", "-q", "-m", "base")): _noy.git(sp, *c)
    monkeypatch.setenv("FACTORY_NO_REAL_ENGINE", "1")  # hermétique : aucun moteur réel
    def faux(moi, argv, env, **k):
        k["log"].write_text(journal)
        return 1, "fin", 0
    monkeypatch.setattr(_noy.Run, "lancer", faux)
    r = _run(tmp_path, espace=sp, liv=["src/mod.py"], base_commit=_noy.git(sp, "rev-parse", "HEAD").strip(), moteur=dict(MOTEUR_GLM), budget=60)
    r.lancer_moteur("consigne")
    return r

def _mf(tmp_path, monkeypatch):
    (tmp_path / "noyau.py").write_text(""); monkeypatch.setenv("MF_ETAT", str(tmp_path / "etat")); monkeypatch.setenv("MF_NOYAU", f"python3 {tmp_path / 'noyau.py'}")
    monkeypatch.setenv("MF_MOTEURS_RELIES", "glm"); monkeypatch.delenv("MF_MOTEUR_FORCE", raising=False)
    s = importlib.util.spec_from_file_location(f"mf_{time.time_ns()}", MF); m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
    m.sha_noyau, m.hash_contrat = (lambda: "h"), (lambda t: t.get("hash_contrat"))
    return m

def test_1re_2e_panne_pret_3e_bloque(tmp_path, monkeypatch):
    """CORR_8a : plafond = 3 pannes au TOTAL — 1re et 2e → pret, la 3e → bloque (rouge sur `<= 3`)."""
    m = _mf(tmp_path, monkeypatch)
    def run(argv, **kw):
        d = tmp_path / f"u{time.time_ns()}"; d.mkdir()
        (d / "recu.json").write_text(json.dumps({"schema": "factory-v3-receipt/3", "milestone": "M1", "verdict": "ABORT",
            "rc": 3, "run_uuid": d.name, "hash_contrat": "hc", "motif_echec": "panne_moteur", "noyau": {}, "commit": {}, "engine": {}}))
        return types.SimpleNamespace(returncode=3, stdout=f"[v3] reçu {d / 'recu.json'}\n")
    monkeypatch.setattr(m, "subprocess", types.SimpleNamespace(run=run, TimeoutExpired=m.subprocess.TimeoutExpired))
    m.ecrire({"T1": {"id": "T1", "projet": "p", "jalon": "M1", "moteur": "glm", "essais": 2, "etat": "pret", "demarrages": [], "reglages": [], "hash_contrat": "hc"}})
    for _ in range(2): m.suivant(m.lire()); assert m.lire()["T1"]["etat"] == "pret"
    m.suivant(m.lire()); t = m.lire()["T1"]
    assert t["etat"] == "bloque" and sum(1 for d in t["demarrages"] if d.get("technique")) == 3

def test_usage_absent_flux_non_erreur_pas_panne(tmp_path, monkeypatch):
    """CORR_8b : aucun step_finish (output None) mais flux non-error → None ≠ 0 : PAS de panne."""
    r = _apres_moteur(tmp_path, monkeypatch, '{"type":"message","part":{"text":"je travaille"}}\n')
    assert r.R["engine"]["usage"]["output"] is None; r.contrat()  # aucune Fin : mutant « None = 0 » rouge

def test_journal_erreur_seule_panne(tmp_path, monkeypatch):
    """Fait mesuré 01/10 : journal réduit à {"type":"error"} → panne (témoin du second bras de (b))."""
    r = _apres_moteur(tmp_path, monkeypatch, '{"type":"error","message":"Unexpected server error"}\n')
    with pytest.raises(_noy.Fin) as e: r.contrat()
    assert e.value.motif_echec == "panne_moteur", (e.value.motif, e.value.motif_echec)

def test_home_moteur_conserve_sur_panne(tmp_path):
    """CORR_8c : terminer() ne purge pas le HOME moteur sur panne_moteur (journal serveur conservé)."""
    r = _run(tmp_path); srv = r.run / "engine" / "home" / ".opencode" / "server.log"
    srv.parent.mkdir(parents=True); (r.run / "engine" / "tmp").mkdir(parents=True); srv.write_text("diagnostic\n")
    assert r.terminer(_noy.Fin("ABORT", 2, "panne moteur : aucun travail", "panne_moteur")) == 2
    assert srv.exists() and not (r.run / "engine" / "tmp").exists()  # rouge si le HOME est purgé
