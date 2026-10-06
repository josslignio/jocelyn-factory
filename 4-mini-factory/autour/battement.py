"""Battement croisé, côté RUNNER (chemin court, étape 6) — référence éprouvée au banc B, à appeler par runner.py (constructeur A).
battre(ici, echeance) : RUNNER/runner.battement = « <t> <échéance> » (epoch entiers ; échéance = fin maximale de l'opération en vol, 0 sinon),
tmp + rename ; à chaque tour de boucle (échéance 0) et juste AVANT chaque op() (échéance = maintenant + délai de l'op).
superviseur(ici, debut) : RUNNER/observateur.battement (écrit par l'observateur en fin de passe) plus vieux que 2700 s (3 passages de 900 s), compté
au plus depuis le démarrage du runner → ALERTE rendue + launchctl kickstart du superviseur, au plus 1 fois / 2 h (tampon écrit AVANT l'appel).
N3 (06/10) : le silence se compte à l'horloge MONOTONE du runner depuis le dernier changement vu du fichier (un recul de l'horloge murale ne le rend
jamais muet) ; seule l'estimation initiale vient du mur, bornée à [0, maintenant − démarrage]."""
import os, re, subprocess, sys, time, json, signal, hashlib, threading, stat
from pathlib import Path


def battre(ici, echeance=0):
    tmp = Path(ici) / ".runner.battement.tmp"
    tmp.write_text(f"{int(time.time())} {int(echeance)}\n")
    os.replace(tmp, Path(ici) / "runner.battement")


VU = {}


def superviseur(ici, debut):
    ici, maint, mono = Path(ici), time.time(), time.monotonic()
    b = ici / "observateur.battement"
    sig = b.stat().st_mtime_ns if b.exists() else None
    if "sig" not in VU or VU["sig"] != sig:  # changement vu (ou 1er appel) : point de départ monotone ; mur futur (recul) → 0
        VU.update(sig=sig, t=mono - (min(max(maint - (sig / 1e9 if sig else 0), 0), max(maint - debut, 0)) if "sig" not in VU else 0))
    muet = mono - VU["t"]
    if muet <= 2700:
        return ""
    msg = f"ALERTE superviseur muet depuis {int(muet) // 60} min ({b.name} non réécrit)"
    k = ici / ".kick_observateur"
    if not k.exists() or maint - k.stat().st_mtime > 7200:
        k.touch()
        rc = subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/com.factory.observateur"], capture_output=True).returncode
        msg += f" → launchctl kickstart com.factory.observateur rc={rc} (au plus 1 fois / 2 h)"
    return msg


