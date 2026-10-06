"""Mini-factory séquentielle minimale (arbitrage CTO D4 ; STRATEGIE_NOYAU_MINI_FACTORY §4). Une file persistante, UN travail actif, un appel au noyau
par pas, démarrage compté sur disque AVANT l'appel, aucune relance d'une issue inconnue, aide exploitable. Pas une plateforme : ni service, ni concurrence.
Usage : mf.py ajouter ID --projet P --jalon M [--moteur glm] [--essais 2] | mf.py suivant
      | mf.py regler ID pret|bloque --raison T | mf.py statut — ajouter/suivant exigent MF_NOYAU (… run.py)"""
import argparse, contextlib, fcntl, hashlib, importlib.util, json, os, re, shlex, subprocess, sys, time, types
from pathlib import Path

ETAT = Path(os.environ.get("MF_ETAT", Path.home() / ".mini_factory")); FILE = ETAT / "file.json"
NOYAU = shlex.split(os.environ.get("MF_NOYAU", ""))  # C:A13 : aucun défaut (ajouter/suivant : refus rc 3)
FICHIERS = ("run.py", "support.py", "juge_isole.py", "bornage.sb", "ENGINES.lock", "CONSIGNE.md.tmpl")  # + moteurs/
MF_SHA = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()  # R3 : le programme qui accepte les reçus
DELAI_S = 6 * 5400  # v9 : > moteur + 4 passes de suite (5 × BUDGET_MAX 5400 de run.py) + 5400 de marge (git, arrêts)
RELIES = set(filter(None, os.environ.get("MF_MOTEURS_RELIES", "").split(",")))   # D3 : moteurs dont compte mesuré = compte débité est ÉTABLI
VERDICTS = {"VERT": "accepte", "ROUGE": "a_corriger", "REFUS": "bloque"}         # tout le reste (ABORT, RUNNING, absent, illisible) = inconnu
RC = {"VERT": 0, "ROUGE": 1, "ABORT": 2, "REFUS": 3}                             # v17 : rc attendu d'un reçu final relu hors appel
TRANS = {"inconnu": ("pret", "bloque"), "bloque": ("pret", "bloque"), "pret": ("bloque",)}  # v10 : sortir un "pret" jamais lancé ; "en_cours" toujours hors de portée

def lire():
    return json.loads(FILE.read_text()) if FILE.exists() else {}

def ecrire(F):
    ETAT.mkdir(parents=True, exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        f.write(json.dumps(F, indent=1, ensure_ascii=False)); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, FILE)
    d = os.open(ETAT, os.O_RDONLY); os.fsync(d); os.close(d)  # L2 : le renommage lui-même survit à la coupure (comme support.ecrire_octets du noyau)

def sha_noyau():
    """B:9/R2 : empreinte des SEULS fichiers exécutés ou consommés par le noyau (ni README, ni sauvegarde)."""
    d, h = Path(NOYAU[-1]).resolve().parent, hashlib.sha256()
    for f in [d / n for n in FICHIERS] + sorted((d / "moteurs").glob("*.json")):
        h.update(f"{f.relative_to(d).as_posix()}\0{hashlib.sha256(f.read_bytes()).hexdigest()}\n".encode())
    return h.hexdigest()

def hash_contrat(t):
    """Empreinte du contrat calculée par le noyau lui-même (--hash-contrat) ; None si illisible ou hors délai."""
    argv = NOYAU + ["--hash-contrat", "--project", t["projet"], "--milestone", t["jalon"]]
    with contextlib.suppress(subprocess.TimeoutExpired):
        p = subprocess.run(argv, capture_output=True, text=True, timeout=120)
        return p.stdout.strip() if p.returncode == 0 and re.fullmatch(r"[0-9a-f]{64}", p.stdout.strip()) else None

def aide(t, blocage, choix):
    print(f"AIDE travail={t['id']} jalon={t['jalon']} moteur={t['moteur']} état={t['etat']} démarrages={len(t['demarrages'])}/{t['essais']} "
          f"dernier={t.get('dernier')} coût=inconnu s'il n'est pas dans le reçu\n  blocage : {blocage}\n  choix demandé : {choix}")
    return 2

