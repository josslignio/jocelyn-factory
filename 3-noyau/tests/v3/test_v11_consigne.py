"""V11 (#23 ANALYSE.md vidéos) : la consigne demande au moteur de relancer lui-même les tests
nommés et de corriger avant de s'arrêter. Rien de plus : cette phrase n'entre dans aucun verdict
(devise 3), elle est vérifiée ici comme texte, pas comme comportement du moteur."""

from test_durci_v8 import V3, _run


def test_consigne_tmpl_demande_de_relancer_les_tests():
    txt = (V3 / "CONSIGNE.md.tmpl").read_text(encoding="utf-8")
    assert "Relance toi-même les tests" in txt and "corrige" in txt, txt


def test_consigne_rendue_contient_la_phrase_relance(tmp_path):
    m = {"produit": "p", "but": "corriger", "spec_ref": "s.md#x"}
    r = _run(tmp_path, m=m, jalon="M1", liv=["a.py"], agv=[])
    r.task = b"la tache"
    txt = r.consigne()
    assert "Relance toi-même les tests" in txt
