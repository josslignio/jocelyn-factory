#!/usr/bin/env python3
"""factory_v3/run.py — noyau V0 : un jalon, un espace jetable, un moteur borné, le juge gelé, un reçu.

Contrat : factory_v3/INTERFACE.md (SPEC_V0_GELEE v3). Codes retour : 0 VERT · 1 ROUGE · 2 ABORT · 3 REFUS avant dépense.
Le noyau ne croit jamais le code retour ni le texte d'un moteur : seuls le diff, FACTORY_V3_BLOCKED.md et le juge gelé
décident. Tout sous-processus moteur/tests passe par sandbox-exec -f bornage.sb ; aucun shell nulle part.
"""
from __future__ import annotations

import argparse, collections, contextlib, ctypes, fcntl, hashlib, json, os, re, shutil, signal  # noqa: E401
import subprocess, sys, tarfile, threading, time, uuid  # noqa: E401
from pathlib import Path
from string import Template

import yaml

V3 = Path(__file__).resolve().parent
ROOT = V3.parent
sys.path.insert(0, str(ROOT))
from factory_v3 import support  # noqa: E402

SCHEMA, PROFIL, BLOCKED = "factory-v3-receipt/3", V3 / "bornage.sb", "FACTORY_V3_BLOCKED.md"
FAKES = (ROOT / "tests" / "v3" / "fakes",)
BUDGET_MAX, LOG_MAX = 5400, 20 << 20
PHASES = ("espace", "tests_base", "moteur", "contrat", "juge", "tests_apres", "publication")
GELES_NOMS = {"conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "sitecustomize.py"}
TEMP_MOTEUR = re.compile(r"(^|/)(__pycache__|\.pytest_cache)(/|$)|\.pyc$|(^|/)\.DS_Store$")
ENV_INTERDITS = {"SSH_AUTH_SOCK", "GH_TOKEN", "GITHUB_TOKEN", "CLAUDECODE"}
GIT = shutil.which("git") or "/usr/bin/git"
SAFE_GIT = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "commit.gpgsign=false", "-c", "core.fileMode=true",
            "-c", "user.name=factory_v3", "-c", "user.email=factory_v3@localhost", "-c", "init.defaultBranch=base"]
GIT_ENV = {"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "HOME": "/var/empty",
           "LANG": "C", "GIT_TERMINAL_PROMPT": "0"}
PY = os.path.realpath(sys.executable)
TEST_PATH = f"{os.path.dirname(PY)}:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

class Fin(Exception):
    """Fin d'un run : verdict, code retour, motif (+ motif_echec, clé des répétitions « 2 fois = humain »)."""
    def __init__(self, verdict, rc, motif, motif_echec=None):
        super().__init__(motif)
        self.verdict, self.rc, self.motif, self.motif_echec = verdict, rc, motif, motif_echec

def refus(motif, echec=None): return Fin("REFUS", 3, motif, echec)  # noqa: E704
def abort(motif, echec=None): return Fin("ABORT", 2, motif, echec)  # noqa: E704
def exiger(ok, motif):
    """Refus uniforme : lève REFUS (rc 3) si la condition n'est pas tenue."""
    if not ok:
        raise refus(motif)
def sha256(b: bytes) -> str: return hashlib.sha256(b).hexdigest()  # noqa: E704
def real(p) -> str: return os.path.realpath(str(p))  # noqa: E704
mono = time.monotonic  # C:B1 : plus de décalage injectable (il s'annulait dans chaque différence)

def hors_du_depot(p: str) -> bool:
    """Prédicat unique (T4/K2) : chemin canonique (normpath(p) == p), relatif, sans « .. », hors .git/.factory_v3."""
    return not p or p in (".", ".git") or os.path.normpath(p) != p or p.startswith(("/", ".git/", ".factory_v3")) or ".." in p.split("/")  # noqa: E501

def git(repo, *a, check=True, timeout=300):
    """git sans hooks, sans config globale/système : le noyau n'exécute jamais rien venu de l'espace."""
    r = subprocess.run([GIT, "-C", str(repo), *SAFE_GIT, *a], capture_output=True, timeout=timeout, env=GIT_ENV)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(a[:3])} : rc={r.returncode}")  # B:1 : stderr jamais recopié
    return r.stdout.decode(errors="replace") if check else r

def git_common(repo) -> Path:
    return Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())

def fichiers_base(repo, sha) -> list[str]:
    """Chemins suivis par git au commit base — source unique de l'inventaire de config moteur (doublable en test pur)."""
    return [p for p in git(repo, "ls-tree", "-r", "--name-only", "-z", sha).split("\0") if p]

def config_moteur(p: str) -> bool:
    """Config de moteur portée par le dépôt produit (constat 27/09 18:11) : dossier .opencode/ à toute profondeur, ou opencode.json(c)."""
    return p.startswith(".opencode/") or "/.opencode/" in p or p.rsplit("/", 1)[-1] in ("opencode.json", "opencode.jsonc")

def runtime_base(repo) -> Path:
    """État unique pour tous les worktrees qui partagent le même dépôt Git."""
    common = git_common(repo)
    return common.parent / ".factory_v3" if common.name == ".git" else common / "factory_v3-runtime"

def boot_id():
    return subprocess.run(["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"], capture_output=True, text=True).stdout.strip() or None

def lire_jsonl(p: Path) -> list[dict]:
    """Lecteur tolérant : une ligne illisible (dernière ligne tronquée) est ignorée, jamais fatale."""
    out = []
    with contextlib.suppress(OSError):
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            with contextlib.suppress(ValueError):
                out.append(json.loads(line))
    return [d for d in out if isinstance(d, dict)]

def ajouter_ligne(p: Path, d: dict) -> None:
    """Une ligne JSON, fsync ; jamais collée à une ligne tronquée laissée par un crash."""
    with open(p, "a+b") as f:
        n = f.seek(0, 2)
        f.seek(max(n - 1, 0))
        sep = b"\n" if n and f.read(1) != b"\n" else b""
        f.write(sep + json.dumps(d, ensure_ascii=False).encode() + b"\n")
        f.flush()
        os.fsync(f.fileno())

def reconcilier(index: Path, runs_dirs) -> None:
    """Index = vue dérivée du reçu (fait foi) : tout reçu final absent y retourne par run_uuid ; un reçu qui change REMPLACE sa ligne ; historique orphelin conservé ; flock sérialisé, même entre jalons."""
    with support.file_lock(index.parent / "index.lock"):
        lignes = lire_jsonl(index); connus = {x.get("run_uuid") for x in lignes}; dirs = {Path(x["recu"]).parent.parent for x in lignes if isinstance(x.get("recu"), str)} | {*runs_dirs}
        derives = {}
        for rp in sorted(p for d in dirs if d.is_dir() for p in d.glob("*/RECEIPT.json")):
            if (R := support.read_json(rp) or {}).get("schema") == SCHEMA and R.get("verdict") not in (None, "RUNNING") and (u := R.get("run_uuid")) and rp.parent.name == u:  # C10 : autre schéma jamais ingéré
                derives[u] = ({k: R.get(k) for k in ("run_uuid", "base_sha", "hash_contrat", "verdict", "rc", "machine_id", "essai", "finished_at", "started_at", "produit", "motif", "motif_echec", "t_wall_total")}
                              | {"jalon": R.get("milestone"), "cost_usd": (R.get("engine") or {}).get("cost_usd"), "test_only": "test_only" in ((R.get("noyau") or {}).get("flags") or []), "recu": str(rp)})
        final = [derives.get(x.get("run_uuid"), x) for x in lignes] + [d for u, d in derives.items() if u not in connus]
        vus = set(); final = [d for d in final if (k := json.dumps(d, sort_keys=True, ensure_ascii=False)) not in vus and not vus.add(k)]  # doublons exacts retirés, contradictions conservées (la preuve n'est pas arbitrée)
        if final != lignes:
            support.ecrire_octets(index, b"".join((json.dumps(d, ensure_ascii=False) + "\n").encode() for d in final))

