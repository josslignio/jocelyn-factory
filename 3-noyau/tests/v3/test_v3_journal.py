"""Trou du banc l.135 — borne exacte du journal (TRI_5.md, « 135 | TROU DU BANC »).

copier_log n'écrit le marqueur de troncature QUE si le flux DÉPASSE LOG_MAX octets.
Le mutant `>` → `>=` (run.py l.135) écrit le marqueur pour un journal d'EXACTEMENT
LOG_MAX octets — complet, pas tronqué — à la position LOG_MAX − len(marqueur) :
il écrase les derniers octets réels et annonce une troncature fausse.

Deux côtés (devise 4, obligation 3) :
- pas de troncature : un flux de LOG_MAX octets connus → copie octet pour octet,
  sans marqueur (ce test devient ROUGE sur le mutant) ;
- honnête : un flux de LOG_MAX + 1 octets → marqueur présent, avec le bon
  nombre d'octets produits.

Aucun moteur réel (FACTORY_NO_REAL_ENGINE=1 posé par conftest.run_v3).
"""

import importlib.util

from conftest import RUN_PY

_spec = importlib.util.spec_from_file_location("v3_noyau_pour_journal", RUN_PY)
noyau = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(noyau)


def _octets_connus(total):
    """total octets déterministes (rejouables sur les mêmes octets), sans « LOG TRONQU »."""
    motif = bytes((i * 31 + 7) % 251 for i in range(256))
    assert b"LOG TRONQU" not in motif
    return motif * (total // 256) + motif[:total % 256]


class _Flux:
    """Faux flux moteur : read1 rend au plus size octets, b"" une fois épuisé."""

    def __init__(self, donnees):
        self.donnees, self._pos = donnees, 0

    def read1(self, size=-1):
        tranche = self.donnees[self._pos:self._pos + size]
        self._pos += len(tranche)
        return tranche


def _copier(donnees, tmp_path):
    sortie = tmp_path / "engine.log"
    noyau.copier_log(_Flux(donnees), sortie)
    return sortie.read_bytes()


def test_l135_journal_de_log_max_octets_nest_pas_tronque(tmp_path):
    """LOG_MAX octets pile : copie fidèle octet pour octet, aucun marqueur."""
    donnees = _octets_connus(noyau.LOG_MAX)
    lu = _copier(donnees, tmp_path)
    assert lu == donnees, (
        "journal complet altéré : %d octets écrits pour %d émis — "
        "des octets réels ont été écrasés" % (len(lu), len(donnees)))
    assert b"LOG TRONQU" not in lu, \
        "marqueur de troncature annoncé sur un journal qui n'était PAS tronqué"


def test_l135_journal_de_log_max_plus_1_octets_porte_le_marqueur(tmp_path):
    """LOG_MAX + 1 octets : marqueur présent, avec le vrai nombre d'octets produits."""
    lu = _copier(_octets_connus(noyau.LOG_MAX + 1), tmp_path)
    attendu = ("\n[factory_v3] LOG TRONQUÉ : %d octets produits\n"
               % (noyau.LOG_MAX + 1)).encode()
    assert len(lu) <= noyau.LOG_MAX
    assert attendu in lu, "marqueur absent ou n faux ; fin du journal : %r" % lu[-120:]
