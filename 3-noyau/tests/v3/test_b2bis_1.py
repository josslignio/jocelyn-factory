"""Banc B2bis — CORR_1 : les violations sont TOUJOURS jugées AVANT la panne moteur.

Relecture Codex B2 (CODEX_B2.txt fin, CORR_1 NO-GO) : contrat() classe la panne
avant les violations — un moteur muet qui commite (commit vide : violation,
0 fichier, 0 octet de journal) obtient un essai gratuit (panne_moteur) au lieu
d'une triche. Test PUR in-process, style test_durci_v8.py (helper _run) : seul
git tourne (dépôt jetable), aucun moteur, aucune fixture appelée directement.
Rouge sur le code de référence (test 1 : motif_echec panne_moteur), vert après.
"""

import importlib.util, time, types  # noqa: E401

import pytest

from conftest import RUN_PY

_spec = importlib.util.spec_from_file_location("v3_noyau_b2bis", RUN_PY)
noyau = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(noyau)


def _run(tmp_path, **kw):
    """Run nu (sans __init__ : ni git, ni verrou) ; etape/ecrire/ev écrivent sous tmp_path/run."""
    r = noyau.Run.__new__(noyau.Run)
    (run := tmp_path / "run").mkdir(exist_ok=True)
    r.run, r.seq, r.m0, r.w0, r.uuid, r.jalon, r.enfant, r.lock, r.lockfd = run, 0, noyau.mono(), time.time(), "u", "M1", None, None, None
    r.espace = r.espace_moteur = None
    r.R = {"phases": dict.fromkeys(noyau.PHASES), "etape": None, "quota": None, "base_sha": "b" * 40, "hash_contrat": "h"}
    r.a, r.index = types.SimpleNamespace(keep=False, engine="glm", budget_s=None), tmp_path / "index.jsonl"
    for k, v in kw.items():
        setattr(r, k, v)
    return r


def _espace(tmp_path, commit_moteur):
    """Dépôt jetable (base commit) ; si commit_moteur : commit VIDE du moteur (violation,
    aucun fichier touché). engine.log créé VIDE dans run/ — la panne y est plausible."""
    sp = tmp_path / "espace"
    (sp / "src").mkdir(parents=True); (sp / "src/mod.py").write_text("a\n")  # noqa: E702
    for c in (("init", "-q", "--template="), ("add", "-A"), ("commit", "-q", "-m", "base")):
        noyau.git(sp, *c)
    base = noyau.git(sp, "rev-parse", "HEAD").strip()
    if commit_moteur:
        noyau.git(sp, "commit", "-q", "--allow-empty", "-m", "moteur")
    r = _run(tmp_path, espace=sp, liv=["src/mod.py"], base_commit=base)
    (r.run / "engine.log").write_bytes(b"")
    return r


def test_violation_jugee_avant_panne(tmp_path):
    """Aucune sortie moteur + commit vide (violation) → TRICHE, PAS panne_moteur.

    Noyau de référence : panne_moteur — la triche obtient un essai gratuit."""
    r = _espace(tmp_path, commit_moteur=True)
    with pytest.raises(noyau.Fin) as e:
        r.contrat()
    assert e.value.verdict == "ABORT" and e.value.motif_echec == "triche", (
        e.value.motif, e.value.motif_echec)


def test_aucune_sortie_aucune_violation_panne(tmp_path):
    """Journal vide + usage MESURÉ à 0 (0 fichier, 0 violation) → panne_moteur : l'autre côté tient.
    Requalifié CORR_9 : le silence seul (usage non mesuré) n'est plus une preuve d'absence de
    travail — il faut un 0 explicite ou des erreurs seules (TEST_9.py couvre l'inverse)."""
    r = _espace(tmp_path, commit_moteur=False)
    r.R["engine"] = {"usage": {"output": 0}}
    with pytest.raises(noyau.Fin) as e:
        r.contrat()
    assert e.value.motif_echec == "panne_moteur", (e.value.motif, e.value.motif_echec)


def test_journal_seul_pas_de_panne(tmp_path):
    """Moteur bavard ET travailleur (jetons mesurés, rien touché) : ni panne ni triche —
    le mutant « tout classifier panne » (condition inversée) devient rouge ici.
    Requalifié CORR_6 : la panne se juge sur les jetons mesurés, plus sur le journal vide."""
    r = _espace(tmp_path, commit_moteur=False)
    (r.run / "engine.log").write_bytes(b"parole moteur\n")
    r.R["engine"] = {"usage": {"output": 120}}
    r.contrat()  # aucune Fin levée : du travail mesuré — le juge décidera (échec métier)
