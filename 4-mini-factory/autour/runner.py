"""RUNNER chemin court (PLAN_CONSTRUCTION 05/10, étapes 1-4) — la SEULE boucle qui lance du travail. Tour équitable ; par produit et par tour UNE opération :
travail prêt → mf.py suivant ; sinon SÉLECTEUR déterministe, 0 moteur (remplace la passe de plan) : 1er jalon de projet/ROADMAP.yaml dans l'ordre_proprietaire
(enfant juste avant sa racine), dépendances vertes, juge ROUGE sur la copie à HEAD (run.py --juger) → jugé : mis en file (--essais 1) ; sans juge : UNE
préparation (contrat + juge, session distincte du constructeur) → acceptation (base rouge, gratuite) → construction. Échec FINI, état racine durable
(etat.json, budgets cumulés jamais remis à 0) : 1er échec = UN redécoupage (sortie réelle des tests jointe, ID et racine imposés) ; 2e échec ou budget épuisé
= racine ISOLÉE, comptée non livrée, jalon indépendant suivant. Aucune attente humaine hors attente_proprietaire argent / publication / identifiants.
Reprise exacte (étape 4) : état synchronisé, chaque effet a son reçu relu avant tout rejeu (mf, _DONE, acceptation.json, reçu d'intégration),
jamais sur un enfant vivant (op.lock). Quota Claude illisible > 30 min = claude_jeton_vivant.sh (1 fois / 30 min). Usage : runner.py [produit …] ; racine .context = RUNNER_X ; arrêt : RUNNER/STOP."""
import fcntl, hashlib, json, os, pwd, re, shutil, subprocess, sys, time
from pathlib import Path
import battement  # chemin_court_b (étape 6) : battement croisé runner ↔ observateur

CODE = Path(__file__).resolve().parent
X = Path(os.environ.get("RUNNER_X", CODE.parent)).resolve()
B, ICI, REST, ENVD = X / "RADAR", X / "RUNNER", X / "CLOTURE_20260927", X / "BUILD_V3_2026-09-25"
QUOTA = X.parent / "factory_v2/quota_live.py"
PY = sys.executable  # chemin absolu : sous launchd, le « python3 » du PATH (Homebrew) n'a pas yaml
PRODUITS = sys.argv[1:] or ["tiktok", "savespace", "mailbox"]
MOTEURS = os.environ.get("RUNNER_MOTEURS", "claude codex").split()  # ordre de repli de chaine_mf.sh
SEUIL = float(os.environ.get("FACTORY_SEUIL_QUOTA", 90))  # 06/10 propriétaire : « dégage la sécu », GLM brûlé jusqu'au bout
MOTEURS_PREP = os.environ.get("RUNNER_MOTEURS_PREP", "claude").split()  # sélection indépendante de la construction
COMMUN = {"tiktok": "COMMUN_TIKTOK_BRIEF", "savespace": "COMMUN_SAVESPACE", "mailbox": "COMMUN_MAILBOX"}
MAX_ERR, PAUSE_S, REESSAI_S, SOMME_S = 3, 3600, 300, int(os.environ.get("RUNNER_SOMME_S", 300))  # REESSAI_S : ex-chaine_mf.sh:25 (sleep 300)
MAX_APPELS, MAX_MIN = 6, 360  # budget PAR RACINE : prép. + constr. + redécoupage + constr. + reconstruction = 5 appels au plus sans panne
JETONS = ("input", "output", "cache_read", "cache_write", "reasoning", "input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
ATTENTES = ("argent", "publication", "identifiants")  # les SEULES attentes humaines admises (DECISION 05/10 §1)
ENV = {**os.environ, "HOME": os.environ.get("HOME") or pwd.getpwuid(os.getuid()).pw_dir, "PATH": f"{Path(PY).parent}:{os.environ.get('PATH', '/bin')}"}
SOURCER = 'sh "$0/identifiants.sh" >/dev/null 2>&1; set -a; for f in "$0/.env_claude" "$0/.env_reel"; do [ -f "$f" ] && . "$f"; done; set +a; exec "$@"'
E = {}

class Occupe(Exception):
    pass  # op.lock tenu : une opération lancée plus tôt (runner mort compris) vit encore — attendre, jamais doubler

def journal(p, msg):  # une ALERTE va aussi dans ALERTES.log (celui de l'observateur)
    for n in ("RUNNER.log", "ALERTES.log")[:1 + msg.startswith("ALERTE")]:
        with open(ICI / n, "a") as f:
            f.write(f"{time.strftime('%F %T')} [{p}] {msg}\n")

def ecrire():  # synchronisé (étape 4) : fichier puis répertoire, comme mf.ecrire — jamais d'état partiel ; N2 : DEUX copies complètes (prec puis json)
    for n in ("etat.prec", "etat.json"):
        with open(ICI / "etat.tmp", "w") as f:
            f.write(json.dumps(E, indent=1, ensure_ascii=False)); f.flush(); os.fsync(f.fileno())
        os.replace(ICI / "etat.tmp", ICI / n)
    d = os.open(ICI, os.O_RDONLY); os.fsync(d); os.close(d)

def lire_etat():  # N2 : état vide ou tronqué = JAMAIS {} silencieux : la copie etat.prec, sinon None (refus explicite) ; {} seulement au 1er démarrage
    for n in ("etat.json", "etat.prec"):
        try:
            if isinstance(d := json.loads(texte(ICI / n)), dict):
                return d, n
        except ValueError:
            pass
    return (None if any((ICI / n).exists() for n in ("etat.json", "etat.prec")) else {}), None

def texte(f):
    return Path(f).read_text() if Path(f).is_file() else ""

def charge(t):
    try:
        d = json.loads(t)
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}