def recevable(R, t, recu, rc):
    """Schéma, types, jalon, rc, dossier = run_uuid, contrat épinglé ; jamais test-only ; VERT = rc 0 ET commit."""
    ok = isinstance(R, dict) and R.get("schema") == "factory-v3-receipt/3" and R.get("milestone") == t["jalon"]
    ok = ok and isinstance(R.get("verdict"), str) and isinstance(R.get("run_uuid"), str)  # R3 : types
    ok = ok and all(isinstance(R.get(k) or {}, dict) for k in ("noyau", "commit", "engine"))
    ok = ok and type(R.get("rc")) is type(rc) and R.get("rc") == rc and bool(R.get("run_uuid")) and Path(recu).parent.name == R["run_uuid"]  # v17 : False ≠ 0
    ok = ok and R.get("hash_contrat") == t.get("hash_contrat")
    ok = ok and "test_only" not in ((R.get("noyau") or {}).get("flags") or [])
    return ok and (R.get("verdict") != "VERT" or (rc == 0 and bool((R.get("commit") or {}).get("sha"))))

def tenu(p):
    """v17 : un processus vivant détient-il le flock de p ? Fichier absent = libre ; toute autre erreur remonte (→ inconnu)."""
    try: fd = os.open(p, os.O_RDONLY)  # noqa: E701
    except FileNotFoundError: return False  # noqa: E701
    try: return fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # None : libre  # noqa: E701
    except BlockingIOError: return True  # noqa: E701
    finally: os.close(fd)  # noqa: E701

def reprendre(t):
    """v17 : « en_cours » orphelin résolu sur FAITS. Verrou de vie (hérité par le noyau, tenu dès sa naissance) ou flock noyau
    (dépôt, jalon) tenu → reste en_cours ; sinon reçu final le plus récent du contrat épinglé, un VERT prime ; aucun → panne
    technique ; démarrage d'avant v17 (fenêtre avant flock non exclue), noyau changé, reçu douteux, bases mêlées → inconnu."""
    d = (t["demarrages"] or [{}])[-1]
    try:
        if not d.get("utc") or d.get("noyau") != sha_noyau():
            raise ValueError("démarrage sans verrou de vie, ou noyau changé depuis")
        if tenu(ETAT / "vie.lock"): return  # noqa: E701
        s = importlib.util.spec_from_file_location("noyau_v3_mf", NOYAU[-1]); k = importlib.util.module_from_spec(s); s.loader.exec_module(k)
        repo = k.charger(t["projet"], t["jalon"], None)[2]  # résolution du noyau lui-même, jamais une copie
        if tenu(k.git_common(repo) / "factory_v3" / "locks" / f"{t['jalon']}.lock"): return  # noqa: E701
        recus = []
        for p in (k.runtime_base(repo) / "runs").glob("*/RECEIPT.json"):
            R = json.loads(p.read_text())
            if R.get("milestone") == t["jalon"] and R.get("hash_contrat") == t["hash_contrat"] and R.get("verdict") != "RUNNING" and R["started_at"] >= d["utc"]:
                if not (recevable(R, t, p, RC.get(R["verdict"])) and isinstance(R["finished_at"], str)):
                    raise ValueError(f"reçu du contrat douteux : {p}")
                recus.append((R["verdict"] == "VERT", R["finished_at"], str(p), R))
        if len({x[3].get("base_sha") for x in recus}) > 1:
            raise ValueError("reçus du contrat sur plusieurs bases")
    except Exception:  # sonde, résolution ou reçu indéterminés : jamais deviné, jamais relancé
        t["etat"] = "inconnu"; return
    _, _, recu, R = max(recus, default=(0, 0, None, None))  # un VERT prime sur tout reçu postérieur (REFUS doublon compris)
    conclure(t, R, recu, R and R["rc"], d["noyau"], panne=R is None)

def pannes(t):  # L2 : budget technique PAR TRAVAIL, cumulé sur tous ses démarrages, jamais remis à zéro (ni regler, ni renommage)
    return sum(1 for d in t["demarrages"] if d.get("technique"))

def vert_doublon(t, R, recu, rc):
    """(b) Codex 03/10 : REFUS « doublon » → le VERT RECEVABLE de même dépôt/jalon/contrat/base fait foi (SON chemin, SON rc) ; sinon le REFUS reste."""
    for p in sorted(Path(recu).parent.parent.glob("*/RECEIPT.json")):
        with contextlib.suppress(OSError, ValueError):
            V = json.loads(p.read_text())
            if recevable(V, t, p, 0) and V["verdict"] == "VERT" and all(R.get(k) and V.get(k) == R.get(k) for k in ("repo", "base_sha")):
                return V, str(p), 0
    return R, recu, rc