def blob_sha(p: Path) -> str:
    d = os.readlink(p).encode() if p.is_symlink() else p.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(d) + d).hexdigest()

def correspond(jid: str, c: str) -> bool:
    """id pytest du juge ↔ nodeid brut du cas (B:6) : lui-même, ou un enfant (::, [paramètres], /sous-chemin)."""
    return c == jid or c.startswith((jid + "::", jid + "[", jid + "/"))

def copier_log(src, path: Path) -> None:
    """Journal plafonné : tête ≤ LOG_MAX marqueur compris, le marqueur écrase la fin (INTERFACE §3) ; texte jamais compté."""
    n = 0
    with open(path, "wb") as f:
        for chunk in iter(lambda: src.read1(65536), b""):
            if n < LOG_MAX:
                f.write(chunk[:LOG_MAX - n])
            n += len(chunk)
        if n > LOG_MAX:
            f.seek(max(0, LOG_MAX - len(m := f"\n[factory_v3] LOG TRONQUÉ : {n} octets produits\n".encode()))); f.write(m)

def lire_usage(spec, log: Path, complet: bool):
    """Usage lu dans le flux du moteur selon le bloc « usage » de moteurs/<id>.json ; null si inconnu, jamais deviné."""
    u = dict.fromkeys(("input", "cache_read", "cache_write", "output", "reasoning", "turns", "cost_usd"))
    evs = [e for e in lire_jsonl(log) if e.get("type") == spec["type"]] if spec else []
    for e in evs if spec and spec.get("cumul") else evs[-1:]:
        for k, chemin in spec["champs"].items():
            v = e
            for c in chemin.split("."):
                v = v.get(c) if isinstance(v, dict) else None
            if isinstance(v, (int, float)):
                u[k] = (u[k] or 0) + v if spec.get("cumul") else v
    if evs and spec.get("cumul"):
        u["turns"] = len(evs)
    cout = u.pop("cost_usd")
    return u | {"source": spec and spec["type"], "complet": bool(evs) and complet}, cout

def lire_quota(pool: str) -> dict | None:
    """Garde-quota lecture seule (lecteurs quota_live) : fenêtre = max du moteur ; absent/injoignable = None, jamais 0."""
    fake = os.environ.get("FACTORY_V3_FAKE_QUOTA")
    exiger(fake in (None, "") or os.environ.get("FACTORY_NO_REAL_ENGINE") == "1",
           f"FACTORY_V3_FAKE_QUOTA : réglage factice réservé à FACTORY_NO_REAL_ENGINE=1")  # F2
    try:
        w = ((json.loads(fake) or {}).get(pool) or {}) if fake is not None else {} if os.environ.get("FACTORY_NO_REAL_ENGINE") == "1" \
            else {k: v * 100 for k, v in support.quota(pool).items()}
    except Exception:
        return None
    w = {k: float(v) for k, v in w.items() if isinstance(v, (int, float))}
    fen = max(w, key=w.get) if w else None
    return fen and {"pool": pool, "fenetre": fen, "pct": w[fen], "fenetres": w}

def charger(project, milestone, repo_arg):
    proj = Path(project).resolve()
    rm = yaml.safe_load((proj / "ROADMAP.yaml").read_text(encoding="utf-8")) or {}
    m = next((x for x in rm.get("milestones") or [] if isinstance(x, dict) and x.get("id") == milestone), None)
    repo = repo_arg or rm.get("product_repo")
    return proj, m, (Path(repo).expanduser().resolve() if repo else None)

def task_path(proj: Path, repo, m: dict):
    """task_file absolu, ou relatif au répertoire du projet, puis au dépôt produit."""
    tf = m.get("task_file")
    if not tf or not isinstance(tf, str):
        return None
    p = Path(tf).expanduser()
    return next((c for c in ([p] if p.is_absolute() else [proj / p] + ([repo / p] if repo else [])) if c.is_file()), None)