def quotas(jeton=True):  # % max des fenêtres par moteur (règle du noyau) ; absent ou illisible = inconnu = aucune dépense
    try:
        d = json.loads(subprocess.run([PY, str(QUOTA), "--json"], capture_output=True, text=True, timeout=120, env=ENV).stdout)
        Q = {m: 100 * max(w["utilisation"] for w in v["windows"].values()) for m, v in d.items() if v.get("ok") and v.get("windows")}
    except Exception:
        Q = {}
    t, G = time.time(), E.setdefault("-", {})  # état commun aux produits, persistant (etat.json)
    G["claude_lu"] = t if "claude" in Q else G.get("claude_lu", t)  # 05/10 : jeton OAuth expiré = 10 h 45 d'attente muette
    if jeton and t - G["claude_lu"] > 1800 and t - G.get("jeton_t", 0) > 1800:  # 1 renouvellement / 30 min, borne persistante
        G["jeton_t"], n = t, (t - G["claude_lu"]) // 60
        ecrire()  # borne écrite AVANT l'appel : un runner tué pendant l'appel ne le refait pas (Codex 05/10)
        f = os.environ["RUNNER_FAUX_JETON"] if os.environ.get("FACTORY_NO_REAL_ENGINE") == "1" else f"{ENV['HOME']}/.local/bin/claude_jeton_vivant.sh"
        j = subprocess.Popen(["sh", f], stdout=open(ICI / "jeton.out", "w"), stderr=subprocess.STDOUT, env=ENV, start_new_session=True)  # NC-2 : il décide
        try:  # sortie dans un fichier, sans op.lock ni tube ; au délai, TOUT le groupe (claude compris) est tué (Codex 05/10)
            rc = j.wait(300)
        except subprocess.TimeoutExpired:
            rc = os.killpg(j.pid, 9) or 124
        Q = quotas(False)
        journal("-", f"{'' if 'claude' in Q else 'ALERTE '}quota Claude illisible depuis {n:.0f} min → {f} rc={rc} ; "
                     + ("relu" if "claude" in Q else "toujours illisible, nouvel essai dans 30 min"))
    return Q

def qt(Q, ms):  # « illisible » (se répare : jeton) ≠ « ≥ 90 % » (attend le renouvellement)
    return ", ".join(f"{m} " + (f"à {Q[m]:.0f} %" if m in Q else "illisible") for m in ms)

def pause_erreurs(s):
    return s.get("pause", 0) > time.time() and (s.get("pause_cause") == "pause erreurs" or "erreurs consécutives" in s.get("statut", {}).get("texte", ""))

def disque_ok():
    faux = os.environ.get("RUNNER_FAUX_DISQUE_GO") if os.environ.get("FACTORY_NO_REAL_ENGINE") == "1" else None
    return float(faux or shutil.disk_usage(X).free / 1e9) >= 10

