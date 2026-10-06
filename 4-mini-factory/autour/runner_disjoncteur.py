"""RUNNER (DECISION 03/10) — la SEULE boucle qui lance du travail. Tour équitable ; par produit et par tour UNE opération :
étape prête → mf.py suivant ; sinon lot (plan → préparation → relecture qui note → acceptation → 1 correction → mf ajouter).
Un échec n'arrête jamais la boucle ; 3 erreurs consécutives = pause 1 h ; état écrit avant et après chaque opération.
Disjoncteur (DECISION 04/10) : 3 lots abandonnés d'affilée sur un produit = plus aucun lot pour lui jusqu'au fichier RUNNER/REPRISE_<produit>.
Usage : runner.py [produit …] (défaut tiktok savespace mailbox) ; racine .context = RUNNER_X ; arrêt propre : fichier RUNNER/STOP."""
import fcntl, json, os, pwd, re, shutil, subprocess, sys, time
from pathlib import Path

CODE = Path(__file__).resolve().parent
X = Path(os.environ.get("RUNNER_X", CODE.parent)).resolve()
B, ICI, REST, ENVD = X / "RADAR", X / "RUNNER", X / "CLOTURE_20260927", X / "BUILD_V3_2026-09-25"
QUOTA = X.parent / "factory_v2/quota_live.py"
PY = sys.executable  # chemin absolu : sous launchd, le « python3 » du PATH (Homebrew) n'a pas yaml
PRODUITS = sys.argv[1:] or ["tiktok", "savespace", "mailbox"]
MOTEURS = os.environ.get("RUNNER_MOTEURS", "claude codex").split()  # ordre de repli de chaine_mf.sh
RELECTEUR = os.environ.get("RUNNER_RELECTEUR", "codex")
COMMUN = {"tiktok": "COMMUN_TIKTOK_BRIEF", "savespace": "COMMUN_SAVESPACE", "mailbox": "COMMUN_MAILBOX"}
DOC = {"tiktok": "TIKTOK", "savespace": "DISK_CLEAN", "mailbox": "INBOX_CLEAN"}
PHASES = {"plan": ("claude", 1, None, (("COMMUN.txt", "plan"), ("P1.txt", "vide"))), "accepter": (None, 0, None, ()),  # moteur, passes, 1re, consignes
          "prep": ("claude", 3, None, (("COMMUN.txt", "prep_commun"), ("P1.txt", "prep1"), ("P2.txt", "prep2"), ("P3.txt", "prep3"))),
          "relire": (RELECTEUR, 9, 9, (("P9.txt", "relire"),)), "corriger": ("claude", 1, None, (("P1.txt", "corriger"),))}
MAX_ERR, PAUSE_S, REESSAI_S, SOMME_S = 3, 3600, 300, int(os.environ.get("RUNNER_SOMME_S", 300))  # REESSAI_S : ex-chaine_mf.sh:25 (sleep 300)
MAX_ABANDONS = 3  # disjoncteur par produit : Celery max_retries=3, mini-swe-agent 3 erreurs (ETUDE_BLOCAGE_0410/DECISION.md)
ENV = {**os.environ, "HOME": os.environ.get("HOME") or pwd.getpwuid(os.getuid()).pw_dir, "PATH": f"{Path(PY).parent}:{os.environ.get('PATH', '/bin')}"}
SOURCER = 'sh "$0/identifiants.sh" >/dev/null 2>&1; set -a; for f in "$0/.env_claude" "$0/.env_reel"; do [ -f "$f" ] && . "$f"; done; set +a; exec "$@"'
E = {}

class Occupe(Exception):
    pass  # op.lock tenu : une opération lancée plus tôt (runner mort compris) vit encore — attendre, jamais doubler

def journal(p, msg):
    with open(ICI / "RUNNER.log", "a") as f:
        f.write(f"{time.strftime('%F %T')} [{p}] {msg}\n")

def ecrire():
    (ICI / "etat.tmp").write_text(json.dumps(E, indent=1, ensure_ascii=False))
    os.replace(ICI / "etat.tmp", ICI / "etat.json")

def texte(f):
    return Path(f).read_text() if Path(f).is_file() else ""

def lire_json(f):
    try:
        return json.loads(texte(f))
    except ValueError:
        return {}

def quotas():  # % max des fenêtres par moteur (règle du noyau) ; absent ou illisible = inconnu = aucune dépense
    try:
        d = json.loads(subprocess.run([PY, str(QUOTA), "--json"], capture_output=True, text=True, timeout=120, env=ENV).stdout)
        return {m: 100 * max(w["utilisation"] for w in v["windows"].values()) for m, v in d.items() if v.get("ok") and v.get("windows")}
    except Exception:
        return {}