def conclure(t, R, recu, rc, h, panne=False):
    if R and R.get("verdict") == "REFUS" and str(R.get("motif")).startswith("doublon"):
        R, recu, rc = vert_doublon(t, R, recu, rc)
    v = R and R.get("verdict")
    # (a) Codex 03/10 : REFUS « quota … inconnu » émis à l'admission, AVANT toute dépense (run.py:327) = panne technique (plafond cumulé de 3)
    avant_depense = v == "REFUS" and re.fullmatch(r"quota \S+ inconnu \(illisible ou absent\) — aucune dépense sans mesure", str(R.get("motif")))
    t["dernier"] = {"rc": rc, "verdict": v, "recu": recu, "cout_usd": R and (R.get("engine") or {}).get("cost_usd")}
    t["etat"] = VERDICTS.get(v, "inconnu")
    if panne or avant_depense or v == "ABORT" and R.get("motif_echec") == "panne_moteur":  # CORR_7 : panne ≠ échec du travail ; v17 : aucun reçu final = panne
        t["demarrages"][-1]["technique"] = True
        t["etat"] = "pret" if pannes(t) < 3 else "bloque"  # CORR_8 : la 3e bloque
    if t["etat"] == "accepte":
        t["preuve"] = {"recu": recu, "run_uuid": R["run_uuid"], "noyau": h, "mf": MF_SHA, "base_sha": R.get("base_sha"),
                       "commit": R["commit"]["sha"], "repo": R.get("repo")}
    if t["etat"] == "a_corriger":
        t["etat"] = "pret" if sum(1 for d in t["demarrages"] if not d.get("technique")) < t["essais"] else "bloque"

