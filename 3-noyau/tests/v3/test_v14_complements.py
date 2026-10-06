"""v14 — compléments du banc (mutation 01/10 16:45 : 2 mutants survivants sur 82).
Mutant 76 (« if (u == 0 or » : la condition « aucun fichier touché » retirée) et mutant 78 (all → any)
restaient VERTS : aucun test n'éprouvait « fichier touché + journal d'erreurs » ni « journal mixte ».
Écrits par l'opérateur (juge), code du noyau inchangé. Helpers repris de test_b6ter_9.py."""
import pytest
from test_b6ter_9 import _noy, _espace, _apres_moteur

def test_fichier_touche_journal_erreur_pas_panne(tmp_path):
    """Un livrable MODIFIÉ + journal d'erreurs seules → PAS panne : du travail a eu lieu, le juge décide."""
    r = _espace(tmp_path)
    (r.espace / "src" / "mod.py").write_text("b\n")
    (r.run / "engine.log").write_text('{"type":"error","message":"Unexpected server error"}\n')
    r.R["engine"] = {"usage": {"output": None}}
    try:
        r.contrat()
    except _noy.Fin as e:
        assert e.motif_echec != "panne_moteur", "fichier touché : jamais un essai gratuit"

def test_journal_mixte_usage_absent_pas_panne(tmp_path, monkeypatch):
    """Erreur + vrai événement de travail, usage non mesuré → PAS panne (TOUS error exigé, pas UN)."""
    r = _apres_moteur(tmp_path, monkeypatch, '{"type":"error","message":"x"}\n{"type":"text","part":{"text":"travail"}}\n')
    assert r.R["engine"]["usage"]["output"] is None
    try:
        r.contrat()
    except _noy.Fin as e:
        assert e.motif_echec != "panne_moteur", "journal mixte : jamais une panne"