def disque_ok():
    faux = os.environ.get("RUNNER_FAUX_DISQUE_GO") if os.environ.get("FACTORY_NO_REAL_ENGINE") == "1" else None
    return float(faux or shutil.disk_usage(X).free / 1e9) >= 10

def op(argv, sortie, delai, env=None, cwd=None):  # op.lock hérité par l'enfant : tenu tant qu'il vit, même runner mort
    fd = os.open(ICI / "op.lock", os.O_CREAT | os.O_RDONLY, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with open(sortie, "w") as f:
            return subprocess.run(argv, stdout=f, stderr=subprocess.STDOUT, timeout=delai, env=env or ENV, cwd=cwd, pass_fds=(fd,)).returncode
    except BlockingIOError:
        raise Occupe()
    except subprocess.TimeoutExpired:
        return 124
    finally:
        os.close(fd)

def actif():  # lien ACTIF résolu à chaque opération : mf et noyau d'une même version (couple figé de chaine_mf.sh)
    return (X / "ACTIF").resolve()

def menv(p, A):
    return {**ENV, "MF_ETAT": str(B / p / "mf_etat"), "MF_NOYAU": f"{PY} {A / 'noyau/factory_v3/run.py'}"}

def mf(p, *a):
    A = actif()
    return subprocess.run([PY, str(A / "mini_factory/mf.py"), *a], capture_output=True, text=True, timeout=300, env=menv(p, A))

def gabarit(nom, v):
    t = texte(CODE / "consignes" / f"{nom}.txt")
    for k, x in v.items():
        t = t.replace(f"@{k}@", x)
    return t

def exigences(d):
    import yaml
    return (yaml.safe_load(texte(d / "contrat.yaml")) or {}).get("a_faire_passer") or []

def construire(p, s, Q):
    A = actif()
    m = next((m for m in MOTEURS if Q.get(m, 100) < 90 and '"actif": false' not in texte(A / f"noyau/factory_v3/moteurs/{m}.json")), None)
    pl = subprocess.run([PY, str(B / "verifier_plafonds.py")], capture_output=True, text=True, timeout=120, env=ENV)
    raison = "aucun moteur à quota connu < 90 %" if not m else "disque < 10 Go" if not disque_ok() else pl.stdout.strip()[:160] if pl.returncode else ""
    if raison:
        return f"attente : {raison} ({MOTEURS})", None
    rc = op(["sh", "-c", SOURCER, str(ENVD), PY, str(A / "mini_factory/mf.py"), "suivant"], ICI / f"mf_{p}.out", 30000, {**menv(p, A), "MF_MOTEUR_FORCE": m, "MF_MOTEURS_RELIES": m})
    ligne = (texte(ICI / f"mf_{p}.out").strip().splitlines() or [""])[0]
    mo = re.match(r"(\S+) (\S+) → (\S+) \((\w+)\) reçu=(\S+)", ligne)
    if not mo:
        return f"construit rc={rc} : {ligne[:200]}", None if re.search(r"en_cours|autre mini-factory", ligne) else False
    i, j, v, etat, recu = mo.groups()  # REFUS quota inconnu et doublon VERT : tranchés par mf.py (mf_propose), plus ici
    if etat == "pret" and v != "ROUGE":  # (3) panne technique remise en file par mf (ABORT panne, sans reçu, quota inconnu) : réessai espacé
        s["reessai"] = time.time() + REESSAI_S
    return f"construit {i} {v} ({m}, {etat}) reçu={recu}", etat == "accepte"

def integrer(p, s, acc, neufs):  # (1) l'intégration suit les FAITS de mf (accepte non intégré) : rattrapée après mort, arrêt ou échec
    if not disque_ok():
        return f"attente : intégration de {neufs} différée (disque < 10 Go)", None
    rc = op([PY, str(B / "integrer.py"), p], ICI / f"integration_{p}.out", 1500, cwd=B)
    s.update(integres=acc, integ_t=0) if rc == 0 else s.update(integ_t=time.time())  # échec : reste en attente, réessai après PAUSE_S
    return f"intégration de {neufs} rc={rc}" + ("" if rc == 0 else f" : toujours en attente, réessai dans {PAUSE_S} s"), rc == 0

def accepter(p, s, D, i):
    if (D / i / "acceptation.out").exists():  # (2) lancée avant une mort du runner : attendre la fin de l'enfant (op.lock), lire SA sortie, jamais rejouer
        rc = "reprise" if op(["true"], os.devnull, 60) == 0 else "reprise ?"
    else:
        rc = op([PY, str(B / "accepter_jalon.py"), f"{p}/projet", f"{p}/copie", str(D / i)], D / i / "acceptation.out", 3000, cwd=B)
    t = texte(D / i / "acceptation.out")
    if "ACCEPTÉ" in t and [x for x in s["lot"].get("exig0", []) if x not in exigences(D / i)]:
        mf(p, "regler", i, "bloque", "--raison", "exigence RETIRÉE par la correction : revue avant construction")
        t = "exigence RETIRÉE par la correction → bloqué pour revue"
    fin = "ACCEPTÉ" in t or s["lot"].get("corrige")
    if fin:  # abandons d'affilée du produit, remis à 0 par une acceptation ; persistant (etat.json)
        s["abandons"] = 0 if "ACCEPTÉ" in t else s.get("abandons", 0) + 1
        if s["abandons"] == MAX_ABANDONS:
            journal(p, f"ALERTE disjoncteur : {MAX_ABANDONS} lots abandonnés d'affilée → produit mis de côté, aucun lot jusqu'à RUNNER/REPRISE_{p}")
    s["lot"] = None if fin else {**s["lot"], "phase": "corriger"}
    suite = "" if "ACCEPTÉ" in t else " → lot abandonné (correction unique déjà faite)" if fin else " → une correction"
    return f"{i} acceptation rc={rc} : {(t.strip().splitlines() or ['sans sortie'])[-1][:200]}{suite}", "ACCEPTÉ" in t

def lot(p, s, Q):
    L = s["lot"]
    D, i, ph = B / L["dos"], L.get("id", "-"), L["phase"]
    qui, n, debut, consignes = PHASES[ph]
    if (qui and Q.get(qui, 100) >= 90 and ph != "relire") or not disque_ok():
        return f"attente : quota {qui} inconnu ou ≥ 90 %, ou disque < 10 Go ({ph})", None
    D.mkdir(parents=True, exist_ok=True)
    if ph == "accepter":
        return accepter(p, s, D, i)
    if ph == "relire" and Q.get(qui, 100) >= 90:
        L["phase"] = "accepter"
        return f"{i} relecture absente : quota {qui} inconnu ou ≥ 90 % (non bloquante)", True
    if ph == "corriger" and not L.get("corrige"):
        L.update(corrige=True, exig0=exigences(D / i))
        (D / "passes_initiales").mkdir(exist_ok=True)
        for f in [*D.glob("P*.txt"), *D.glob("Q*.txt"), *D.glob("_DONE_*.txt"), *D.glob("REVUE.md")]:
            f.rename(D / "passes_initiales" / f.name)
        (D / i / "acceptation.out").rename(D / i / "acceptation.out.refus1")
        ecrire()
    modele = re.sub(r"NOUVEAUX_[A-Za-z0-9_]*(?=/)|(?<=écris SEULEMENT dans \.context/RADAR/)NOUVEAUX_[A-Za-z0-9_]*", L["dos"],
                    texte(B / f"CONSIGNES_GLM/{COMMUN.get(p)}.txt"))
    v = {"LOT": L["dos"], "P": p, "ID": i, "id": i.lower(), "DOC": DOC.get(p, p), "COMMUN": modele.rstrip("\n"),
         "CONSIGNE": texte(B / f"CONSIGNES_GLM/{i}.txt"), "DEJA": " ".join(sorted(x.name[len(f"NOUVEAUX_{p}_A_"):] for x in B.glob(f"NOUVEAUX_{p}_A_*"))),
         "ACCEPTATION": "\n".join(texte(D / i / "acceptation.out.refus1").splitlines()[-40:])}
    for f, g in consignes:
        (D / f).write_text(gabarit(g, v))
    env = {**ENV, "DEBUT": str(debut)} if debut else None
    rc = op(["sh", str(REST / f"{qui}_restreint.sh"), str(X.parent), os.path.relpath(D, X.parent), str(n)], D / "runner.out", 6000, env)
    if ph == "plan":
        i = (texte(D / "ORDRE.txt").split() or ["-"])[0]
        s["lot"] = None
        if not (re.fullmatch(r"[A-Za-z0-9_]{1,40}", i) and (D / f"CONSIGNE_{i}.txt").is_file() and not (B / f"NOUVEAUX_{p}_A_{i}").exists()):
            return f"plan rc={rc} → aucune étape neuve ({i})", False
        shutil.copy(D / f"CONSIGNE_{i}.txt", B / f"CONSIGNES_GLM/{i}.txt")
        s["lot"] = {"phase": "prep", "id": i, "dos": f"NOUVEAUX_{p}_A_{i}"}
        return f"plan rc={rc} → {i}", True
    if ph == "relire":
        L["phase"] = "accepter"
        return f"{i} relecture {qui} rc={rc} : {(texte(D / 'REVUE.md').splitlines() or ['sans verdict'])[0][:80]} (notée, non bloquante)", True
    ok = rc == 0 and (D / i / "contrat.yaml").is_file()
    L["phase"] = {"prep": "relire", "corriger": "accepter"}[ph] if ok else ph
    return f"{i} {ph} rc={rc}", ok

def etape(p, s):
    r = mf(p, "statut")
    if r.returncode:
        return f"mf statut rc={r.returncode} : {(r.stdout + r.stderr).strip()[-160:]}", None if "autre mini-factory" in r.stdout else False
    F = json.loads(r.stdout)
    inconnus = [i for i, t in F.items() if t["etat"] == "inconnu"]
    for i in inconnus:
        mf(p, "regler", i, "bloque", "--raison", "issue inconnue : isolée par le runner, jamais rejouée")
    if inconnus:
        return f"{inconnus} inconnu → isolé (bloque) ; la boucle continue", False
    acc = sorted(i for i, t in F.items() if t["etat"] == "accepte")
    neufs = [i for i in acc if i not in s.get("integres", [])]
    if neufs and time.time() >= s.get("integ_t", 0) + PAUSE_S:
        return integrer(p, s, acc, neufs)
    Q = quotas()  # lu juste avant la dépense : le noyau relit le même cache (< 120 s)
    if any(t["etat"] in ("pret", "en_cours") for t in F.values()) and (s["lot"] or {}).get("phase") != "accepter":  # (2) lot soldé AVANT
        if s.get("reessai", 0) > time.time():
            return f"attente : réessai espacé après panne technique (jusqu'à {time.strftime('%T', time.localtime(s['reessai']))})", None
        return construire(p, s, Q)
    if not s["lot"] and s.get("abandons", 0) >= MAX_ABANDONS:  # disjoncteur : aucun lot neuf ; les étapes prêtes (ci-dessus) se construisent
        if not (ICI / f"REPRISE_{p}").exists():
            return f"attente : produit mis de côté (disjoncteur : {s['abandons']} lots abandonnés d'affilée) — reprise : créer RUNNER/REPRISE_{p}", None
        (ICI / f"REPRISE_{p}").unlink()
        s["abandons"] = 0
        journal(p, f"REPRISE_{p} lu et supprimé : compteur d'abandons remis à 0, lots de nouveau permis")
    s["lot"] = s["lot"] or {"phase": "plan", "dos": f"NOUVEAUX_PLAN_{p}_{time.time_ns()}"}  # unique : 2 plans dans la même seconde ne partagent rien
    ecrire()
    return lot(p, s, Q)

def main():
    global E
    ICI.mkdir(parents=True, exist_ok=True)
    verrou = open(ICI / "runner.lock", "a")
    try:
        fcntl.flock(verrou, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return print("REFUS : un autre runner vit (runner.lock)") or 3
    E = lire_json(ICI / "etat.json")
    journal("-", f"DÉBUT pid={os.getpid()} produits={PRODUITS} reprise={sorted(E)}")
    tours, n = int(os.environ.get("RUNNER_TOURS", 0)), 0
    while not (ICI / "STOP").exists() and not (tours and n >= tours):
        n, agi = n + 1, False
        for p in PRODUITS:
            s = E.setdefault(p, {"erreurs": 0, "pause": 0, "lot": None})
            if s["pause"] > time.time():
                journal(p, f"PAUSE jusqu'à {time.strftime('%T', time.localtime(s['pause']))}")
                continue
            try:
                statut, ok = etape(p, s)
            except Occupe:
                statut, ok = "attente : une opération lancée plus tôt vit encore (op.lock)", None
            except Exception as e:
                statut, ok = f"plantage {type(e).__name__}: {e}"[:300], False
            s["erreurs"] = 0 if ok else s["erreurs"] + (ok is False)
            if s["erreurs"] >= MAX_ERR:
                s.update(erreurs=0, pause=time.time() + PAUSE_S, lot=None)
                statut += f" ; {MAX_ERR} erreurs consécutives → pause 1 h, lot abandonné"
            s["statut"] = {"t": time.strftime("%F %T"), "ok": ok, "texte": statut}
            ecrire()
            journal(p, f"{ {True: 'OK', None: 'ATTENTE', False: 'ÉCHEC'}[ok]} {statut}")
            agi = agi or ok is not None
        time.sleep(0 if agi else SOMME_S)
    journal("-", "FIN de boucle (STOP ou nombre de tours atteint)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
