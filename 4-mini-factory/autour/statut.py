"""STATUT (chemin court, étape 7) : « LIVRÉ / RESTANT / BLOQUÉ » par produit, lu UNIQUEMENT dans des faits : reçus RUNNER/integration_<p>.jsonl,
ROADMAP.yaml (dénominateur = TOUS ses jalons), mf_etat (bloque = isolé = NON livré), dépôt réel (origine git de RADAR/<p>/copie) : LIVRÉ = sha_final
ancêtre de HEAD ET livrables présents à HEAD. Heure lue à l'horloge (Bangkok). Ni écriture, ni %. Usage : statut.py [produit …] ; racine = RUNNER_X.
05/10 (audit) B10 : LIVRÉ/RESTANT/BLOQUÉ sur le MÊME périmètre que le runner (ordre_proprietaire, enfants compris ; clé absente = tout), le total
historique sur une ligne à part ; BLOQUÉ = la DERNIÈRE tâche mf du jalon est bloque ET a démarré (une tâche bloquée jamais lancée n'est pas un échec).
B6 : une ligne d'historique illisible (tronquée) est isolée et signalée, le reste compté."""
import json, os, subprocess, sys, time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import yaml
import battement

X = Path(os.environ.get("RUNNER_X") or next(p for p in Path(__file__).resolve().parents if (p / "RADAR").is_dir()))
git = lambda d, *a: subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True)  # noqa: E731


def produit(p):
    rm = yaml.safe_load((X / "RADAR" / p / "projet/ROADMAP.yaml").read_text())
    ms = {m["id"]: m for m in ((rm.get("milestones") if isinstance(rm, dict) else rm) or []) if isinstance(m, dict) and "id" in m}
    f, h = X / "RADAR" / p / "mf_etat/file.json", X / "RUNNER" / f"integration_{p}.jsonl"
    o = (rm.get("ordre_proprietaire", list(ms)) if isinstance(rm, dict) else list(ms)) or []   # B10 : périmètre du runner (runner.roadmap)
    pe, dern = {i for i, m in ms.items() if (m.get("racine") or i) in o}, {}
    for i, t in (json.loads(f.read_text()) if f.exists() else {}).items():
        dern[t.get("jalon", i)] = t   # état COURANT : la dernière tâche du jalon
    bloq = {j for j, t in dern.items() if t.get("etat") == "bloque" and t.get("demarrages")} & pe
    reel = git(X / "RADAR" / p / "copie", "remote", "get-url", "origin").stdout.strip() or "?"
    head, livre, notes = git(reel, "rev-parse", "HEAD").stdout.strip(), {}, []
    if not ms or not head:   # ROADMAP vide ou dépôt réel illisible : jamais « RESTANT 0/0 » muet
        raise ValueError(f"ROADMAP sans jalon ({len(ms)}) ou dépôt réel illisible ({reel})")
    for k, x in enumerate(h.read_text().splitlines() if h.exists() else [], 1):
        try:   # B6 : chaque reçu VALIDÉ (t numérique, integres liste) ; un reçu invalide est isolé, jamais la cécité du statut
            r = {} if not x.strip() else json.loads(x)
            if x.strip() and not (isinstance(r, dict) and isinstance(r.get("t"), (int, float)) and isinstance(r.get("integres") or [], list)):
                raise ValueError("schéma")
        except (ValueError, AttributeError):
            notes.append(f"  ⚠ ligne {k} de {h.name} illisible ou invalide (tronquée ? sans t ?) : isolée, le reste compté"); continue  # noqa: E702
        sha = str(r.get("sha_final"))
        if r.get("integres") and git(reel, "merge-base", "--is-ancestor", sha, "HEAD").returncode:
            notes.append(f"  ⚠ sha_final {sha[:8]} n'est pas un ancêtre de HEAD : {r['integres']} non comptés"); continue  # noqa: E702
        for i in r.get("integres") or []:
            liv = (ms.get(i) or {}).get("livrables") or []
            manq = [x for x in liv if git(reel, "cat-file", "-e", f"HEAD:{x}").returncode]
            if i not in ms or not liv or manq:
                pq = "hors ROADMAP" if i not in ms else f"artefact absent à HEAD {manq or 'aucun livrable'}"
                notes.append(f"  ⚠ {i} non compté : {pq}"); continue  # noqa: E702
            livre[i] = (r["t"], sha, liv[0])
    N, L, B = len(pe), len(pe & set(livre)), len(bloq - set(livre))
    t, i = max(((v[0], k) for k, v in livre.items() if k in pe), default=(None, None))
    age = f"dernière livraison il y a {int(time.time() - t) // 60} min" if t else "aucune livraison prouvée"
    out = [f"{p} : LIVRÉ {L}/{N} — RESTANT {N - L - B}/{N} — BLOQUÉ {B}/{N} — {age}"]
    out += [f"  témoin {i} : {livre[i][2]} présent à HEAD {head[:8]} de {reel}, intégré par {livre[i][1][:8]}"] if i else []
    out += [f"  historique (hors périmètre compris) : LIVRÉ {len(livre)}/{len(ms)} jalons de la ROADMAP"] if len(pe) < len(ms) else []
    return "\n".join(out + notes)


if __name__ == "__main__":
    print(battement.production_statut(X / "RUNNER"))
    print(f"statut lu à {datetime.now(ZoneInfo('Asia/Bangkok')):%d/%m/%Y %H:%M} (Bangkok, horloge) — LIVRÉ = reçu d'intégration vérifié dans le dépôt réel")
    rc = 0
    for p in sys.argv[1:] or ["tiktok", "mailbox", "savespace"]:
        try:
            print(produit(p))
        except Exception as e:  # un produit illisible ne masque pas les autres, et se dit illisible
            print(f"{p} : ILLISIBLE — {type(e).__name__}: {e}"); rc = 1  # noqa: E702
    sys.exit(rc)