def faucher(x):
    """NC-4 : noyau tué net (SIGKILL) → son moteur, en session propre, lui survit et brûle le quota. Reçu RUNNING dont le verrou du jalon est
    LIBRE (aucun noyau ne le tient) = noyau mort : les survivants SIGNÉS de son bac — et eux seuls (signature sandbox, jamais un processus né avant
    le run) — sont tués par la routine même du noyau (Run.tuer_survivants, mutation 82/82). Rend « run : n tué(s) » par run fauché, sinon ""."""
    import calendar, fcntl, json, sys
    out = []
    for rp in sorted(Path(x).glob("RADAR/*/copie/.factory_v3/runs/*/RECEIPT.json")):
        try:  # B8 : un reçu tronqué ou étrange est signalé et SAUTÉ, jamais la fin du faucheur pour les autres runs
            R = json.loads(rp.read_text() or "{}"); R.get  # noqa: E702
            if R.get("verdict") not in ("RUNNING", "VERT", "ROUGE", "REFUS", "ABORT") or not isinstance(R.get("milestone"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", R["milestone"]):
                raise ValueError("verdict/milestone invalide")
            w0 = calendar.timegm(time.strptime(R["started_at"], "%Y-%m-%dT%H:%M:%SZ")) if R["verdict"] == "RUNNING" else 0
        except (OSError, ValueError, AttributeError, KeyError, TypeError, OverflowError) as e:
            out.append(f"{rp.parent.name[:8]} : reçu illisible ({type(e).__name__}), sauté"); continue  # noqa: E702
        lk = rp.parents[3] / ".git/factory_v3/locks" / f"{R.get('milestone')}.lock"
        if R.get("verdict") != "RUNNING" or not lk.is_file():
            continue
        with open(lk) as f:
            try:
                fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)  # pris = aucun détenteur : le noyau de ce run est mort (relâché à la fermeture)
            except OSError:
                continue  # noyau vivant (mf tué compris) : son moteur est le sien, jamais touché
        sys.dont_write_bytecode = True; sys.path.insert(0, str((Path(x) / "ACTIF/noyau").resolve()))  # noqa: E702 — ACTIF jamais écrit
        from factory_v3.run import Run
        r = Run.__new__(Run); r.run, r.w0 = rp.parent, w0  # noqa: E702
        n = r.tuer_survivants()
        n and out.append(f"{rp.parent.name[:8]} : {n} tué(s)")
    return " ; ".join(out)


# Production réelle seulement : les diagnostics et tours à vide ne rafraîchissent jamais ce fichier.
def lire(ici, nom):
    try:
        return json.loads((Path(ici) / nom).read_text())
    except (OSError, ValueError):
        return {}


def sauver(ici, nom, valeur):
    p = Path(ici) / nom
    tmp = p.with_name("." + nom + ".tmp")
    tmp.write_text(json.dumps(valeur, ensure_ascii=False))
    os.replace(tmp, p)


def produire(ici, cause):
    precedent = lire(ici, "production.battement")
    sauver(ici, "production.battement", {**precedent, "t": time.time(), "cause": cause})


class Progression:
    def __init__(self, ici, nom, flux, espaces, mesurer=None):
        self.ici, self.nom, self.flux, self.espaces = ici, nom, flux, espaces
        self.mesurer = mesurer
        self.mesure = None
        self.dernier_gain = self.derniere_mesure = time.monotonic()
        self.intervalle = 600
        self.signature_mesuree = None
        self.travail = None
        self.retour = None
        self.meilleur = -1
        self.debut_t = time.time()
        self.debut = self.progression = self.parole = time.monotonic()
        self.signatures, self.positions, self.erreurs = {}, {}, {}
        self.empreintes = {}
        self.alerte_muette = self.debut - 600
        self.inspecter(initial=True)

    def inspecter(self, initial=False):
        maintenant = time.monotonic()
        journaux = set(self.flux())
        fichiers = set(journaux)
        for espace in self.espaces():
            fichiers.update(p for p in espace.rglob("*") if p.is_file() and ".git" not in p.parts
                            and p.parent.resolve() != Path(self.ici).resolve())
        for p in fichiers:
            try:
                if p.is_symlink(): continue
                st = p.stat()
                if not stat.S_ISREG(st.st_mode): continue
                meta = (st.st_size, st.st_mtime_ns, st.st_ctime_ns)
                ancien = self.empreintes.get(p)
                sig = ancien[1] if ancien and ancien[0] == meta else hashlib.sha256(p.read_bytes()).hexdigest()
                self.empreintes[p] = (meta, sig)
                if self.signatures.get(p) != sig and not initial:
                    self.progression = maintenant
                    produire(self.ici, "flux moteur" if p in journaux else "fichier " + p.name)
                self.signatures[p] = sig
                if p in journaux:
                    position = self.positions.get(p, 0)
                    if st.st_size < position: position = 0
                    with p.open("rb") as f:
                        f.seek(position); lignes = f.read().splitlines(keepends=True)
                    for ligne in lignes:
                        if not ligne.endswith(b"\n"): break
                        position += len(ligne)
                        if not initial:
                            self.parole = maintenant
                            texte = ligne.decode(errors="replace").strip()
                            try:
                                e = json.loads(texte)
                            except ValueError:
                                e = {}
                            if e.get("type") == "error" or e.get("error") or e.get("is_error") is True or re.match(r"(?i)^(error|erreur|exception|traceback)\b", texte):
                                cle = json.dumps(e.get("error") or e.get("message") or texte, sort_keys=True)
                                self.erreurs[cle] = self.erreurs.get(cle, 0) + 1
                    self.positions[p] = position
            except OSError:
                continue
        disparus = set(self.signatures) - fichiers
        if disparus and not initial:
            self.progression = maintenant
        for p in disparus:
            self.signatures.pop(p, None)
            self.empreintes.pop(p, None)
        if self.mesurer and not initial:
            self.sonder(maintenant)
        silence = maintenant - self.parole
        cause = ("moteur bloqué / boucle : même erreur répétée 3 fois" if max(self.erreurs.values(), default=0) >= 3 else "")
        if self.mesurer:
            cause = ""
            if self.mesure and maintenant - self.dernier_gain >= 1200 and maintenant - self.progression >= 1200:
                cause = f"ne progresse plus vers le résultat attendu ({self.mesure['k']}/{self.mesure['n']} figé depuis 20 min)"
        if not initial: sauver(self.ici, "operation.json", {"nom": self.nom, "debut_t": self.debut_t, "duree": maintenant - self.debut,
               "progression": maintenant - self.progression, "silence": silence, "cause": cause, "en_cours": True, "mesure": self.mesure,
               "mesure_intervalle": self.intervalle, "mesure_erreur": getattr(self, "mesure_erreur", "")})
        return cause, silence >= 600


    def sonder(self, maintenant):
        # Le juge tourne hors de la boucle de surveillance : un juge lent ne retarde pas TERM.
        if self.travail is not None and not self.travail.is_alive():
            resultat, duree, erreur = self.retour
            self.travail = None
            self.mesure_erreur = erreur or ("jugement > 3 min : mesures espacées à 20 min" if self.intervalle == 1200 else "")
            if duree > 180:
                self.intervalle = 1200
                self.mesure_erreur = "jugement > 3 min : mesures espacées à 20 min" + (" ; " + erreur if erreur else "")
            if resultat is not None:
                precedent = self.mesure
                k = resultat["k"]
                if k > self.meilleur:
                    self.meilleur, self.dernier_gain = k, maintenant
                self.mesure = {**resultat, "delta": k - (precedent["k"] if precedent else 0),
                               "depuis_min": round((time.time() - precedent["t"]) / 60) if precedent else 10,
                               "t": time.time(), "duree_s": duree}
                sauver(self.ici, "production.battement", {"t": time.time(), "cause": resume_mesure(self.mesure), "mesure": self.mesure})
        signature = tuple(sorted((str(p), v) for p, v in self.signatures.items()))
        if not signature or self.travail is not None or maintenant - self.derniere_mesure < self.intervalle or signature == self.signature_mesuree:
            return
        self.derniere_mesure, self.signature_mesuree = maintenant, signature
        def jouer():
            debut = time.monotonic()
            try:
                resultat, erreur = self.mesurer(), ""
            except Exception as e:
                resultat, erreur = None, f"mesure illisible : {type(e).__name__}: {e}"
            self.retour = resultat, time.monotonic() - debut, erreur
        self.travail = threading.Thread(target=jouer, daemon=True)
        self.travail.start()


def resume_mesure(m):
    return (f"{m['jalon']} : {m['k']}/{m['n']} tests attendus verts, {m['delta']:+d} depuis {m['depuis_min']} min"
            f" ; à garder verts {m['gardes']}/{m['total_gardes']} ; heure {time.strftime('%H:%M:%S', time.localtime(m['t']))}")


def arreter(p):
    # Session propre de l'opération ; TERM laisse au noyau son nettoyage des sessions moteur.
    # Les sessions descendantes sont photographiées AVANT TERM, jamais recherchées par nom.
    def photographie():
        lignes = subprocess.run(["ps", "-axo", "pid=,ppid=,pgid=,lstart="], capture_output=True, text=True, check=True).stdout
        return [(int(v[0]), int(v[1]), int(v[2]), tuple(v[3:])) for l in lignes.splitlines() if len(v := l.split()) >= 3]
    arbre = photographie()
    descendants = {p.pid}
    while True:
        suite = descendants | {pid for pid, parent, groupe, naissance in arbre if parent in descendants}
        if suite == descendants: break
        descendants = suite
    groupes = {p.pid} | {groupe for pid, parent, groupe, naissance in arbre if pid in descendants and groupe in descendants}
    identites = {(pid, groupe, naissance) for pid, parent, groupe, naissance in arbre if pid in descendants}
    for sig in (signal.SIGTERM, signal.SIGKILL):
        vivants = {(pid, groupe, naissance) for pid, parent, groupe, naissance in photographie()}
        for groupe in groupes:
            # Pas de signal à un PGID recyclé après TERM : un membre original doit encore exister.
            if not any(g == groupe for pid, g, naissance in identites & vivants): continue
            try: os.killpg(groupe, sig)
            except ProcessLookupError: pass
        if sig == signal.SIGTERM: time.sleep(5)
    p.wait()


def diagnostic(ici):
    o = lire(ici, "operation.json")
    return (o.get("cause") or "") + " ; " + f"dernière opération {o.get('nom', 'inconnue')}, durée {int(o.get('duree', 0)) // 60} min, progression il y a {int(o.get('progression', 0)) // 60} min"


def production_statut(ici):
    b, a = lire(ici, "production.battement"), lire(ici, "attente.json")
    o = lire(ici, "operation.json")
    age = max(0, int(time.time() - b.get("t", a.get("origine", o.get("debut_t", a.get("depuis", time.time())))))) // 60
    if o.get("mesure"):
        return resume_mesure(o["mesure"]) + (" ; " + o["mesure_erreur"] if o.get("mesure_erreur") else "") + (" ; " + o["cause"] if o.get("cause") else "")
    if b and age < 5:
        return f"PRODUIT il y a {age} min"
    o = lire(ici, "operation.json")
    cause = (o.get("cause") or o.get("mesure_erreur") or diagnostic(ici)) if o.get("en_cours") else (a.get("cause") or o.get("cause") or diagnostic(ici))
    return f"RIEN PRODUIT depuis {age} min — cause : {cause}"


def oisive(ici):
    if lire(ici, "operation.json").get("en_cours"):
        return False
    a = lire(ici, "attente.json")
    # Seul le runner décide du travail prêt et de la cause réelle, jamais une déduction du quota absent.
    return bool(a.get("pret") and not a.get("legitime") and time.time() - a.get("depuis", time.time()) > 300)


if __name__ == "__main__":  # observateur.sh : battement.py faucher <racine .context>
    print(faucher(sys.argv[2]) if sys.argv[1:2] == ["faucher"] else (("usine oisive sans raison : " if oisive(sys.argv[2]) else "") + production_statut(sys.argv[2]) + " ; " + diagnostic(sys.argv[2])))