def op(argv, sortie, delai, env=None, cwd=None, avant=None, surveillance=None):  # op.lock hérité par l'enfant : tenu tant qu'il vit, même runner mort
    fd = os.open(ICI / "op.lock", os.O_CREAT | os.O_RDONLY, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        battement.battre(ICI, time.time() + delai)  # échéance de l'op en vol, lue par l'observateur (chemin_court_b)
        avant and avant()  # budget de la racine écrit APRÈS la prise du verrou (une attente n'est jamais comptée) et AVANT l'appel
        if surveillance is None:
            battement.sauver(ICI, "operation.json", {"nom": str(sortie), "debut_t": time.time(), "duree": 0, "progression": 0, "en_cours": True})
        with open(sortie, "w") as f:
            p = subprocess.Popen(argv, stdout=f, stderr=subprocess.STDOUT, env=env or ENV, cwd=cwd, pass_fds=(fd,), start_new_session=True)
            debut = time.monotonic()
            while True:
                cause, muet = surveillance.inspecter() if surveillance else ("", False)
                if muet and time.monotonic() - surveillance.alerte_muette >= 600:
                    journal("-", "ALERTE moteur muet : " + surveillance.nom)
                    surveillance.alerte_muette = time.monotonic()
                if surveillance is None:
                    battement.sauver(ICI, "operation.json", {"nom": str(sortie), "debut_t": time.time() - (time.monotonic() - debut), "duree": time.monotonic() - debut,
                        "progression": time.monotonic() - debut, "en_cours": True})
                if time.monotonic() - debut >= delai:
                    cause = "plafond de durée dépassé : " + str(sortie)
                if cause:
                    o = battement.lire(ICI, "operation.json"); o["cause"] = cause
                    battement.sauver(ICI, "operation.json", o)
                    journal("-", "ALERTE " + cause)
                    battement.arreter(p)
                    return 124
                if p.poll() is not None:
                    return p.returncode
                time.sleep(1)
    except BlockingIOError:
        raise Occupe()
    except subprocess.TimeoutExpired:
        return 124
    finally:
        if "p" in locals():
            o = battement.lire(ICI, "operation.json"); o["en_cours"] = False
            battement.sauver(ICI, "operation.json", o)
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

def rac(m):  # racine d'un jalon : donnée de la roadmap (écrite par le runner via l'acceptation), jamais tirée de l'ID
    return m.get("racine") or m["id"]

def afp(m):
    return (m.get("juge") or {}).get("a_faire_passer") or []

def exig(m):
    return afp(m) + ((m.get("juge") or {}).get("a_garder_verts") or [])

def roadmap(p):  # la roadmap EXÉCUTABLE du noyau (pas une 3e) ; périmètre et ordre = ordre_proprietaire, sinon l'ordre du fichier
    import yaml
    r = yaml.safe_load(texte(B / p / "projet/ROADMAP.yaml")) or {}
    M = {m["id"]: m for m in r.get("milestones") or [] if isinstance(m, dict) and "id" in m}
    o = r.get("ordre_proprietaire", list(M)) or []  # clé absente = tout le fichier ; présente et vide = AUCUN jalon (roadmaps_proposees)
    return M, sorted((m for m in M.values() if rac(m) in o), key=lambda m: (o.index(rac(m)), m["id"] == rac(m)))

def jetons(s, R, u):  # NC-14 : jetons de l'appel (reçu du noyau « engine.usage » ou « result » du flux de préparation), cumulés par racine ; absent = 0
    racine(s, R)["jetons"] = racine(s, R).get("jetons", 0) + (n := sum(v for k, v in (u or {}).items() if k in JETONS and isinstance(v, (int, float))))
    return n

def racine(s, R):  # état racine DURABLE : budgets cumulés et échecs, jamais remis à 0 (ni renommage, ni redémarrage)
    return s.setdefault("racines", {}).setdefault(R, {"appels": 0, "minutes": 0, "echecs": 0})

def epuise(r):  # 2e échec ou budget cumulé épuisé : vérifié avant CHAQUE dépense (sélection, préparation, construction)
    return r.get("isolee") or r["echecs"] >= 2 or r["appels"] >= MAX_APPELS or r["minutes"] >= MAX_MIN

def isoler(p, R, r):
    r["isolee"] = True
    journal(p, f"ALERTE racine {R} ISOLÉE ({r['echecs']} échecs, {r['appels']} appels, {r['minutes']:.0f} min) : comptée non livrée, jalon indépendant suivant")
    return f"racine {R} isolée"

def compter(s, R, D=None):  # un appel déjà fait (passe _DONE avant une mort du runner) n'est pas recompté
    racine(s, R)["appels"] += not (D and (D / "_DONE_1.txt").exists())
    ecrire()

def echec(s, R, j, cause="", recu=None):  # 1er échec = UN redécoupage ; tout échec suivant = isolement (décidé à la sélection)
    r = racine(s, R)
    r["echecs"] += 1
    r.setdefault("causes", []).append({"j": j, "cause": cause[-3000:], "recu": recu})
    return f" → échec {r['echecs']} de la racine {R}"

def causes(p, M, r):  # rendues là où elles servent (redécoupage) : la sortie réelle d'un ROUGE est rejugée par le noyau à ce moment, une fois
    def transmettre(c):
        cause = c["cause"]
        if "ne progresse plus vers le résultat attendu" in cause:
            return "échec technique : construction inactive ; reprendre le contrat et les livrables"
        return cause or sortie(p, M.get(c["j"]), c["recu"])
    return "\n".join(f"--- échec de {c['j']} ---\n{transmettre(c)}" for c in r.get("causes", [])[-2:])

def juger(p, sha, m, figes=None):  # juge de m sur la copie à sha (+ livrables figés d'un run) PAR LE NOYAU, comme accepter_jalon : (vert ?, sortie pytest)
    a = ICI / f"arbre_{p}"
    if figes or texte(a / ".sha") != sha:
        shutil.rmtree(a, ignore_errors=True)
        a.mkdir(parents=True)
        g = subprocess.Popen(["git", "-C", str(B / p / "copie"), "archive", sha], stdout=subprocess.PIPE)
        if subprocess.run(["tar", "-x", "-C", str(a)], stdin=g.stdout).returncode or g.wait():
            return None, f"git archive {sha} illisible"
        figes and figes.is_dir() and shutil.copytree(figes, a, dirs_exist_ok=True)
        (a / ".sha").write_text("" if figes else sha)
    j = subprocess.run([PY, str(actif() / "noyau/factory_v3/run.py"), "--juger", str(a), "--livrables", ":".join(m.get("livrables") or []), "--budget-s", "900",
                        "--", "-q", "-p", "no:cacheprovider", "-p", "no:rerunfailures", *sorted({x.split("::")[0] for x in exig(m)})],
                       capture_output=True, text=True, errors="replace", timeout=1000, env=ENV)
    v = charge(j.stdout.strip().rpartition("\n")[2]); cas = v.get("cas")  # B5 : rc pytest 0/1 cohérent avec les cas, sinon (interrompu…) ILLISIBLE
    vu = lambda x: {v for k, v in cas.items() if k == x or k.startswith((x + "::", x + "["))}  # noqa: E731 — sauté ≠ vert
    if not (isinstance(cas, dict) and v.get("rc") in (0, 1) and (v["rc"] == 1) == ("fail" in cas.values()) and afp(m) and all(vu(x) for x in exig(m))):
        return None, j.stderr
    battement.produire(ICI, "verdict du juge")
    return all(vu(x) == {"pass"} for x in exig(m)), j.stderr  # vert = exigences ET non-régression (a_garder_verts) vertes

def mesurer_construction(p, m, espaces, A):
    """Photo RO des seuls livrables ; le juge vient du HEAD intégré, jamais du moteur.
    Arbre, HOME du juge, sortie et verdict restent tous sous RUNNER (hors bac moteur).
    """
    import tempfile, stat
    candidats = espaces()
    if len(candidats) != 1:
        raise ValueError("espace du run absent ou ambigu")
    espace = candidats[0].resolve()
    base = (B / p / "copie/.factory_v3/spaces").resolve()
    if not espace.is_relative_to(base) or espace == base:
        raise ValueError("espace hors spaces du noyau")
    with tempfile.TemporaryDirectory(prefix="mesure_", dir=ICI) as tmp:
        arbre = Path(tmp) / "arbre"
        arbre.mkdir()
        archive = Path(tmp) / "base.tar"
        subprocess.run(["git", "-C", str(B / p / "copie"), "archive", "-o", str(archive), "HEAD"], check=True, capture_output=True)
        subprocess.run(["tar", "-xf", str(archive), "-C", str(arbre)], check=True, capture_output=True)
        for nom in m.get("livrables") or []:
            chemin = Path(nom)
            if chemin.is_absolute() or ".." in chemin.parts or not chemin.parts:
                raise ValueError("livrable hors arbre")
            src, dst = espace / chemin, arbre / chemin
            # Aucun lien (y compris parent), FIFO ou périphérique : ne pas lire hors du workspace.
            if any(q.is_symlink() for q in [src, *src.parents][:len(chemin.parts)]):
                raise ValueError("livrable lien symbolique")
            if any(q.is_symlink() for q in [dst, *dst.parents][:len(chemin.parts)]):
                raise ValueError("destination lien symbolique")
            if dst.exists():
                if dst.is_dir(): raise ValueError("livrable attendu fichier")
                dst.unlink()
            if src.exists():
                fd = os.open(src, os.O_RDONLY | getattr(os, "O_NOFOLLOW_ANY", os.O_NOFOLLOW) | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as f:
                    if not stat.S_ISREG(os.fstat(f.fileno()).st_mode): raise ValueError("livrable non régulier")
                    contenu = f.read()
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(contenu)
        j = subprocess.run([PY, str(A / "noyau/factory_v3/run.py"), "--juger", str(arbre), "--livrables", ":".join(m.get("livrables") or []), "--budget-s", "900",
                            "--", "-q", "-p", "no:cacheprovider", "-p", "no:rerunfailures", *sorted({x.split("::")[0] for x in exig(m)})],
                           capture_output=True, text=True, errors="replace", timeout=1000, env=ENV)
        v = charge(j.stdout.strip().rpartition("\n")[2]); cas = v.get("cas")
        if not (j.returncode == 0 and isinstance(cas, dict) and v.get("rc") in (0, 1)
                and (v["rc"] == 1) == ("fail" in cas.values()) and afp(m)):
            raise ValueError("verdict de mesure illisible")
        def vert(x):
            vus = {st for nom, st in cas.items() if nom == x or nom.startswith((x + "::", x + "["))}
            if not vus: raise ValueError("test attendu absent du verdict : " + x)
            return vus == {"pass"}
        gardes = (m.get("juge") or {}).get("a_garder_verts") or []
        return {"jalon": m["id"], "k": sum(vert(x) for x in afp(m)), "n": len(afp(m)),
                "gardes": sum(vert(x) for x in gardes), "total_gardes": len(gardes)}


def choisir(p, s, F):  # SÉLECTEUR, 0 moteur : verdicts recalculés par le noyau, en cache pour UN SHA de la copie (rien n'est cru d'un autre état)
    M, L = roadmap(p)
    sha = subprocess.run(["git", "-C", str(B / p / "copie"), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    V = s["verdicts"] = s.get("verdicts") if (s.get("verdicts") or {}).get("@") == sha else {"@": sha}
    cle = lambda m: m["id"] + "@" + hashlib.sha256(json.dumps([m.get("juge"), m.get("livrables")], sort_keys=True).encode()).hexdigest()[:12]  # noqa: E731 — N4
    def vert(m):  # verdict en cache pour (SHA de la copie, CONTRAT du jalon) : un contrat changé est rejugé
        if cle(m) not in V and "juge" in m:
            v, sortie = juger(p, sha, m)
            if v is None:  # erreur technique : ni rouge ni vert, jamais en cache, jamais construit
                return journal(p, f"ALERTE juge de {m['id']} illisible à {sha[:8]} : {sortie.strip()[-200:]}")
            V[cle(m)] = v
        return V.get(cle(m), False)
    iso = [R for R, r in (s.get("racines") or {}).items() if r.get("isolee")]
    att = [m["id"] for m in L if str(m.get("attente_proprietaire") or "-").split()[0].strip(":") in ATTENTES]
    fait = set(s.get("integres", []) + s.get("ecartes", []))  # en file = prêt, en cours, ou VERT pas encore intégré ni écarté : jamais remis en file
    file = {t["jalon"] for i, t in F.items() if t["etat"] in ("pret", "en_cours") or t["etat"] == "accepte" and i not in fait}
    for m in L:
        deps = (m.get("depend_de") or []) + ((M.get(rac(m)) or {}).get("depend_de") or [] if rac(m) != m["id"] else [])
        if rac(m) not in iso and m["id"] not in att + list(file) and all(d in M and vert(M[d]) for d in deps) and vert(m) is False:
            return m, ""
    return None, (f"aucun jalon constructible : livrés {sum(V.get(cle(m)) is True for m in L)}/{len(L)} (juge vert à {sha[:8]}) ; "
                  f"isolés {iso} ; attente propriétaire {att}")

def construire(p, s, Q, F):
    A = actif()
    m = next((m for m in MOTEURS if Q.get(m, 100) < SEUIL and '"actif": false' not in texte(A / f"noyau/factory_v3/moteurs/{m}.json")), None)
    pl = subprocess.run([PY, str(B / "verifier_plafonds.py")], capture_output=True, text=True, timeout=120, env=ENV)
    cli = m and charge(texte(A / f"noyau/factory_v3/moteurs/{m}.json")).get("cli")  # NC-10 : CLI épinglé effacé (mise à jour) : attente nommée, 0 échec
    raison = (f"aucun moteur < 90 % ({qt(Q, MOTEURS)})" if not m else f"ALERTE CLI épinglé de {m} absent : {cli}" if cli and not shutil.which(cli)
              else "disque < 10 Go" if not disque_ok() else pl.stdout.strip()[:160] if pl.returncode else "")
    s["attente_legitime"] = "disque <" in raison or (not m and all(Q.get(x, 0) >= 90 for x in MOTEURS))
    if raison:
        raison.startswith("ALERTE") and journal(p, raison)
        return f"attente : {raison} ({MOTEURS})", None
    M, L = roadmap(p)
    t = next((t for t in F.values() if t["etat"] == "pret"), None)  # celle que mf lancera (1re prête) ; un orphelin seul n'est pas un appel
    R = t and rac(M.get(t["jalon"]) or {"id": t["jalon"]})
    hors = t and t["jalon"] not in {x["id"] for x in L}  # B3 strict : tâche hors ordre_proprietaire (ou jalon RETIRÉ de la roadmap) : jamais construite
    if t and (hors or epuise(racine(s, R))):  # Codex 05/10 : une tâche déjà en file ne dépense jamais au-delà de sa racine
        i = next(i for i, x in F.items() if x is t)
        mf(p, "regler", i, "bloque", "--raison", "hors ordre_proprietaire" if hors else f"racine {R} isolée ou budget épuisé : jamais lancée")
        return (f"{i} hors ordre_proprietaire" if hors else isoler(p, R, racine(s, R))) + f" ; {i} bloqué sans lancement", True
    avant = (lambda: compter(s, R)) if t else None
    t0 = time.time()
    base = B / p / "copie/.factory_v3"
    anciens = set((base / "runs").glob("*"))
    flux = lambda: [r / "engine.log" for r in (base / "runs").glob("*") if r not in anciens]
    espaces = lambda: [Path(d["espace"]) for r in (base / "runs").glob("*") if r not in anciens
                       for d in [charge(texte(r / "RECEIPT.json"))] if d.get("espace")]
    mesurer = (lambda: mesurer_construction(p, M[t["jalon"]], espaces, A)) if t else None
    surveillance = battement.Progression(ICI, f"construction {p}", flux, espaces, mesurer=mesurer)
    rc = op(["sh", "-c", SOURCER, str(ENVD), PY, str(A / "mini_factory/mf.py"), "suivant"], ICI / f"mf_{p}.out", 5400,
            {**menv(p, A), "MF_MOTEUR_FORCE": m, "MF_MOTEURS_RELIES": m}, avant=avant, surveillance=surveillance)
    if t:
        racine(s, R)["minutes"] += (time.time() - t0) / 60
    if rc == 124 and t:
        i = next(i for i, v in F.items() if v is t)
        mf(p, "regler", i, "bloque", "--raison", "échec technique : surveillance moteur")
        s.setdefault("vus", []).append(i)
        return "échec technique construction" + echec(s, R, t["jalon"], battement.diagnostic(ICI)), False
    ligne = (texte(ICI / f"mf_{p}.out").strip().splitlines() or [""])[0]
    mo = re.match(r"(\S+) (\S+) → (\S+) \((\w+)\) reçu=(\S+)", ligne)
    if not mo:
        return f"construit rc={rc} : {ligne[:200]}", None if re.search(r"en_cours|autre mini-factory", ligne) else False
    battement.produire(ICI, "verdict construction")
    i, j, v, etat, recu = mo.groups()  # REFUS quota inconnu et doublon VERT : tranchés par mf.py (mf_propose), plus ici
    n = t and jetons(s, R, (charge(texte(recu) if recu != "None" else "").get("engine") or {}).get("usage"))
    if etat == "pret" and v != "ROUGE":  # (3) panne technique remise en file par mf (ABORT panne, sans reçu, quota inconnu) : réessai espacé
        s["reessai"] = time.time() + REESSAI_S
    return f"construit {i} {v} ({m}, {etat}) reçu={recu} jetons={n}", etat == "accepte"

def sortie(p, m, recu):  # sortie RÉELLE des tests du candidat : copie à la base du run + ses livrables figés, rejugée par le noyau
    R = charge(texte(recu or ""))
    if R.get("verdict") != "ROUGE" or not m:
        return f"{R.get('verdict')} : {R.get('motif')}"
    return f"ROUGE : {R.get('motif')}\n" + juger(p, R.get("base_sha", "?"), m, Path(recu).parent / "figes")[1][-2500:]

def integrer(p, s, F, neufs, pe):  # (1) suit les FAITS de mf (accepte non intégré) ; intégré = listé dans le REÇU de CET appel, jamais « rc 0 donc tout »
    if not disque_ok():
        s["attente_legitime"] = True
        s["pret"] = True
        return f"attente : intégration de {neufs} différée (disque < 10 Go)", None
    t0 = int(time.time())
    rc = op([PY, str(B / "integrer.py"), p, "--seulement", ",".join(sorted(pe))], ICI / f"integration_{p}.out", 1500, cwd=B)  # B3 : liste POSITIVE
    R = charge(texte(ICI / f"integration_{p}.json"))  # {t, sha_final, integres, ecartes} écrit atomiquement par integrer.py
    R = R if isinstance(R.get("t"), (int, float)) and R["t"] >= t0 else {}  # absent, illisible ou d'avant cet appel = rien d'intégré
    vu = lambda k: [i for i in neufs if {i, F[i]["jalon"]} & set(R.get(k) or [])]  # noqa: E731
    if vu("integres"): battement.produire(ICI, "intégration")
    s["integres"], s["ecartes"] = s.get("integres", []) + vu("integres"), s.get("ecartes", []) + vu("ecartes")  # écarté : rejugé à HEAD par le sélecteur
    s["integ_t"] = 0 if len(vu("integres") + vu("ecartes")) == len(neufs) else time.time()  # reste en attente : réessai après PAUSE_S
    s["integ_echecs"] = 0 if R and rc == 0 else s.get("integ_echecs", 0) + 1  # NC-19 : échec PERMANENT = ALERTE nommée, pas « 0 livraison » générique
    s["integ_echecs"] >= 3 and journal(p, f"ALERTE intégration de {neufs} en échec {s['integ_echecs']} fois de suite : voir RUNNER/integration_{p}.out")
    return f"intégration de {neufs} rc={rc} : intégrés {vu('integres')}, écartés {vu('ecartes')}" + ("" if R else " : aucun reçu de cet appel"), bool(R) and rc == 0

def accepter(p, s, D, i):  # résultat STRUCTURÉ (acceptation.json, écrit en dernier, atomique) ; une sortie partielle n'est jamais une acceptation ;
    L, res = s["lot"], D / i / "acceptation.json"  # absent = jamais lancée ou interrompue → rejouée (idempotente), jamais sur un enfant vivant, 3 fois au plus
    if not L.get("acceptations"):  # Codex 05/10 : avant NOTRE 1er lancement, un reçu présent vient de la préparation (qui écrit dans le lot) : effacé
        res.unlink(missing_ok=True)
    if not res.exists() and L.get("acceptations", 0) < 3:
        rc = op([PY, str(B / "accepter_jalon.py"), f"{p}/projet", f"{p}/copie", str(D / i), L["racine"]], D / i / "acceptation.out", 1427, cwd=B,
                avant=lambda: (L.update(acceptations=L.get("acceptations", 0) + 1), ecrire()))
        if rc == 124:
            s["lot"] = None
            return "échec technique acceptation" + echec(s, L["racine"], i, "plafond 1427 s (max sain × 1,3)"), False
        if not res.exists():
            return f"{i} acceptation rc={rc} sans résultat (interrompue, {L['acceptations']}/3) : rejouée au tour suivant", None  # attente : ni erreur, ni échec ; borné à 3
    A = charge(texte(res)) or {"motif": f"aucun résultat lisible après {L.get('acceptations', 0)} acceptations interrompues"}
    if res.exists(): battement.produire(ICI, "verdict acceptation")
    s["lot"], ok = None, A.get("verdict") == "ACCEPTÉ" and A.get("id") == i
    t = "\n".join(texte(D / i / "acceptation.out").strip().splitlines()[-40:])
    return f"{i} acceptation {A.get('verdict', 'REFUS')} : {str(A.get('motif'))[:200]}" + ("" if ok else echec(s, L["racine"], i, t or str(A.get("motif")))), ok

def lot(p, s, Q):  # UNE passe de préparation (contrat + juge) ; jamais de solution, de relecture ni de correction
    L = s["lot"]
    D, i, R = B / L["dos"], L["id"], L["racine"]
    prep = next((m for m in MOTEURS_PREP if m in ("claude", "codex", "glm") and Q.get(m, 100) < SEUIL), None)
    raison = f"quota {qt(Q, MOTEURS_PREP)}" if not prep else "disque < 10 Go" if not disque_ok() else ""
    s["attente_legitime"] = "disque <" in raison or (not prep and all(Q.get(x, 0) >= 90 for x in MOTEURS_PREP))
    if raison:  # attente d'infrastructure : rien n'est compté (ni erreur, ni échec, ni appel)
        return f"attente : {raison} (préparation)", None
    D.mkdir(parents=True, exist_ok=True)
    if L["phase"] == "accepter":
        return accepter(p, s, D, i)
    if epuise(racine(s, R)):  # Codex 05/10 : un lot persistant ne dépense jamais au-delà de sa racine
        s["lot"] = None
        return isoler(p, R, racine(s, R)), True
    M = roadmap(p)[0]
    m = M[R] if L.get("redecoupe") else M[i]
    modele = re.sub(r"NOUVEAUX_[A-Za-z0-9_]*(?=/)|(?<=écris SEULEMENT dans \.context/RADAR/)NOUVEAUX_[A-Za-z0-9_]*", L["dos"],
                    texte(B / f"CONSIGNES_GLM/{COMMUN.get(p)}.txt"))
    v = {"LOT": L["dos"], "P": p, "ID": i, "id": i.lower(), "RACINE": R, "COMMUN": modele.rstrip("\n"), "CONSIGNE": str(m.get("but", "")),
         "EXIGENCES": json.dumps(afp(m), ensure_ascii=False), "CAUSES": causes(p, M, racine(s, R))}
    (D / "COMMUN.txt").write_text(gabarit("prep_commun", v))
    (D / "P1.txt").write_text((gabarit("redecouper", v) if L.get("redecoupe") else "") + gabarit("prep", v))
    t0 = time.time()
    rc = op(["sh", str(REST / f"{prep}_restreint.sh"), str(X.parent), os.path.relpath(D, X.parent), "1"], D / "runner.out", 2700, avant=lambda: compter(s, R, D),
            surveillance=battement.Progression(ICI, f"préparation {p}/{i}", lambda: list(D.glob("*_p1.jsonl")), lambda: [D]))
    racine(s, R)["minutes"] += (time.time() - t0) / 60
    if rc == 124:
        s["lot"] = None
        return "échec technique préparation" + echec(s, R, i, battement.diagnostic(ICI)), False
    f, n = D / f"{prep}_p1.jsonl", 0  # NC-14 : chaque flux compté UNE fois (repère = son mtime, écrit avec le lot dans etat.json)
    if f.is_file() and L.get("jetons_t") != f.stat().st_mtime:
        usages = [e.get("usage") if e.get("type") in ("result", "turn.completed") else e.get("part", {}).get("tokens") for e in map(charge, texte(f).splitlines())]
        L["jetons_t"], n = f.stat().st_mtime, sum(jetons(s, R, {**u, **{f"cache_{k}": v for k, v in u.get("cache", {}).items()}}) for u in usages if isinstance(u, dict))
    ok = rc == 0 and (D / i / "contrat.yaml").is_file()
    L["phase"] = "accepter" if ok else "prep"
    if not ok and (D / "_DONE_1.txt").exists():  # Codex 05/10 : passe finie SANS contrat = échec de la racine (une panne technique, elle, est rejouée)
        s["lot"] = None
        return f"{i} préparation finie sans contrat" + echec(s, R, i, texte(D / "runner.out")[-2000:] or "passe finie sans contrat.yaml"), False
    return f"{i} préparation{' (redécoupage)' if L.get('redecoupe') else ''} racine {R} rc={rc} jetons={n}", ok

def migrer(p, s):  # UNE fois par produit (marque « migre ») : reçu d'amorçage des jalons dont le juge est VERT, rejugé par le noyau, au HEAD du
    reel = subprocess.run(["git", "-C", str(B / p / "copie"), "remote", "get-url", "origin"], capture_output=True, text=True).stdout.strip()  # dépôt RÉEL
    sha = reel and subprocess.run(["git", "-C", reel, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()  # (statut.py ne compte que les reçus)
    verts = [m["id"] for m in roadmap(p)[0].values() if sha and "juge" in m and juger(p, sha, m)[0] is True]  # juge absent au HEAD réel = jamais vert
    if sha:
        R = {"t": int(time.time()), "sha_final": sha, "integres": verts, "ecartes": [], "raisons": {"*": f"migration 05/10 : juge VERT au HEAD réel {sha[:8]} ({reel})"}}
        with open(ICI / f"integration_{p}.jsonl", "a") as f:  # B6 : une ligne tronquée laissée avant reste isolée (saut de ligne d'abord)
            f.write(("\n" if texte(ICI / f"integration_{p}.jsonl")[-1:] not in ("", "\n") else "") + json.dumps(R, ensure_ascii=False) + "\n")
    s["migre"] = sha or "sans dépôt réel (copie sans origin)"
    journal(p, f"migration : {len(verts)} jalon(s) au juge VERT au HEAD réel {str(sha)[:8]} → reçu d'amorçage" if sha else "migration : copie sans dépôt réel, rien")

def etape(p, s):
    "migre" in s or migrer(p, s)
    r = mf(p, "statut")
    if r.returncode:
        return f"mf statut rc={r.returncode} : {(r.stdout + r.stderr).strip()[-160:]}", None if "autre mini-factory" in r.stdout else False
    F = json.loads(r.stdout)
    inconnus = [i for i, t in F.items() if t["etat"] == "inconnu"]
    for i in inconnus:  # jamais rejouée : bloquée (puis comptée ci-dessous comme tout échec)
        mf(p, "regler", i, "bloque", "--raison", "issue inconnue : isolée par le runner, jamais rejouée")
        F[i]["etat"] = "bloque"
    V, M = s.setdefault("vus", []), None
    for i, t in F.items():  # RÉCONCILIATION sur les FAITS de mf : chaque issue finale comptée UNE fois, avec l'état qui la note (mort du runner comprise)
        if t["etat"] == "bloque" and i not in V:
            M = M or roadmap(p)[0]
            journal(p, f"{i} bloque{echec(s, rac(M.get(t['jalon']) or {'id': t['jalon']}), t['jalon'], recu=(t.get('dernier') or {}).get('recu'))}")
            V.append(i)
    if inconnus:
        return f"{inconnus} inconnu → isolé (bloque), échec compté ; la boucle continue", False
    fait, pe = set(s.get("integres", []) + s.get("ecartes", [])), {m["id"] for m in roadmap(p)[1]}  # B3 strict : liste POSITIVE = ordre_proprietaire
    neufs = sorted(i for i, t in F.items() if t["etat"] == "accepte" and i not in fait and t["jalon"] in pe)  # un ancien VERT hors périmètre : jamais
    if neufs and time.time() >= s.get("integ_t", 0) + PAUSE_S:
        return integrer(p, s, F, neufs, pe)
    s["pret"] = bool(s.get("lot") or any(t["etat"] == "pret" for t in F.values()))
    Q = quotas()  # lu juste avant la dépense : le noyau relit le même cache (< 120 s)
    if any(t["etat"] in ("pret", "en_cours") for t in F.values()):  # l'acceptation ne met plus en file : aucun lot à solder avant
        if s.get("reessai", 0) > time.time():
            return f"attente : réessai espacé après panne technique (jusqu'à {time.strftime('%T', time.localtime(s['reessai']))})", None
        return construire(p, s, Q, F)
    if not s["lot"]:
        m, bilan = choisir(p, s, F)
        if not m:  # tout est vert, isolé ou en attente propriétaire : repos, aucune dépense
            s["pause"] = time.time() + 6 * PAUSE_S
            return f"{bilan} : produit au repos 6 h", None
        R, r = rac(m), racine(s, rac(m))
        if epuise(r):
            return isoler(p, R, r), True
        if r["echecs"] and not r.get("enfant"):  # UN redécoupage par racine : ID et racine imposés par le runner, pas par le moteur
            r["enfant"] = f"{R}_R"
            s["lot"] = {"phase": "prep", "id": r["enfant"], "racine": R, "dos": f"NOUVEAUX_{p}_A_{r['enfant']}_{r['appels']}", "redecoupe": True}
        elif "juge" in m:
            k = next(x for x in [m["id"]] + [f"{m['id']}.{n}" for n in range(2, 99)] if x not in F)
            a = mf(p, "ajouter", k, "--projet", str(B / p / "projet"), "--jalon", m["id"], "--moteur", "glm", "--essais", "1")
            return f"{m['id']} juge ROUGE à HEAD → mis en file {k} (racine {R}) rc={a.returncode} {a.stdout.strip()[-120:]}", a.returncode == 0
        else:
            s["lot"] = {"phase": "prep", "id": m["id"], "racine": R, "dos": f"NOUVEAUX_{p}_A_{m['id']}_{r['appels']}"}
    s["pret"] = True
    return lot(p, s, Q)  # le lot est écrit avec le budget, juste avant l'appel (compter) ; une attente l'écrit en fin de tour

def main():
    global E
    ICI.mkdir(parents=True, exist_ok=True)
    verrou = open(ICI / "runner.lock", "a")
    try:
        fcntl.flock(verrou, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return print("REFUS : un autre runner vit (runner.lock)") or 3
    E, n = lire_etat()
    if E is None:
        return journal("-", "ALERTE état illisible (etat.json ET etat.prec vides ou tronqués) : REFUS, rien lancé — restaurer l'état") or 3
    n == "etat.prec" and journal("-", "ALERTE etat.json illisible : reprise sur etat.prec (copie complète précédente), budgets gardés")
    DEBUT = float(os.environ.get("RUNNER_FAUX_DEBUT") or 0) if os.environ.get("FACTORY_NO_REAL_ENGINE") == "1" else 0
    DEBUT = DEBUT or time.time()
    journal("-", f"DÉBUT pid={os.getpid()} produits={PRODUITS} reprise={sorted(E)}")
    tours, n = int(os.environ.get("RUNNER_TOURS", 0)), 0
    while not (ICI / "STOP").exists() and not (tours and n >= tours):
        n, agi, G, t = n + 1, False, E.setdefault("-", {}), time.time()
        G.setdefault("mur", DEBUT)  # recul possible avant le premier contrôle
        if t < G.get("mur", DEBUT) - 60:  # N3 : horloge RECULÉE : toutes les échéances stockées décalées d'autant, dit en ALERTE
            journal("-", f"ALERTE horloge reculée de {(G['mur'] - t) / 60:.0f} min : pauses, réessais, intégration, jeton décalés d'autant")
            for s in (v for v in E.values() if isinstance(v, dict)):
                s.update({k: s[k] - (G["mur"] - t) for k in ("pause", "reessai", "integ_t", "jeton_t", "claude_lu") if s.get(k)})
            G["mur"] = t; ecrire()   # décalage DURABLE avant tout tour
        G["mur"] = t
        if (ICI / "relance.tour").exists():
            (ICI / "relance.tour").unlink()
            for k, v in E.items():
                if k != "-" and v.get("pret") and not v.get("attente_legitime") and not pause_erreurs(v): v.update(pause=0, reessai=0)
            ecrire()
        for p in PRODUITS:
            s = E.setdefault(p, {"erreurs": 0, "pause": 0, "lot": None})
            if s["pause"] > time.time():
                if pause_erreurs(s): s["attente_legitime"] = True
                journal(p, f"PAUSE cause : {s.get('statut', {}).get('texte', 'panne technique')} ; fin prévue : {time.strftime('%T', time.localtime(s['pause']))}")
                continue
            occupe = False
            s["attente_legitime"] = False
            s.pop("pause_cause", None)
            try:
                statut, ok = etape(p, s)
            except Occupe:  # l'échéance de l'op orpheline est gardée (jamais remise à 0)
                statut, ok, occupe = "attente : une opération lancée plus tôt vit encore (op.lock)", None, True
            except Exception as e:
                statut, ok = f"plantage {type(e).__name__}: {e}"[:300], False
            s["erreurs"] = 0 if ok else s["erreurs"] + (ok is False)
            if s["erreurs"] >= MAX_ERR:
                s.update(erreurs=0, pause=time.time() + PAUSE_S, lot=None, attente_legitime=True, pause_cause="pause erreurs")
                statut += f" ; {MAX_ERR} erreurs consécutives → pause erreurs 1 h, lot abandonné ; fin prévue : {time.strftime('%F %T', time.localtime(s['pause']))}"
            if ok is None:
                statut += f" ; fin prévue : inconnue, réexamen {time.strftime('%F %T', time.localtime(time.time() + SOMME_S))}"
            s["statut"] = {"t": time.strftime("%F %T"), "ok": ok, "texte": statut}
            ecrire()
            journal(p, f"{ {True: 'OK', None: 'ATTENTE', False: 'ÉCHEC'}[ok]} {statut}")
            battement.battre(ICI, (texte(ICI / "runner.battement").split() or [0, 0])[1] if occupe else 0)
            agi = agi or ok is not None
        attentes = [v for k, v in E.items() if k != "-" and v.get("pret")]
        cause = " ; ".join(v.get("statut", {}).get("texte", "cause inconnue") for v in attentes)
        legitime = bool(attentes) and all(v.get("attente_legitime", False) for v in attentes)
        if not cause:
            cause = " ; ".join(v.get("statut", {}).get("texte", "") for k, v in E.items() if k != "-")
        ancien = battement.lire(ICI, "attente.json")
        battement.sauver(ICI, "attente.json", {"pret": bool(attentes) and not agi, "legitime": legitime, "cause": cause, "fin": max((v.get("pause", 0) for v in attentes if pause_erreurs(v)), default=None), "origine": ancien.get("origine", DEBUT),
            "depuis": ancien.get("depuis", time.time()) if ancien.get("pret") and not agi else time.time()})
        if (ICI / "relance.tour").exists():
            (ICI / "relance.tour").unlink()
            for v in attentes:
                if not v.get("attente_legitime") and not pause_erreurs(v): v.update(pause=0, reessai=0)
            ecrire(); agi = True
        a = battement.superviseur(ICI, DEBUT)  # observateur muet > 45 min : ALERTE + kickstart (au plus 1 / 2 h), décidés par battement.py
        a and journal("-", a)
        if not agi:
            for _ in range(SOMME_S):
                if (ICI / "STOP").exists() or (ICI / "relance.tour").exists(): break
                time.sleep(1)
    journal("-", "FIN de boucle (STOP ou nombre de tours atteint)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
