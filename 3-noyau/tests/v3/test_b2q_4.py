"""TEST_4 — CORR_4 : ferme le NO-GO Codex B2ter sur CORR_3 (levier 5, sélection du DERNIER essai ROUGE).
(a) Contre-exemple Codex : deux ROUGE au MÊME finished_at (support.now() horodate à la seconde) ;
max() à clé seule rend le PREMIER de l'index → noms du VIEUX essai dans la consigne. Clé
(finished_at, essai) : reconcilier recopie « essai » du reçu dans chaque ligne d'index, admission le
calcule 1+précédents même base — strictement croissant, le flock sérialise le jalon : pas d'ex æquo.
(b) Tri à identifiants MÉLANGÉS en entrée : le mutant « dédoublonnage+plafond sans tri » rougit.
Test 1 ROUGE sur la référence (assertion métier), VERT après CORR_4 ; test 2 témoin vert avant/après.
Hors ligne (FACTORY_NO_REAL_ENGINE=1), in-process, fixtures reçues en paramètre, jamais appelées."""
import json, pathlib, sys  # noqa: E401

BANC = pathlib.Path(__file__).resolve().parent
if not (BANC / "conftest.py").is_file():  # livré hors tests/v3 : retourner au banc
    BANC = BANC.parents[1] / "DURCI_20260928" / "W_CAND_V13_REEL" / "cand_v8" / "tests" / "v3"
sys.path.insert(0, str(BANC))

import test_durci_v8 as banc  # noqa: E402

_run, noyau = banc._run, banc.noyau
_T = "2026-10-01T01:00:00Z"  # horodatage à la seconde : l'égalité est le cas réel


def _rouge(tmp_path, rouges, u, essai, fini=_T, base="b" * 40):
    """Reçu ROUGE + ligne d'index : mêmes champs que reconcilier recopie, « essai » compris."""
    d = tmp_path / "runs" / u
    d.mkdir(parents=True)
    recu = d / "RECEIPT.json"
    recu.write_text(json.dumps({"schema": noyau.SCHEMA, "run_uuid": u, "milestone": "M1", "hash_contrat": "h",
                                "base_sha": base, "verdict": "ROUGE", "essai": essai, "finished_at": fini,
                                "tests_apres": {"rouges": rouges}}), encoding="utf-8")
    return {"jalon": "M1", "hash_contrat": "h", "base_sha": base, "verdict": "ROUGE", "essai": essai,
            "run_uuid": u, "recu": str(recu), "finished_at": fini}


def _admis(tmp_path, monkeypatch, lignes):
    """Decor complet : tout ce qu'admission()/consigne() lisent, r.repo compris (faux rouges B2bis)."""
    monkeypatch.setattr(noyau, "reconcilier", lambda *a: None)
    monkeypatch.setattr(noyau, "fichiers_base", lambda *a: [])
    monkeypatch.setenv("FACTORY_NO_REAL_ENGINE", "1")
    monkeypatch.setenv("FACTORY_V3_FAKE_DISK_FREE_GB", "50")
    (tmp_path / "index.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lignes), encoding="utf-8")
    r = _run(tmp_path, m={"produit": "p", "but": "b", "spec_ref": "s"}, liv=["src/mod.py"], agv=[],
             task=b"faire\n", repo=tmp_path, moteur=None)
    r.admission()
    return r


def test_egalite_finished_at_les_noms_du_dernier_essai(tmp_path, monkeypatch):
    """Contre-exemple Codex (NO-GO B2ter) : même seconde, index [vieux, dernier] → DERNIER seul."""
    r = _admis(tmp_path, monkeypatch, [_rouge(tmp_path, ["juge/test_x.py::test_vieux"], "u1", 1),
                                       _rouge(tmp_path, ["juge/test_x.py::test_dernier"], "u2", 2)])
    assert r.R["essai"] == 3, "préparation morte : les deux précédents ne sont pas comptés"
    txt = r.consigne()
    assert "test_dernier" in txt and "test_vieux" not in txt


def test_tri_entree_melangee_sortie_triee(tmp_path, monkeypatch):
    """Entrée MÉLANGÉE (+ doublon, 11 uniques) : sortie triée t01..t10 — l'ordre d'entrée ne suffit pas."""
    ids = [f"f/t.py::t{n:02d}" for n in (7, 3, 11, 1, 9, 5, 2, 10, 4, 8, 6)] + ["f/t.py::t03"]
    r = _admis(tmp_path, monkeypatch, [_rouge(tmp_path, ids, "u1", 1)])
    assert r.R["essai"] == 2, "préparation morte : le précédent n'est pas compté"
    txt = r.consigne()
    assert "rouges : " + ", ".join(f"t{n:02d}" for n in range(1, 11)) in txt
    assert "t11" not in txt and txt.count("t03") == 1
