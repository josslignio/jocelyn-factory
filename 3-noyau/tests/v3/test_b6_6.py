"""Banc CORR_6 — panne moteur = aucun travail mesuré (0 jeton de sortie), pas « journal vide ».

Fait mesuré 01/10 14:00-15:30 : 6/8 essais ROUGE en ~1 s — opencode écrivait UNE ligne
{"type":"error",…} (aucun step_finish, 0 jeton, 0 fichier) ; le noyau v13 ne classait
panne_moteur que si engine.log était VIDE. Test PUR in-process, style test_b2bis_1.py :
seul git tourne (dépôt jetable), lancer est simulé, le VRAI lire_usage lit le journal
posé. Rouge sur le noyau de référence (cas 1 : la panne part en échec métier brûlé)."""

import importlib.util, time, types  # noqa: E401

import pytest

from conftest import RUN_PY

_spec = importlib.util.spec_from_file_location("v3_noyau_corr6", RUN_PY)
noyau = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(noyau)

MOTEUR_GLM = {"id": "glm", "argv": ["/bin/true"], "arret": {}, "defauts": {}, "format_sortie": "opencode_json",
              "cli_version": "x", "config_sha256": "c", "plafonds": {"mecanisme": "aucun", "drapeaux": []},
              "usage": {"type": "step_finish", "cumul": True, "champs": {"output": "part.tokens.output"}}}


def _run(tmp_path, **kw):
    r = noyau.Run.__new__(noyau.Run)
    (run := tmp_path / "run").mkdir(exist_ok=True)
    r.run, r.seq, r.m0, r.w0, r.uuid, r.jalon, r.enfant, r.lock, r.lockfd = run, 0, noyau.mono(), time.time(), "u", "M1", None, None, None
    r.espace = r.espace_moteur = None
    r.R = {"phases": dict.fromkeys(noyau.PHASES), "etape": None, "quota": None, "base_sha": "b" * 40, "hash_contrat": "h"}
    r.a, r.index = types.SimpleNamespace(keep=False, engine="glm", budget_s=None), tmp_path / "index.jsonl"
    for k, v in kw.items():
        setattr(r, k, v)
    return r


def _apres_moteur(tmp_path, monkeypatch, journal, commit_moteur=False):
    """Dépôt jetable + lancer_moteur RÉEL (lancer simulé : pose <journal>, rc 1) → Run prêt pour contrat()."""
    sp = tmp_path / "espace"
    (sp / "src").mkdir(parents=True); (sp / "src/mod.py").write_text("a\n")  # noqa: E702
    for c in (("init", "-q", "--template="), ("add", "-A"), ("commit", "-q", "-m", "base")):
        noyau.git(sp, *c)
    base = noyau.git(sp, "rev-parse", "HEAD").strip()
    if commit_moteur: noyau.git(sp, "commit", "-q", "--allow-empty", "-m", "moteur")  # noqa: E701 — violation, 0 fichier
    monkeypatch.setenv("FACTORY_NO_REAL_ENGINE", "1")  # hermétique : aucun moteur réel appelé

    def faux(moi, argv, env, **k):
        k["log"].write_text(journal)
        return 1, "fin", 0
    monkeypatch.setattr(noyau.Run, "lancer", faux)
    r = _run(tmp_path, espace=sp, liv=["src/mod.py"], base_commit=base, moteur=dict(MOTEUR_GLM), budget=60)
    r.lancer_moteur("consigne")  # le vrai lire_usage remplit R["engine"]["usage"] depuis le journal
    return r


def test_ligne_erreur_seule_est_panne_moteur(tmp_path, monkeypatch):
    """Cas mesuré 01/10 : UNE ligne d'erreur, 0 jeton, 0 fichier → ABORT panne_moteur, pas un ROUGE brûlé."""
    r = _apres_moteur(tmp_path, monkeypatch, '{"type":"error","message":"Unexpected server error"}\n')
    assert r.R["engine"]["usage"]["output"] is None, "aucun événement d'usage = aucun travail mesuré"
    with pytest.raises(noyau.Fin) as e:
        r.contrat()
    assert e.value.verdict == "ABORT" and e.value.motif_echec == "panne_moteur", (e.value.motif, e.value.motif_echec)


def test_jetons_sans_fichier_reste_echec_metier(tmp_path, monkeypatch):
    """Jetons produits mais 0 fichier : du travail → PAS panne ; contrat passe, le juge décide → ROUGE."""
    r = _apres_moteur(tmp_path, monkeypatch, '{"type":"step_finish","part":{"tokens":{"output":120}}}\n')
    assert r.R["engine"]["usage"]["output"] == 120, "le mutant « tout classifier panne » devient rouge ici"
    r.contrat()  # aucune Fin levée : pas de panne déclarée pour un moteur qui a travaillé
    monkeypatch.setattr(noyau.Run, "jouer_tests", lambda moi, ph: (1, {"tests/t.py::a": "fail"}))
    monkeypatch.setattr(noyau.Run, "copier_livrables", lambda *a: None)
    r.afp, r.agv, r.base_cases = ["tests/t.py::a"], [], {}
    with pytest.raises(noyau.Fin) as e:
        r.tests_apres()
    assert e.value.verdict == "ROUGE" and e.value.motif_echec == "tests/t.py::a", (e.value.verdict, e.value.motif_echec)


def test_violation_avec_0_jeton_reste_triche(tmp_path, monkeypatch):
    """Violation (commit du moteur) + 0 jeton : jugée AVANT la panne — la triche n'obtient pas d'essai gratuit."""
    r = _apres_moteur(tmp_path, monkeypatch, '{"type":"error","message":"Unexpected server error"}\n', commit_moteur=True)
    with pytest.raises(noyau.Fin) as e:
        r.contrat()
    assert e.value.verdict == "ABORT" and e.value.motif_echec == "triche", (e.value.motif, e.value.motif_echec)
