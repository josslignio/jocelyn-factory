"""TEST_9 — CORR_9 : all([]) n'est jamais « erreurs seules » — ≥ 1 événement JSON exigé.

Fermeture relecture Codex B6bis (CODEX_B6BIS.txt fin, partie (1)) : le bras « journal » de
CORR_8 classait PANNE un journal vide après filtrage (texte non JSON, ou 0 octet) même avec
un usage positif mesuré — test_journal_seul_pas_de_panne (test_b2bis_1.py) est ROUGE sur
X_CAND_V16. Style TEST_8 : in-process, FACTORY_NO_REAL_ENGINE=1, aucune fixture appelée
directement, échec sur l'assertion métier ; noyau par conftest (RUN_PY = parents[2]).
Rouge sur X_CAND_V16 actuel (tests 1, 3) ; test 2 témoin (le fait mesuré reste panne)."""
import importlib.util, time, types
import pytest
from conftest import RUN_PY

_noy = importlib.util.module_from_spec(_s := importlib.util.spec_from_file_location("v3_noyau_corr9", RUN_PY))
_s.loader.exec_module(_noy)
MOTEUR_GLM = {"id": "glm", "argv": ["/bin/true"], "arret": {}, "defauts": {}, "format_sortie": "opencode_json", "cli_version": "x",
              "config_sha256": "c", "plafonds": {"mecanisme": "aucun", "drapeaux": []},
              "usage": {"type": "step_finish", "cumul": True, "champs": {"output": "part.tokens.output"}}}

def _run(tmp_path, **kw):
    r = _noy.Run.__new__(_noy.Run)
    (run := tmp_path / "run").mkdir(exist_ok=True)
    r.run, r.seq, r.m0, r.w0, r.uuid, r.jalon, r.enfant, r.lock, r.lockfd = run, 0, _noy.mono(), time.time(), "u", "M1", None, None, None
    r.espace = r.espace_moteur = None
    r.R = {"phases": dict.fromkeys(_noy.PHASES), "etape": None, "quota": None, "base_sha": "b" * 40, "hash_contrat": "h"}
    r.a, r.index = types.SimpleNamespace(keep=False, engine="glm", budget_s=None), tmp_path / "index.jsonl"
    for k, v in kw.items(): setattr(r, k, v)
    return r

def _espace(tmp_path):
    """Dépôt jetable (base commit, rien touché) + Run nu : contrat() jouable directement."""
    sp = tmp_path / "espace"; (sp / "src").mkdir(parents=True); (sp / "src" / "mod.py").write_text("a\n")
    for c in (("init", "-q", "--template="), ("add", "-A"), ("commit", "-q", "-m", "base")): _noy.git(sp, *c)
    return _run(tmp_path, espace=sp, liv=["src/mod.py"], base_commit=_noy.git(sp, "rev-parse", "HEAD").strip())

def _apres_moteur(tmp_path, monkeypatch, journal):
    """lancer simulé (pose <journal>, rc 1) : le VRAI lire_usage remplit R["engine"]."""
    r = _espace(tmp_path)
    monkeypatch.setenv("FACTORY_NO_REAL_ENGINE", "1")  # hermétique : aucun moteur réel
    def faux(moi, argv, env, **k):
        k["log"].write_text(journal)
        return 1, "fin", 0
    monkeypatch.setattr(_noy.Run, "lancer", faux)
    r.moteur, r.budget = dict(MOTEUR_GLM), 60
    r.lancer_moteur("consigne")
    return r

def test_journal_texte_jetons_positifs_pas_panne(tmp_path):
    """0 fichier + journal TEXTE (0 événement JSON après filtrage) + 120 jetons mesurés → PAS
    panne : le mutant « all([]) = erreurs seules » devient rouge ici (retour de
    test_journal_seul_pas_de_panne, même fait, construction indépendante)."""
    r = _espace(tmp_path)
    (r.run / "engine.log").write_bytes(b"parole moteur\n")
    r.R["engine"] = {"usage": {"output": 120}}
    r.contrat()  # aucune Fin levée : du travail mesuré — le juge décide (échec métier)

def test_erreur_seule_usage_absent_panne(tmp_path, monkeypatch):
    """Témoin (fait mesuré 01/10) : UNE ligne {"type":"error"} + usage non mesuré → panne."""
    r = _apres_moteur(tmp_path, monkeypatch, '{"type":"error","message":"Unexpected server error"}\n')
    assert r.R["engine"]["usage"]["output"] is None, "aucun step_finish = usage non mesuré"
    with pytest.raises(_noy.Fin) as e: r.contrat()
    assert e.value.motif_echec == "panne_moteur", (e.value.motif, e.value.motif_echec)

def test_journal_vide_usage_absent_pas_panne(tmp_path, monkeypatch):
    """0 fichier + journal VIDE + usage non mesuré → PAS panne : le silence n'est pas une
    preuve (« AU MOINS UN événement et TOUS error ») ; le juge décide."""
    r = _apres_moteur(tmp_path, monkeypatch, "")
    assert r.R["engine"]["usage"]["output"] is None and not _noy.lire_jsonl(r.run / "engine.log")
    r.contrat()  # aucune Fin levée : all([]) ne vaut jamais « erreurs seules »