def lancer(t, F, h):
    ETAT.mkdir(parents=True, exist_ok=True); vie = os.open(ETAT / "vie.lock", os.O_CREAT | os.O_RDONLY, 0o600)
    try:  # v17 : verrou de VIE, hérité par le noyau dès sa naissance (avant son propre flock) ; mf tué → le noyau le tient seul
        fcntl.flock(vie, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(vie); return aide(t, "verrou de vie tenu : un noyau lancé plus tôt vit encore", "attendre sa fin (aucun second lancement)")
    t["demarrages"].append({"t": time.strftime("%F %T"), "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "noyau": h, "mf": MF_SHA, "moteur": t["moteur"]})
    t["etat"] = "en_cours"
    ecrire(F)  # compté AVANT l'appel
    argv = NOYAU + ["--project", t["projet"], "--milestone", t["jalon"], "--engine", t["moteur"]]
    try:  # R3 : appel borné ; au délai, subprocess.run ne tue que son enfant direct
        p = subprocess.run(argv, capture_output=True, text=True, timeout=DELAI_S, pass_fds=(vie,))
    except subprocess.TimeoutExpired:
        p = types.SimpleNamespace(returncode=None, stdout="")
    finally:
        os.close(vie)
    recu = ([mo.group(1) for mo in re.finditer(r"(?m)^\[v3\] reçu (\S+)$", p.stdout)] or [None])[-1]  # B:1
    R = None
    with contextlib.suppress(OSError, ValueError, TypeError, AttributeError, KeyError):  # R2/R3 : illisible, mal typé
        lu = json.loads(Path(recu).read_text())
        R = lu if recevable(lu, t, recu, p.returncode) and sha_noyau() == h else None  # noyau changé pendant l'appel
    # L2 : noyau tué par un signal (rc < 0) sans reçu recevable, mf vivant → la même réconciliation sur FAITS qu'un orphelin
    reprendre(t) if R is None and (p.returncode or 0) < 0 else conclure(t, R, recu, p.returncode, h)
    v = (t.get("dernier") or {}).get("verdict")
    ecrire(F)
    print(f"{t['id']} {t['jalon']} → {v} ({t['etat']}) reçu={(t.get('dernier') or {}).get('recu')}")
    blocage = f"issue {v or 'sans reçu valide'}"
    blocage += f" (noyau hors délai : {DELAI_S} s)" if p.returncode is None else ""
    return 0 if t["etat"] in ("accepte", "pret") else aide(t, blocage, "examiner le reçu puis `mf.py regler`")

def suivant(F):
    if not Path(NOYAU[-1]).is_file():
        return print(f"REFUS : noyau introuvable ({NOYAU[-1]}) — aucun démarrage") or 3
    for t in F.values():
        if t["etat"] == "en_cours":                            # verrou tenu ici : un « en_cours » est orphelin (pilote mort pendant un appel)
            reprendre(t); ecrire(F)                            # v17 : issue lue sur les FAITS (verrous, reçus), jamais devinée
        if t["etat"] in ("en_cours", "inconnu"):
            return aide(t, f"travail {t['etat']} : un seul travail actif ; issue à réconcilier avant toute suite", "examiner puis `mf.py regler`")
    h = sha_noyau()  # une fois par appel
    for t in F.values():
        if t["etat"] != "pret":
            continue
        if t.get("apres"):  # v9 Q1(1) : --apres retiré ; toute file existante non vide est REFUSÉE, jamais ignorée
            return aide(t, f"champ 'apres'={t['apres']!r} non vide (retiré, v9)", "migrer la file (retirer 'apres')")
        t["moteur"] = os.environ.get("MF_MOTEUR_FORCE") or t["moteur"]   # repli propriétaire 28/09 « quand GLM est mort, switch sur Claude » : moteur écrit au travail, tracé au démarrage
        if t["moteur"] not in RELIES:
            return aide(t, f"compte mesuré non relié au compte débité pour le moteur {t['moteur']} (D3)", "établir le lien puis l'ajouter à MF_MOTEURS_RELIES")
        if sum(1 for d in t["demarrages"] if not d.get("technique")) >= t["essais"]:
            t["etat"] = "bloque"; ecrire(F); return aide(t, "plafond de tentatives atteint", "décider : arrêt ou nouveau contrat")
        if hash_contrat(t) != t.get("hash_contrat"):  # B:9 : contrat épinglé à l'ajout ; changé/illisible = refus
            return aide(t, f"contrat changé depuis l'ajout ({t.get('hash_contrat')})", "nouveau travail")
        return lancer(t, F, h)
    return aide({"id": "-", "jalon": "-", "moteur": "-", "etat": "rien", "demarrages": [], "essais": 0}, "aucun travail prêt", "ajouter ou régler un travail")

def main(argv=None):
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("ajouter"); a.add_argument("id")
    for o, k in (("--projet", {"required": True}), ("--jalon", {"required": True}), ("--moteur", {"default": "glm"}),
                 ("--essais", {"type": int, "default": 2})):
        a.add_argument(o, **k)
    r = sub.add_parser("regler"); r.add_argument("id"); r.add_argument("vers", choices=("pret", "bloque")); r.add_argument("--raison", required=True)
    sub.add_parser("suivant"); sub.add_parser("statut")
    x = ap.parse_args(argv)
    if x.cmd in ("ajouter", "suivant") and not NOYAU:
        return print("REFUS : MF_NOYAU obligatoire (commande du noyau, chemin de run.py en dernier)") or 3
    ETAT.mkdir(parents=True, exist_ok=True)
    with open(ETAT / "verrou", "w") as v:
        try:
            fcntl.flock(v, fcntl.LOCK_EX | fcntl.LOCK_NB)      # un seul écrivain ; libéré par le système si le processus meurt
        except OSError:
            return print("REFUS : une autre mini-factory tourne sur cet état") or 3
        F = lire()
        if x.cmd == "ajouter":
            if x.id in F:
                return print("REFUS : identifiant déjà présent") or 3
            F[x.id] = {"id": x.id, "projet": x.projet, "jalon": x.jalon, "moteur": x.moteur, "essais": x.essais,
                       "etat": "pret", "demarrages": [], "reglages": []}
            if not (hc := hash_contrat(F[x.id])):
                return print("REFUS : empreinte du contrat illisible (--hash-contrat du noyau)") or 3
            F[x.id]["hash_contrat"] = hc
            return ecrire(F) or 0
        if x.cmd == "regler":
            if x.id not in F:
                return print(f"REFUS : travail inconnu : {x.id}") or 3
            t = F[x.id]
            if x.vers not in TRANS.get(t["etat"], ()):
                return print(f"REFUS : {x.id} est {t['etat']}, transition vers {x.vers} interdite") or 3
            if x.vers == "pret" and pannes(t) >= 3:  # L2 : remettre en file ne rachète jamais de pannes techniques
                return print(f"REFUS : {x.id} a épuisé son budget technique ({pannes(t)}/3 pannes) — nouveau contrat") or 3
            t["reglages"].append({"t": time.strftime("%F %T"), "de": t["etat"], "vers": x.vers, "raison": x.raison}); t["etat"] = x.vers
            return ecrire(F) or 0                                # les démarrages déjà comptés ne sont JAMAIS remis à zéro
        if x.cmd == "statut":
            return print(json.dumps(F, indent=1, ensure_ascii=False)) or 0
        return suivant(F)

if __name__ == "__main__":
    sys.exit(main())