def contrat_octets(m: dict, task: bytes) -> bytes:
    """Octets hachés du contrat (B:7) : sha256(run/CONTRAT.json) == hash_contrat, recalculable par un tiers."""
    p = {k: m.get(k) for k in ("id", "but", "juge", "test_cmd", "timeout_s", "budget_moteur_s", "reseau_tests", "spec_ref")}
    p["livrables"], p["geles"] = sorted(m.get("livrables") or []), sorted(m.get("geles") or [])
    if m.get("juge_isole") is True: p["juge_isole"] = True  # noqa: E701 — absent du contrat = empreinte inchangée
    blob = json.dumps(p, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    return blob + b"\n" + task

class Run:
    def __init__(self, a, proj, m, repo):
        self.a, self.proj, self.m, self.repo, self.jalon = a, proj, m or {}, repo, a.milestone
        self.uuid, self.m0, self.w0, self.seq, self.boot = str(uuid.uuid4()), mono(), time.time(), 0, boot_id()
        self.base = runtime_base(repo)
        self.index = self.base / "index.jsonl"
        self.run = self.base / "runs" / self.uuid
        self.run.mkdir(parents=True)
        self.lock = self.lockfd = self.espace = self.espace_moteur = self.enfant = self.moteur = self.rouges_precedents = None
        self.R = {"schema": SCHEMA, "run_uuid": self.uuid, "machine_id": None, "boot_id": self.boot, "pid": os.getpid(),
                  "produit": self.m.get("produit"), "milestone": self.jalon, "repo": str(repo), "base_sha": None, "essai": None,
                  "hash_contrat": None, "hash_consigne": None, "engine": None, "phases": dict.fromkeys(PHASES),
                  "sandbox": {"profil_sha256": sha256(PROFIL.read_bytes()) if PROFIL.is_file() else None, "charge": False,
                              "mecanisme": "sandbox-exec -f bornage.sb"},
                  "tests_base": None, "contrat": None, "tests_apres": None, "commit": None, "quota": None,
                  "disk_free_gb": None, "etape": "admission", "verdict": "RUNNING", "rc": None, "motif": None,
                  "motif_echec": None, "started_at": support.now(), "finished_at": None, "t_monotone_total": None,
                  "t_wall_total": None, "noyau": {"sha_run_py": sha256(Path(__file__).read_bytes()),
                                                  **{f"sha_{f.name.replace('.', '_')}": sha256(f.read_bytes()) for f in (V3 / "support.py", V3 / "juge_isole.py")},
                                                  "flags":
                                                  ["test_only"] * a.test_only + ["keep"] * a.keep}}
    # ---- preuves (fn 10) : reçu atomique à chaque étape, EVENTS intention avant effet / résultat après --------------------
    def ecrire(self):
        support.atomic_write(self.run / "RECEIPT.json", self.R)
    def ev(self, event, step, data=None):
        self.seq += 1
        ajouter_ligne(self.run / "EVENTS.jsonl", {"schema_version": 1, "seq": self.seq, "t_monotone": round(mono() - self.m0, 3),
                                                  "t_wall": support.now(), "event": event, "step": step, "data": data or {}})
    @contextlib.contextmanager
    def etape(self, nom):
        self.R["etape"] = nom
        self.ecrire()
        self.ev(f"{nom.upper()}_START", nom)
        t0 = mono()
        yield
        self.R["phases"][nom] = d = round(mono() - t0, 3)
        self.ev(f"{nom.upper()}_END", nom, {"s": d})
    # ---- verrou (fn 11) : un run par (dépôt, jalon) ; flock TENU par le fd jusqu'à la fin — détenteur tué = libéré ----------
    def verrou(self):
        common = git_common(self.repo)
        excl = common / "info" / "exclude"
        txt = excl.read_text() if excl.exists() else ""
        if ".factory_v3/" not in txt.split():
            excl.parent.mkdir(parents=True, exist_ok=True)
            excl.write_text(txt + ("\n" if txt and not txt.endswith("\n") else "") + ".factory_v3/\n")
        p = common / "factory_v3" / "locks" / f"{self.jalon}.lock"
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p, os.O_CREAT | os.O_RDONLY, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # M12 : détenteur tué → l'OS libère le verrou
        except OSError:
            os.close(fd)
            raise refus(f"verrou tenu : {p} (flock) — détenteur vivant ; identité : reçu RUNNING sous runs/")  # noqa: E501
        self.lock, self.lockfd = p, fd  # le fd reste ouvert : c'est lui qui détient le verrou tout le run
    # ---- lire_jalon (fn 1) -------------------------------------------------------------------------------------------------
    def lire_jalon(self):
        m = self.m
        exiger(m, f"jalon introuvable dans {self.proj}/ROADMAP.yaml : {self.jalon}")
        for k in ("produit", "spec_ref", "but"):
            exiger(m.get(k), f"jalon non déclarable : champ {k} absent")
        exiger(m.get("type", "construction") == "construction", f"jalon de type {m.get('type')} : jamais donné à un moteur")
        tp = task_path(self.proj, self.repo, m)
        exiger(tp and tp.read_bytes().strip(), "consigne vide : task_file absent ou vide")
        self.task, liv, tc, j = tp.read_bytes(), m.get("livrables"), m.get("test_cmd"), m.get("juge")
        exiger(isinstance(liv, list) and 1 <= len(liv) <= 3 and all(
               isinstance(x, str) and not hors_du_depot(x) and x != BLOCKED for x in liv), "livrables : liste blanche de 1 à 3 chemins relatifs au dépôt exigée")
        ign = [x for x in liv if git(self.repo, "check-ignore", "-q", "--", x, check=False).returncode == 0]  # C5 : helper borné
        exiger(not ign, f"livrable ignoré par .gitignore : {ign}")
        exiger(isinstance(tc, list) and tc and all(isinstance(x, str) for x in tc), "test_cmd absent ou pas en argv (liste de chaînes)")
        exiger(not any("factory_v2" in x or "product_e2e" in x for x in tc) and any("pytest" in x for x in tc)
               and any("{junit}" in x for x in tc), "test_cmd : V0 = pytest avec --junitxml {junit}, sans repli factory_v2/product_e2e")
        exiger(m.get("juge_isole") is True, "juge_isole: true exigé (F1) : in-process, le verdict sortirait d'un JUnit sous le HOME inscriptible du bac")
        exiger(tc[1:3] == ["-m", "pytest"], "juge isolé : test_cmd = [python, '-m', 'pytest', ...] exigé")
        exiger(isinstance(j, dict) and j.get("a_faire_passer") and all(
               isinstance(x, str) for x in (j.get("a_faire_passer") or []) + (j.get("a_garder_verts") or [])),
               "juge : a_faire_passer (ids pytest) obligatoire")
        fiches = {x.partition("::")[0] for x in (j.get("a_faire_passer") or []) + (j.get("a_garder_verts") or [])}
        exiger(all(not hors_du_depot(f) for f in fiches), f"juge : partie fichier d'un id hors du dépôt : {sorted(fiches)}")
        interdits = sorted(x for x in liv if x.rsplit("/", 1)[-1] in GELES_NOMS or x in fiches)
        exiger(not interdits, f"livrable interdit (config de test gelée ou fichier du juge) : {interdits}")
        exiger(not m.get("reseau_tests"), "reseau_tests: true interdit en V0 (palier D avec preuve)")
        exiger("donnees" not in j, "juge.donnees : manifeste de données retiré (v9), toute présence de la clé est refusée")
        self.R["hash_contrat"] = hc = sha256(c := contrat_octets(m, self.task))
        (self.run / "CONTRAT.json").write_bytes(c)
        exiger(isinstance(m.get("valide"), dict) and m["valide"].get("hash_contrat") == hc,
               f"contrat non validé : valide.hash_contrat absent ou ≠ {hc}")
        self.liv, self.test_cmd, self.afp, self.agv = liv, tc, j["a_faire_passer"], j.get("a_garder_verts") or []
        self.timeout = min(int(m.get("timeout_s") or 600), BUDGET_MAX)
        self.budget = min(int(self.a.budget_s or m.get("budget_moteur_s") or 2700), BUDGET_MAX)
    # ---- garde_hermetique (fn 6) + profil moteur (§7a) ---------------------------------------------------------------------
    def resoudre_moteur(self):
        fake, sans_reel = os.environ.get("FACTORY_V3_FAKE_ENGINE"), os.environ.get("FACTORY_NO_REAL_ENGINE") == "1"
        if fake is not None:
            path = next((d / f"{fake}.py" for d in FAKES if (d / f"{fake}.py").is_file()), None)
            exiger(sans_reel and re.fullmatch(r"[a-z0-9_]+", fake) and path,
                   f"faux moteur refusé : {fake!r} (exige FACTORY_NO_REAL_ENGINE=1 et le catalogue tests/v3/fakes)")
            self.R["noyau"]["flags"].append(f"fake:{fake}")
            return {"id": f"fake:{fake}", "argv": [PY, str(path), "{prompt_path}"], "format_sortie": "aucun", "defauts": {},
                    "plafonds": {"mecanisme": "aucun", "drapeaux": []}, "cli_version": "fake", "config_sha256": sha256(path.read_bytes()),
                    "arret": {"signal_1": "SIGTERM", "grace_s": 5, "signal_2": "SIGKILL"}}
        exiger(not sans_reel, "FACTORY_NO_REAL_ENGINE=1 : aucun CLI réel (FACTORY_V3_FAKE_ENGINE absent)")
        cfg_p = V3 / "moteurs" / f"{self.a.engine}.json"
        cfg, lock = json.loads(cfg_p.read_text()), json.loads((V3 / "ENGINES.lock").read_text())
        exiger(cfg.get("actif", True) is not False, f"moteur {cfg['id']} inactif (actif: false)")
        exe = shutil.which(cfg["cli"])
        ver = (re.search(r"\d+\.\d+\.\d+", subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30).stdout)
               or [None])[0] if exe else None
        exiger(ver is not None and ver == lock.get(cfg["id"]), f"moteur {cfg['id']} : version installée {ver} ≠ ENGINES.lock {lock.get(cfg['id'])}")
        manque = [k for k in (cfg.get("auth") or {}).get("env", []) if not os.environ.get(k)]
        exiger(not manque, f"moteur {cfg['id']} : authentification absente ({manque})")
        mag = (cfg.get("auth") or {}).get("magasin_cli")  # Codex OAuth : magasin réel du CLI, relatif au home
        exiger(not mag or (not hors_du_depot(mag) and (Path.home() / mag).is_file()),
               f"moteur {cfg['id']} : magasin d'authentification absent (~/{mag})")
        return {**cfg, "argv": [real(exe), *cfg["argv"][1:]], "cli_version": ver, "config_sha256": sha256(cfg_p.read_bytes())}
    # ---- doublon, répétitions, quota, disque, config moteur (REFUS avant dépense) ------------------------------------------
    def admission(self):
        reconcilier(self.index, [self.run.parent]); base, hc = self.R["base_sha"], self.R["hash_contrat"]  # B6 : reçu fait foi — l'index (vue) recalculé avant tout verdict
        prev = [x for x in lire_jsonl(self.index) if x.get("jalon") == self.jalon and x.get("hash_contrat") == hc
                and not x.get("test_only")]
        for x in prev[:]:  # C11 : reçu EXISTANT d'un autre schéma = ignoré (et dit) ; recu illisible = compté
            if isinstance((p := x.get("recu")), str) and (r := support.read_json(Path(p))) is not None and r.get("schema") != SCHEMA:
                prev.remove(x); print(f"[v3] admission : ligne {x.get('run_uuid')} ignorée — reçu d'un autre schéma ({r.get('schema')})")
        prev = [x for x in prev if not (x.get("verdict") == "ABORT" and x.get("motif_echec") == "panne_moteur")]  # D§2b
        self.R["essai"] = 1 + sum(x.get("base_sha") == base and x.get("verdict") != "REFUS" for x in prev)
        # levier 5 : noms des tests rouges du DERNIER essai ROUGE (même base) — reçu structuré, jamais motif_echec (tronqué à 400)
        der = max((x for x in prev if x.get("verdict") == "ROUGE" and x.get("base_sha") == base),  # noqa: E501
                  key=lambda x: (x.get("finished_at") or "", x.get("essai") or 0), default=None)
        R = (support.read_json(Path(der["recu"])) or {}) if der and isinstance(der.get("recu"), str) else {}  # noqa: E501
        self.rouges_precedents = sorted({i.split("::", 1)[1].split("[", 1)[0] for i in (R.get("tests_apres") or {}).get("rouges") or [] if "::" in i})[:10]  # noqa: E501
        exiger(not any(x.get("verdict") == "VERT" and x.get("base_sha") == base for x in prev),
               "doublon : reçu VERT existant pour (jalon, SHA base, hash_contrat) — sortie : autre base, ou nouveau contrat réellement changé et validé ; réévaluer un artefact déjà produit : non implémenté")
        cnt = collections.Counter(x.get("motif_echec") for x in prev if x.get("motif_echec"))
        if cnt and max(cnt.values()) >= 2:
            raise refus(f"ATTENTE_HUMAINE : 2 essais au même échec ({cnt.most_common(1)[0][0]}) — arrêt ; aucune procédure de reprise n'est implémentée")
        pool = self.a.engine
        q = lire_quota(pool) if self.moteur else None  # C:A7 : --test-only ne dépense rien, ne lit aucun quota
        self.R["quota"] = q and {"pool": q["pool"], "fenetre": q["fenetre"], "pct_avant": q["pct"], "pct_apres": None, "fenetres": q["fenetres"],
                                 "limites": "exhaustivité des fenêtres non établie ; pour glm, clé mesurée (ZAI_API_KEY) = clé remise au moteur"}  # D3
        if q is None and self.moteur and os.environ.get("FACTORY_NO_REAL_ENGINE") != "1":  # C07 : inconnu ≠ 0 %
            raise refus(f"quota {pool} inconnu (illisible ou absent) — aucune dépense sans mesure")
        if q and q["pct"] >= float(os.environ.get("FACTORY_SEUIL_QUOTA", 90)): raise refus(f"quota {pool} {q['fenetre']} à {q['pct']:.0f} %")  # noqa: E701
        raw = os.environ.get("FACTORY_V3_FAKE_DISK_FREE_GB")
        exiger(raw in (None, "") or os.environ.get("FACTORY_NO_REAL_ENGINE") == "1",
               f"FACTORY_V3_FAKE_DISK_FREE_GB={raw} : réglage factice réservé à FACTORY_NO_REAL_ENGINE=1")  # F2
        self.R["disk_free_gb"] = free = round(float(raw) if raw not in (None, "") else support.free_gb(self.repo), 2)
        exiger(free >= 10.0, f"disque : {free} Gio libres, seuil 10 Gio")  # C:B12 : seuil constant
        cfg = [p for p in fichiers_base(self.repo, base) if config_moteur(p)]  # constat 27/09 : refuser AVANT la dépense, pas 243 s après
        exiger(not cfg, f"base contient une configuration de moteur de projet : {cfg[:3]} — nettoyer le dépôt avant tout lancement")
    # ---- ouvrir_espace (fn 3) : git archive -o, tar -x, git init jetable, 1 commit « base » ----------------------
    def extraire(self, dest: Path, paths=()):
        """v9 : seule la commande git a un délai (300 s) ; l'extraction du fichier n'a ni délai ni plafond de volume."""
        tar = self.run / "base.tar"
        try:
            git(self.repo, "archive", "--format=tar", "-o", str(tar), self.R["base_sha"], "--", *paths)
            with tarfile.open(tar) as tf:
                tf.extractall(dest, filter="data")
        finally:
            tar.unlink(missing_ok=True)
    def effacer(self, paths):
        for p in paths:
            q = self.espace / p
            shutil.rmtree(q) if q.is_dir() and not q.is_symlink() else q.unlink(missing_ok=True)
    def remettre_a_base(self):
        git(self.espace, "checkout", "-q", "-f", self.base_commit, "--", ".")
        self.effacer(filter(None, git(self.espace, "ls-files", "--others", "-z").split("\0")))
    def copier_livrables(self, src, dst):
        """Livrables figés au moment du contrat (run/figes), jamais ré-échantillonnés depuis l'espace vivant."""
        for f in self.liv:
            a, b = src / f, dst / f
            if a.exists() or a.is_symlink():
                b.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(a, b, follow_symlinks=False)
            else:
                b.unlink(missing_ok=True)
    def ouvrir_espace(self):
        with self.etape("espace"):
            self.espace = self.base / "spaces" / f"{self.jalon}-{self.R['base_sha'][:8]}-e{self.R['essai']}-{self.R['machine_id']}-{self.uuid[:8]}"
            self.espace.mkdir(parents=True)
            self.R["espace"] = str(self.espace)
            self.extraire(self.espace)
            # SPEC §7g : les fichiers des tests « à faire passer » ne sont pas montrés au builder ; le juge les remet.
            self.caches = [c for c in sorted({j.partition("::")[0] for j in self.afp} - set(self.liv)) if (self.espace / c).is_file()]
            self.effacer(self.caches)
            for cmd in (("init", "-q", "--template="), ("add", "-A", "-f"), ("commit", "-q", "--no-verify", "-m", "base")):
                git(self.espace, *cmd)
            self.base_commit = git(self.espace, "rev-parse", "HEAD").strip()
    # ---- sous-processus bornés (fn 5) : sandbox-exec, session propre, arrêt gradué, survivants tués -------------------------
    def bornage(self, argv, net, tmp, home):
        params = {"ESPACE": real(self.espace or self.base / "spaces" / "_sonde"), "RUN": real(self.run), "TMP": real(tmp),
                  "HOME": real(home), "NET": net, "CONTROL": real(ROOT), "USERHOME": real(Path.home())}
        if net == "1" and (mag := ((self.moteur or {}).get("auth") or {}).get("magasin_cli")):  # phase moteur seule
            params["MAGASIN"] = real(Path.home() / mag)
        return ["/usr/bin/sandbox-exec", "-f", str(PROFIL), *[f"-D{k}={v}" for k, v in params.items()], *argv]
    def sonder_profil(self):
        if not PROFIL.is_file() or not Path("/usr/bin/sandbox-exec").exists():
            raise abort("profil non chargé : bornage.sb ou sandbox-exec absent — jamais de repli")
        (d := self.run / "sonde").mkdir()
        r = subprocess.run(self.bornage(["/usr/bin/true"], "0", d, d), capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"},
                           timeout=30)
        if r.returncode: raise abort(f"profil non chargé (rc {r.returncode}) : {r.stderr.strip()[:200]} — jamais de repli")  # noqa: E701,E501
        self.R["sandbox"]["charge"] = True
    def lancer(self, argv, env, *, net, tmp, home, budget, log, arret=None, pass_fds=()):
        p = subprocess.Popen(self.bornage(argv, net, tmp, home), cwd=self.espace, env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True, pass_fds=pass_fds)
        self.enfant = (p, arret)
        th = threading.Thread(target=copier_log, args=(p.stdout, log), daemon=True)
        th.start()
        w0, raison = time.time(), "fin"
        while p.poll() is None and raison == "fin":
            with contextlib.suppress(subprocess.TimeoutExpired): p.wait(0.2)  # noqa: E701
            if time.time() - w0 > budget:  # Q2 : SEUL le budget mur arrête, jamais le texte ni le silence
                raison = "budget"
        tues = self.arreter(p, arret)
        th.join(5)
        self.enfant = None
        return p.returncode, raison, tues
    def arreter(self, p, arret):
        # R1 : gradué PAR PID de la racine — plus aucun signal de groupe (un pgid libre devient réutilisable)
        arret = arret or {"signal_1": "SIGTERM", "grace_s": 5, "signal_2": "SIGKILL"}
        for sig in (arret["signal_1"], arret["signal_2"]):
            if p.poll() is not None:
                break
            with contextlib.suppress(OSError):
                os.kill(p.pid, getattr(signal, sig))
            with contextlib.suppress(subprocess.TimeoutExpired):
                p.wait(arret.get("grace_s", 5))
        tues = self.tuer_survivants()  # R1 : les descendants signés du bac — et eux seuls — sont tués et comptés ici
        with contextlib.suppress(subprocess.TimeoutExpired):
            p.wait(5)
        return tues
    def tuer_survivants(self) -> int:
        """Descendants de CE run = processus autorisés à écrire <RUN>/engine mais pas <RUN> (signature du bac à sable, qu'aucun
        descendant ne peut quitter : ni chdir, ni env, ni setsid ne l'effacent) ; aucun autre processus n'est visé (mesuré 27/09
        sur ~600 processus), et jamais un processus né avant le run (défense en profondeur, incident du 27/09 00:04)."""
        sonde, senti = self.run / "engine" / ".sonde_bac", self.run / ".sentinelle_bac"
        sonde.parent.mkdir(exist_ok=True); sonde.touch(); senti.touch()  # noqa: E702
        chk = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_int, *[ctypes.c_int64] * 5, ctypes.c_char_p)(("sandbox_check", ctypes.CDLL("/usr/lib/libSystem.B.dylib")))
        ecrit = lambda pid, f: chk(pid, b"file-write-data", 1, 0, 0, 0, 0, 0, os.fsencode(real(f))) == 0  # noqa: E731
        age = lambda e: sum(int(c) * m for c, m in zip(reversed(re.split(r"[-:]", e)), (1, 60, 3600, 86400)))  # noqa: E731
        vises = {p for p, e in ((int(l[0]), l[1]) for l in (x.split() for x in subprocess.run(["/bin/ps", "-axo", "pid=,etime="], capture_output=True, text=True).stdout.splitlines()) if len(l) == 2) if age(e) <= (time.time() - self.w0) + 2 and p != os.getpid() and ecrit(p, sonde) and not ecrit(p, senti) and ecrit(p, sonde)}  # 2e sonde : encore vivant ET autorisé — une erreur de sandbox_check n'est jamais prise pour « interdit »
        if not (sonde.is_file() and senti.is_file()): raise abort("attribution impossible : sonde du bac à sable supprimée pendant le balayage", "survivant détaché")  # noqa: E701
        n = 0
        for pid in vises:
            if not (ecrit(pid, sonde) and not ecrit(pid, senti)):
                continue  # D1 : signature COMPLÈTE revérifiée AU tir ; un pid remplacé ou sans sonde n'est jamais visé
            n += 1  # compté dès la revérification complète passée : un tir refusé (PermissionError) reste un survivant signalé
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        return n
    # ---- tests_base / tests_apres (fn 2, 7) : même profil, réseau coupé, JUnit sous HOME (jamais sous TMPDIR : B1) ---------
    def jouer_tests(self, phase):
        (d := self.run / f"t_{phase}").joinpath("home").mkdir(parents=True)
        junit = d / "home" / "junit.xml"   # B1 : rapport hors de tout chemin dérivable de TMPDIR, l'env que lit le livrable importé
        argv = [x.replace("{junit}", str(junit)) for x in self.test_cmd]
        if phase.startswith("base"):                                  # un test livrable pas encore écrit n'existe pas à la base
            argv = [x for x in argv if x not in self.liv or (self.espace / x).exists()]
        env = {"PATH": TEST_PATH, "HOME": str(d / "home"), "TMPDIR": str(d), "LANG": "en_US.UTF-8",
               "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",  # B:2 : aucun .pyc inscriptible
               "PYTEST_ADDOPTS": "-p no:cacheprovider", "FACTORY_V3_RUN_UUID": self.uuid}
        r, w = os.pipe()
        lu = bytearray()
        th = threading.Thread(target=lambda: lu.extend(b"".join(iter(lambda: os.read(r, 65536), b""))), daemon=True)
        th.start()
        livs = ":".join(str(self.espace / f) for f in self.liv)
        argv = [PY, str(V3 / "juge_isole.py"), str(w), livs, "--", *argv[3:]]
        try:
            rc, raison, _ = self.lancer(argv, env, net="0", tmp=d, home=d / "home", budget=self.timeout,
                                        log=d / "sortie.log", pass_fds=(w,))
        finally:  # C:A1 : bout d'écriture toujours fermé, sinon le lecteur attend sans fin
            os.close(w)
            th.join(10)
            os.close(r)
        support.ecrire_octets(self.run / f"verdict_{phase}.json", bytes(lu))  # R2 : sous RUN, jamais TMP/HOME du bac
        cases = None
        with contextlib.suppress(Exception): cases = (json.loads(bytes(lu)) or {}).get("cas")  # noqa: E501
        return (124 if raison == "budget" else rc), cases  # F1/F3 : verdict du tube non hérité
    def statut_id(self, jid, cases, base):
        """(vert?, rouges, verts, skips_nouveaux) d'un id du juge ; skip/xfail de base = neutre ; nouveau ou absent = rouge."""
        sel = [c for c in cases if correspond(jid, c)]
        skn = [c for c in sel if cases[c] in ("skip", "xfail") and base.get(c) != cases[c]]
        rouges = [c for c in sel if cases[c] == "fail"] if sel else [f"{jid} (absent)"]
        return not (rouges or skn), rouges + skn, [c for c in sel if cases[c] == "pass"], skn
    def tests_base(self):
        with self.etape("tests_base"):
            self.extraire(self.espace, self.caches) if self.caches else None
            rc, c1 = self.jouer_tests("base1")
            tb = self.R["tests_base"] = {"rc": rc, "passes": 1, "deterministe": None,
                                         **{k: sorted(c for c, v in (c1 or {}).items() if v in s) for k, s in
                                            (("rouges", ("fail",)), ("verts", ("pass",)), ("skips", ("skip", "xfail")))}}
            if not c1 and (rc in (0, 5) or c1 == {}): raise abort(f"0 test collecté à la base (rc {rc})")  # noqa: E701
            exiger(c1, f"jalon non déclarable : environnement (rc pytest {rc}, pas de verdict du juge)")
            ko = [j for j in self.agv if not self.statut_id(j, c1, c1)[0]]
            exiger(not ko, f"jalon non déclarable : « à garder verts » non verts à la base : {ko}")
            deja = [j for j in self.afp if self.statut_id(j, c1, {})[0]]
            exiger(not deja, f"déjà vert à la base : {deja}")
            self.remettre_a_base()                                    # passe 2 dans les mêmes conditions que la passe 1
            self.extraire(self.espace, self.caches) if self.caches else None
            rc2, c2 = self.jouer_tests("base2")
            tb.update(passes=2, deterministe=c2 == c1 and (rc2 == 0) == (rc == 0))
            exiger(tb["deterministe"], "jalon non déclarable : test_cmd non déterministe (2 passes différentes)")
            self.base_cases = c1
            self.remettre_a_base()
    # ---- contrat (fn 8) : diff complet vs base (commits, index, worktree, non suivis, ignorés) -----------------------------
    def contrat(self):
        sp = self.espace
        with self.etape("contrat"):
            head = git(sp, "rev-parse", "HEAD").strip()
            chemins = git(sp, "diff", "--name-only", "--no-renames", "-z", self.base_commit).split("\0") + \
                git(sp, "ls-files", "--others", "-z").split("\0")               # --others sans exclusion = ignorés compris
            touches = sorted({p for p in chemins if p and not TEMP_MOTEUR.search(p) and p != BLOCKED})
            viol = [p for p in touches if p not in self.liv] + [f"<commit du moteur {head[:12]}>"] * (head != self.base_commit) + [f"lien symbolique : {f}" for f in self.liv if any(q.is_symlink() for q in [sp / f, *(sp / f).parents][:f.count("/") + 1])]  # noqa: E501 — un livrable-lien renvoie à des octets encore modifiables
            blocked = (sp / BLOCKED).exists()
            self.R["contrat"] = {"touches": touches, "violations": viol, "blocked": blocked}
            git(sp, "add", "-A", "-f")
            (self.run / "diff.patch").write_bytes(git(sp, "diff", "--cached", "--binary", self.base_commit, "--", ".",
                                                      ":(exclude,glob)**/__pycache__/**", ":(exclude,glob)**/.pytest_cache/**",
                                                      check=False).stdout)
            git(sp, "reset", "-q")
            self.copier_livrables(sp, self.run / "figes")
        if blocked: raise abort(f"contrat contesté : {BLOCKED} écrit par le moteur", "contrat contesté")  # noqa: E701
        if viol:  # B:1 : un compte, jamais un nom choisi par le moteur — B2 : jugée AVANT la panne, une violation n'est jamais un essai gratuit
            raise abort(f"contrat touché : {len(viol)} violation(s), liste dans reçu contrat.violations", "triche")
        u = ((self.R.get("engine") or {}).get("usage") or {}).get("output")  # CORR_8 : None = non mesuré ≠ 0
        if not touches and (u == 0 or (ev := lire_jsonl(self.run / "engine.log")) and all(e.get("type") == "error" for e in ev)):  # noqa: E501 — CORR_9 : ≥ 1 événement exigé, all([]) n'est jamais « erreurs seules »
            raise abort("panne moteur : aucun travail (jetons à 0 mesurés, ou journal d'erreurs seules)", "panne_moteur")  # D§2b : seulement si AUCUNE violation
    # ---- juge gelé (fn 7) : écart photographié, épreuves rejouées dans un espace neuf hors de portée du moteur -------------
    def juge(self):
        with self.etape("juge"):
            self.espace_moteur, self.espace = self.espace, self.run / "jugement"  # neuf : hors de portée du moteur
            self.espace.mkdir(parents=True)
            self.extraire(self.espace)
    def tests_apres(self):
        with self.etape("tests_apres"):
            self.copier_livrables(self.run / "figes", self.espace)     # gel du contrat, jamais ré-échantillonné
            rc, cases = p1 = self.jouer_tests("apres")
            if self.jouer_tests("apres2") != p1 or rc == 124:  # R2 : rc ET cas des 2 passes ; délai = aucun verdict
                raise abort("jugement non déterministe ou interrompu (2 passes « après » : rc/cas différents ou délai)")
            cases = cases or {}
            coherent = (rc == 0) == (bool(cases) and all(v != "fail" for v in cases.values()))
            rouges, verts, skn = set(), set(), set()
            for jid in self.afp + self.agv:
                _, r, v, s = self.statut_id(jid, cases, {} if jid in self.afp else self.base_cases)  # AFP : skip/xfail = rouge
                rouges, verts, skn = rouges | set(r), verts | set(v), skn | set(s)
            rouges |= {c for jid in self.afp + self.agv for c in self.base_cases if correspond(jid, c) and c not in cases}
            self.R["tests_apres"] = {"rc": rc, "rouges": sorted(rouges), "verts": sorted(verts), "skips_nouveaux": sorted(skn),
                                     "coherent": coherent}
        if not coherent:
            raise abort(f"juge incohérent : rc pytest {rc} ≠ verdict du tube")
        if rouges:  # B:1 : un compte ; la liste reste au reçu
            raise Fin("ROUGE", 1, f"tests rouges : {len(rouges)}, liste dans reçu tests_apres.rouges",
                      ",".join(sorted(rouges))[:400])
    # ---- consigne (fn 4) + moteur (fn 5) -----------------------------------------------------------------------------------
    def consigne(self) -> str:
        m = self.m
        txt = Template((V3 / "CONSIGNE.md.tmpl").read_text(encoding="utf-8")).safe_substitute(
            jalon=self.jalon, produit=m["produit"], but=str(m["but"]).strip(), spec_ref=m["spec_ref"], blocked=BLOCKED,
            livrables="\n".join(f"- `{x}`" for x in self.liv), a_garder="\n".join(f"- `{x}`" for x in self.agv) or "- (aucun)",
            tache=self.task.decode("utf-8", errors="replace").strip())
        if rouges := getattr(self, "rouges_precedents", ()):  # levier 5 : une seule ligne, seulement s'il y a des noms
            txt += f"\nEssai précédent : ces tests du juge étaient rouges : {', '.join(rouges)}"  # noqa: E501
        (self.run / "CONSIGNE.md").write_text(txt, encoding="utf-8")
        self.R["hash_consigne"] = sha256(txt.encode("utf-8"))
        return txt
    def lancer_moteur(self, txt):
        E, home, tmp = self.moteur, self.run / "engine" / "home", self.run / "engine" / "tmp"
        xdg = {"XDG_CONFIG_HOME": home / ".config", "XDG_DATA_HOME": home / ".local/share", "XDG_CACHE_HOME": home / ".cache",
               "XDG_STATE_HOME": home / ".local/state"}
        for d in (tmp, *xdg.values()):
            d.mkdir(parents=True, exist_ok=True)
        if mag := (E.get("auth") or {}).get("magasin_cli"):  # LIEN, jamais copie : le jeton rafraîchi repart au magasin
            (home / mag).parent.mkdir(parents=True, exist_ok=True)
            os.symlink(real(Path.home() / mag), home / mag)
        dft = E.get("defauts") or {}
        vals = {k: str(dft.get(k, "")) for k in ("model", "max_turns", "budget_usd")} | {
            "home": str(home), "prompt_path": str(self.run / "CONSIGNE.md"), "prompt": txt}
        fmt = lambda s: re.sub(r"\{(\w+)\}", lambda mo: vals.get(mo.group(1), mo.group(0)), s)  # noqa: E731
        argv = [fmt(x) for x in E["argv"]]
        env = {"PATH": f"{os.path.dirname(argv[0])}:{TEST_PATH}", "HOME": str(home), "TMPDIR": str(tmp), "LANG": "en_US.UTF-8",
               "FACTORY_V3_ESPACE": str(self.espace), "FACTORY_V3_RUN_DIR": str(self.run), "FACTORY_V3_BASE_SHA": self.R["base_sha"],
               "FACTORY_V3_RUN_UUID": self.uuid, **{k: str(v) for k, v in xdg.items()}}
        env |= {k: os.environ[k] for k in E.get("env_allowlist") or [] if k in os.environ and k not in ENV_INTERDITS}
        env |= {k: fmt(v) for k, v in (E.get("env_fixe") or {}).items() if k not in ENV_INTERDITS}
        with self.etape("moteur"):
            rc, raison, tues = self.lancer(argv, env, net="1", tmp=tmp, home=home, budget=self.budget,
                                           log=self.run / "engine.log", arret=E["arret"])
            usage, cout = lire_usage(E.get("usage"), self.run / "engine.log", raison == "fin")
            self.R["engine"] = {"id": E["id"], "cli_version": E["cli_version"], "model": vals["model"] or None, "usage": usage,
                                "format_sortie": E["format_sortie"], "config_sha256": E["config_sha256"], "cost_usd": cout,
                                "plafonds_effectifs": {"mecanisme": E["plafonds"]["mecanisme"], "budget_mur_s": self.budget,
                                                       "drapeaux": [fmt(x) for x in E["plafonds"].get("drapeaux", [])]},
                                "raison_fin": raison, "rc": rc, "survivants_detectes": tues}
            if mag and not (os.path.islink(home / mag) and os.readlink(home / mag) == real(Path.home() / mag)):
                raise abort(f"magasin ~/{mag} : lien du home moteur remplacé ; engine/home gardé (jeton)",
                            "magasin remplacé")  # R2 : un jeton rafraîchi peut n'exister QUE là
            if tues: raise abort(f"descendant détaché détecté à la fin du moteur : {tues} processus (SIGKILL tenté — la détection suffit)", "survivant détaché")  # noqa: E701
            if self.R["quota"]:
                self.R["quota"]["pct_apres"] = (lire_quota(self.R["quota"]["pool"]) or {}).get("pct")
    # ---- commit_propre (fn 9) ; L2 : branche (jalon, base, contrat) retrouvée au rejeu, vérifiée ; jamais 2 commits, jamais forcée ----
    def publier(self):
        with self.etape("publication"):
            base, hc = self.R["base_sha"], self.R["hash_contrat"]
            branche = f"v3/{self.jalon}-{base[:8]}-{hc[:16]}"
            sha = existante = git(self.repo, "rev-parse", "-q", "--verify", f"refs/heads/{branche}", check=False).stdout.decode().strip()
            if not existante:
                pub = self.base / "publish" / self.uuid[:8]
                git(self.repo, "worktree", "add", "--detach", "-q", str(pub), base)
                try:
                    self.copier_livrables(self.run / "figes", pub)
                    git(pub, "add", "-A", "--", ".")
                    git(pub, "commit", "-q", "--no-verify", "-m", f"v3: {self.jalon} essai {self.R['essai']}\n\nrun_uuid: "
                        f"{self.uuid}\nhash_contrat: {hc}\nbase: {base}\n")
                    sha = git(pub, "rev-parse", "HEAD").strip()
                finally:
                    git(self.repo, "worktree", "remove", "--force", str(pub), check=False)
            files = [x for x in git(self.repo, "diff", "--name-only", "--no-renames", "-z", base, sha).split("\0") if x]
            publies = {e.split("\t", 1)[1]: tuple(e.split("\t", 1)[0].split()[:3:2]) for e in git(self.repo, "ls-tree", "-z", sha, "--", *self.liv).split("\0") if e}  # noqa: E501
            attendu = {f: ("120000" if p.is_symlink() else "100755" if p.stat().st_mode & 0o100 else "100644", blob_sha(p)) for f in self.liv if (p := self.run / "figes" / f).exists() or p.is_symlink()}  # noqa: E501
            entete = git(self.repo, "log", "-1", "--format=%P%n%B", sha)
            if publies != attendu or not entete.startswith(base + "\n") or f"\nhash_contrat: {hc}\n" not in entete or not set(files) <= set(self.liv):
                raise abort(f"publication ≠ base, contrat, livrables ou octets/modes jugés : {branche} (jamais forcée)", "publication altérée")
            if not existante:
                git(self.repo, "branch", branche, sha)                   # sans -f : une branche apparue entre-temps fait échouer, jamais écrasée
            self.R["commit"] = {"sha": sha, "branch": branche, "files": files, "depot": str(self.repo), "retrouvee": bool(existante)}
    # ---- déroulé : verrou → reçu RUNNING → profil → jalon → doublon/quota/disque → espace → tests_base → moteur → ... ----------
    def derouler(self):
        exiger(os.path.relpath(real(self.repo), real(ROOT)).split("/")[0] in ("..", ".context"),
               "dépôt produit dans le dépôt de contrôle (hors .context) : utiliser un checkout isolé")  # A:F1
        mf = Path.home() / ".factory_v3" / "machine_id"
        mid = os.environ.get("FACTORY_V3_MACHINE_ID") or (mf.read_text().strip() if mf.is_file() else "")
        exiger(re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", mid), "machine_id absent (FACTORY_V3_MACHINE_ID ou ~/.factory_v3/machine_id)")
        exiger(re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", self.jalon), f"id de jalon invalide : {self.jalon!r}")
        self.R["machine_id"] = mid
        self.ev("ADMISSION_START", "admission")
        self.verrou()
        self.ecrire()                                                     # reçu RUNNING à l'admission, sous verrou, avant l'espace
        self.ev("ADMISSION_END", "admission", {"pid": os.getpid(), "lock": str(self.lock)})
        self.sonder_profil()
        self.lire_jalon()
        self.R["base_sha"] = git(self.repo, "rev-parse", "HEAD").strip()
        self.moteur = None if self.a.test_only else self.resoudre_moteur()
        self.admission()
        self.ouvrir_espace()
        self.tests_base()
        if self.a.test_only: raise Fin("VERT", 0, "test-only : jalon déclarable (à faire passer rouges, à garder verts, déterministe)")  # noqa: E701,E501
        self.lancer_moteur(self.consigne())
        self.contrat()
        self.juge()
        self.tests_apres()
        self.publier()
        raise Fin("VERT", 0, "tests du juge verts, contrat intouché, juge gelé")
    def terminer(self, f: Fin) -> int:
        for s in (signal.SIGTERM, signal.SIGHUP):  # C:A3 : un 2e signal ne coupe plus l'arrêt du moteur
            signal.signal(s, signal.SIG_IGN)
        if self.enfant:
            with contextlib.suppress(Exception):
                self.arreter(*self.enfant)
        self.R.update(verdict=f.verdict, rc=f.rc, motif=f.motif, motif_echec=f.motif_echec, finished_at=support.now(),
                      t_monotone_total=round(max(0.0, mono() - self.m0), 3), t_wall_total=round(time.time() - self.w0, 3))
        self.ecrire()
        if self.lockfd is not None:  # C:A4 : flock rendu avec le reçu durable ; le fichier reste (jamais unlink)
            os.close(self.lockfd)
        self.ev(f.verdict if f.verdict in ("REFUS", "ABORT") else "VERDICT", self.R["etape"], {"verdict": f.verdict, "rc": f.rc, "motif": f.motif})
        reconcilier(self.index, [self.run.parent])                          # B6 : reçu écrit = fait foi ; l'index se recalcule depuis les reçus
        home = None if f.motif_echec in ("magasin remplacé", "panne_moteur") else self.run / "engine" / "home"  # R2/CORR_8 : jeton ou journal serveur à conserver
        for sp in (self.espace_moteur, self.espace, home, self.run / "engine" / "tmp", *self.run.glob("t_*")):  # C:A5
            if sp and sp.exists() and not self.a.keep:  # K3/F1 : ABORT compris ; échec de purge sans effet
                with contextlib.suppress(OSError):
                    shutil.rmtree(sp, onexc=lambda fn, p, e: (os.chmod(os.path.dirname(p), 0o755), fn(p)))
        print(f"[v3] {self.jalon} {f.verdict} rc={f.rc} — {f.motif}\n[v3] reçu {self.run / 'RECEIPT.json'}")
        return f.rc

def juger(a) -> int:
    """--juger ARBRE [--livrables a:b] -- <args pytest> (L1) : acceptation et intégration jugées par jouer_tests ; verdict JSON sur stdout."""
    r = Run.__new__(Run); r.espace, r.moteur, r.enfant, r.w0, r.uuid = Path(a.juger).resolve(), None, None, time.time(), str(uuid.uuid4())
    r.liv, r.test_cmd, r.timeout = [x for x in (a.livrables or "").split(":") if x], [PY, "-m", "pytest", *a.args], a.budget_s or 900
    r.run = r.espace.parent / f".bac_{r.uuid}"  # TMP, HOME, verdict HORS de l'arbre : l'arbre reste en lecture seule (NET=0)
    try: rc, cas = r.jouer_tests("juger")  # noqa: E701
    finally:  # 04/10 : fin du journal pytest sur STDERR (diag, jamais lu pour le verdict) ; TMP inscriptible : aucun lien suivi, ni fifo
        with contextlib.suppress(OSError), open(os.open(r.run / "t_juger/sortie.log", os.O_NOFOLLOW_ANY | os.O_NONBLOCK), "rb") as f:
            f.seek(max(0, os.fstat(f.fileno()).st_size - 6000)); sys.stderr.write(f.read(6000).decode(errors="replace"))  # noqa: E702
        shutil.rmtree(r.run, ignore_errors=True)
    return print(json.dumps({"rc": rc, "cas": cas})) or (0 if cas is not None else 2)

# ---- cli (fn 12) --------------------------------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    for opt in ("--project", "--milestone", "--repo", "--juger", "--livrables"):
        ap.add_argument(opt)
    ap.add_argument("--engine", default="glm", choices=sorted(x.stem for x in (V3 / "moteurs").glob("*.json")))
    ap.add_argument("--budget-s", type=int)
    for opt in ("--test-only", "--keep", "--hash-contrat"):
        ap.add_argument(opt, action="store_true")
    ap.add_argument("args", nargs="*")                                    # --juger : arguments pytest après « -- »
    a = ap.parse_args(argv)
    if a.juger: return juger(a)  # noqa: E701
    if not a.project or not a.milestone:
        return print("REFUS : --project et --milestone obligatoires", file=sys.stderr) or 3
    try:
        proj, m, repo = charger(a.project, a.milestone, a.repo)
    except (OSError, yaml.YAMLError) as exc:
        return print(f"REFUS : ROADMAP.yaml illisible : {exc}", file=sys.stderr) or 3
    if a.hash_contrat:
        tp = task_path(proj, repo, m) if m else None
        if not tp:
            return print("REFUS : jalon ou task_file introuvable", file=sys.stderr) or 3
        return print(sha256(contrat_octets(m, tp.read_bytes()))) or 0
    if not (repo and repo.is_dir() and git(repo, "rev-parse", "--git-dir", check=False).returncode == 0):
        return print(f"REFUS : dépôt produit absent ou non git (--repo ou product_repo) : {repo}", file=sys.stderr) or 3
    try:
        r = Run(a, proj, m, repo)
    except BaseException as exc:  # C5 : construction ratée = ABORT rc 2 (jamais 1 = ROUGE), sans reçu
        return print(f"ABORT : construction du run impossible : {exc}"[:500], file=sys.stderr) or 2
    for s in (signal.SIGTERM, signal.SIGHUP):                             # arrêt demandé = ABORT avec reçu, moteur tué
        signal.signal(s, signal.default_int_handler)
    try:
        try:
            r.derouler()
        except Fin as e:
            f = e
        except BaseException as exc:                                      # toute exception non prévue = ABORT rc 2 avec reçu
            f = abort("arrêt demandé (SIGTERM/SIGHUP/SIGINT)" if isinstance(exc, KeyboardInterrupt)
                      else f"exception {type(exc).__name__}: {exc}"[:500])
        return r.terminer(f)                                              # B7 : verdict ET finalisation sous le même traitement extérieur
    except BaseException as exc:                                          # finalisation elle-même en échec, terminer jamais rappelé
        err = f"finalisation interrompue ({type(exc).__name__}: {exc})"[:500]
        durable = (support.read_json(r.run / "RECEIPT.json") or {}).get("verdict") not in (None, "RUNNING")  # C12 : verdict déjà écrit ?
        if durable:
            r.R.update(finalisation=err)                                  # C12 : la panne est ajoutée, le verdict durable n'est jamais réécrit
        else:
            r.R.update(verdict="ABORT", rc=2, motif_echec="finalisation interrompue", finished_at=support.now(), motif=err)
        with contextlib.suppress(Exception):
            r.ecrire()                                                    # reçu impossible (stockage mort) : rc 2 + diagnostic, aucun VERT prétendu
        print(f"[v3] {r.jalon} {r.R['verdict']} rc={r.R['rc']} — {err}", file=sys.stderr, flush=True)
        return r.R["rc"] if durable else 2


if __name__ == "__main__":
    sys.exit(main())
