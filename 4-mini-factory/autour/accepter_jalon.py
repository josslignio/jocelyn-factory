"""Acceptation MÉCANIQUE d'un jalon préparé (chemin court 05/10 : UNE passe de préparation ; plus de patch honnête ni de triches rédigés par IA).
Usage : python3 accepter_jalon.py <dossier projet> <dépôt copie> <dossier ID avec contrat.yaml> [racine imposée par le runner]
Règles (toutes exigées, sinon REFUS — tout REFUS de la base précède toute écriture hors de la zone de travail dos/acceptation/) :
  id      : celui du dossier, imposé par le runner (un redécoupage ne remplace jamais le bloc de sa racine) ;
  base    : chaque a_faire_passer ROUGE, chaque a_garder_verts VERT — le juge prouve qu'il sait rougir, à coût machine nul ; absent ou sauté ≠ conforme ;
  livrables sans fichier de test ni configuration lue par pytest ; juge sans serveur (bind/listen/serve_forever : le juge tourne sans réseau).
La base est jugée par le noyau (run.py --juger, L1 02/10) : même bac, même HOME jetable, même réseau coupé que le jugement d'un run.
Si accepté : juge déposé et commité dans la copie, contrat ajouté à ROADMAP.yaml (racine, depend_de, attente_proprietaire conservés) + fichier de tâche,
hash calculé par le noyau, --test-only du noyau VERT ; résultat dans acceptation.json, écrit en dernier (étape 4). La mise en file (mf, --essais 1) est faite par le runner, qui sélectionne. Aucun moteur lancé ici.
Les refus noyau finaux (après l'écriture du juge, du commit et de la roadmap) disent « contrat écrit mais NON mis en file »."""
import contextlib, json, os, re, shutil, subprocess, sys
from pathlib import Path
import yaml
def fin(v, motif, d=""):  # résultat STRUCTURÉ écrit en DERNIER, atomiquement (étape 4) : une sortie partielle n'est jamais prise pour une acceptation
    r = Path(sys.argv[3]).resolve() / "acceptation.json"
    r.with_suffix(".tmp").write_text(json.dumps({"id": globals().get("ID"), "verdict": v, "motif": motif}, ensure_ascii=False)); os.replace(r.with_suffix(".tmp"), r)
    sys.exit(print(f"{d}{v} {globals().get('ID')} : {motif}") or (0 if v == "ACCEPTÉ" else 3))
sys.excepthook = lambda t, v, tb: fin("REFUS", f"erreur interne de l'acceptation (à corriger dans le contrat ou le juge) : {t.__name__}: {v}")  # 01/10 : un plantage devient un REFUS

RADAR = Path(__file__).resolve().parent
NOYAU = (RADAR.parent / "ACTIF").resolve() / "noyau/factory_v3/run.py"
projet, depot, dos = (Path(x).resolve() for x in sys.argv[1:4]); RACINE = sys.argv[4] if len(sys.argv) > 4 else None
c = yaml.safe_load((dos / "contrat.yaml").read_text()); ID = c["id"]
for k in ("a_faire_passer", "a_garder_verts"):          # nom de test seul → rattaché au fichier du juge (format GLM toléré)
    c[k] = [x if ".py" in x else f"{c['chemin_juge']}::{x}" for x in (c.get(k) or [])]
juge_src = next(dos.glob("test_*.py"))
refus = lambda m, d="": fin("REFUS", m, d)  # noqa: E731  (d : fin de la sortie pytest du juge, AVANT la ligne REFUS)
GELES = ("pyproject.toml", "setup.cfg", "tox.ini", "pytest.ini", "conftest.py")  # 05/10 : un pyproject livré = juge sans verdict, 15 abandons sur 15

if ID != dos.name: refus(f"id du contrat ≠ id imposé par le runner ({dos.name})")  # noqa: E701  (un renommage ne remplace ni n'échappe à une racine)
if any("test" in Path(x).name or Path(x).name in GELES for x in c["livrables"]): refus(f"un livrable est un fichier de test ou de configuration pytest {GELES}")  # noqa: E701,E501
if re.search(r"\.bind\(|\.listen\(|serve_forever", juge_src.read_text()): refus("juge avec serveur (bind/listen) : le juge tourne sans réseau")  # noqa: E701
if c["chemin_juge"] != f"juge_noyau/test_{ID.lower()}.py": refus(f"chemin du juge imposé : juge_noyau/test_{ID.lower()}.py (jamais le juge d'un autre jalon)")  # noqa: E701  Codex 05/10
if re.search(r"def test_|assert ", str(c.get("tache", ""))): refus("tache contient du code de juge : elle va au constructeur, le juge jamais")  # noqa: E701  Codex 05/10
if len(f"{c.get('tache', '')}{c['but']}".encode()) > 65536: refus("tache + but > 64 Ko : la consigne part en argument du moteur (E2BIG)")  # noqa: E701  NC-15
CH = [*c["livrables"], c["chemin_juge"]]
if any(Path(x).is_absolute() or ".." in Path(x).parts for x in CH): refus("chemin hors copie")  # noqa: E701

def jouer(nom):
    global DIAG; DIAG = ""; v = dos / "acceptation" / nom; shutil.rmtree(v, ignore_errors=True); v.mkdir(parents=True)
    a = subprocess.Popen(["git", "-C", str(depot), "archive", "HEAD"], stdout=subprocess.PIPE)
    subprocess.run(["tar", "-x", "-C", str(v)], stdin=a.stdout, check=True); a.stdout.close(); a.wait()
    (v / c["chemin_juge"]).parent.mkdir(parents=True, exist_ok=True); shutil.copy(juge_src, v / c["chemin_juge"])
    fichiers = sorted({c["chemin_juge"], *(g.split("::")[0] for g in c["a_garder_verts"])})
    j = subprocess.run([sys.executable, str(NOYAU), "--juger", str(v), "--livrables", ":".join(c["livrables"]), "--budget-s", "900", "--", "-q", "-p", "no:cacheprovider",
                        "-p", "no:rerunfailures", *fichiers], capture_output=True, text=True, errors="replace", timeout=1000)  # L1 : jugé PAR le noyau
    lu, DIAG = j.stdout.strip().rpartition("\n")[2], "".join(f"  | {x}\n" for x in j.stderr.replace("ACCEPTÉ", "accepté").splitlines()[-25:])  # stderr : diag
    (v / "verdict_isole.json").write_text(lu); (v / "sortie_juge.txt").write_text(j.stderr); cases = None   # verdict = stdout du noyau, jamais relu
    with contextlib.suppress(Exception): cases = (json.loads(lu) or {}).get("cas")
    if not cases: return pq(f"jugement sans verdict (voir acceptation/{nom}/verdict_isole.json)")  # Q5b
    return {k: {"fail": "rouge", "pass": "vert"}.get(st, "saute") for k, st in cases.items()}  # Q5a : skip/xfail ≠ vert

_jouer = jouer
def pq(m): globals().update(POURQUOI=m)  # noqa: E704
def jouer(nom):   # 28/09 04:5x (pilote) : l'arbre extrait (622 Mo × 4 par jalon Twitter) est effacé après lecture ; seul verdict_isole.json (la preuve) reste
    try: return _jouer(nom)
    finally:
        for q in (dos / "acceptation" / nom).iterdir():
            if q.name not in ("verdict_isole.json", "sortie_juge.txt"): shutil.rmtree(q) if q.is_dir() and not q.is_symlink() else q.unlink()

def statut(etat, i):
    s = [v for k, v in etat.items() if k == i or k.startswith(i + "::") or (i.endswith(".py") and k.split("::")[0] == i)]
    return "absent" if not s else ("rouge" if "rouge" in s else "saute" if "saute" in s else "vert")  # Q5a : « saute » distinct de « vert »

base = jouer("base")
if base is None: refus(f"base inapplicable : {globals().get('POURQUOI', '?')}", DIAG)  # noqa: E701
mauvais = [i for i in c["a_faire_passer"] if statut(base, i) != "rouge"] + [g for g in c["a_garder_verts"] if statut(base, g) != "vert"]
if mauvais: refus(f"base : attendus rouges/verts non conformes : {mauvais}", DIAG)  # noqa: E701
print(f"BASE OK {ID} : {len(c['a_faire_passer'])} rouge(s), {len(c['a_garder_verts'])} vert(s) à garder ; aucun patch rédigé par IA")

# --- dépôt du juge, contrat, hash, validation, mini-factory ---
shutil.copy(juge_src, depot / c["chemin_juge"])
env = {**os.environ, "GIT_AUTHOR_NAME": "Jocelyn Factory", "GIT_COMMITTER_NAME": "Jocelyn Factory",
       "GIT_AUTHOR_EMAIL": "jocelyn.grosjean@gmail.com", "GIT_COMMITTER_EMAIL": "jocelyn.grosjean@gmail.com"}
subprocess.run(["git", "-C", str(depot), "add", c["chemin_juge"]], check=True)
subprocess.run(["git", "-C", str(depot), "commit", "-q", "-m", f"juge {ID} (préparation unique, base rouge vérifiée)"], check=False, env=env)  # rejouable : rien à commiter = juge inchangé
(projet / f"{ID}.md").write_text(f"# Tâche {ID}\n\n{c['tache'].strip()}\n")
tests = sorted({c["chemin_juge"], *(g.split("::")[0] for g in c["a_garder_verts"])})
m = {"id": ID, "produit": projet.parent.name, "type": "construction", "spec_ref": f"{ID}.md", "but": c["but"], "task_file": f"{ID}.md",
     "livrables": c["livrables"], "juge": {"a_faire_passer": c["a_faire_passer"], "a_garder_verts": c["a_garder_verts"]},
     "test_cmd": ["python3", "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:rerunfailures", "--junitxml", "{junit}", *tests],
     "timeout_s": 600, "budget_moteur_s": 1800, "reseau_tests": False}
if c["chemin_juge"].startswith("juge_noyau/"): m["juge_isole"] = True   # noyau final : juge isolé obligatoire (lot O)
rm = projet / "ROADMAP.yaml"; t0 = rm.read_text()
K = next((k for k in (f"- id: {json.dumps(ID)}\n", f"- id: {ID}\n") if k in t0), None)  # 30/09 : l'id est écrit ENTRE GUILLEMETS → l'ancien bloc n'était jamais retrouvé (doublons, hash périmé)
ancien = next((x for x in (yaml.safe_load(t0) or {}).get("milestones") or [] if isinstance(x, dict) and x.get("id") == ID), {})
m.update({k: ancien[k] for k in ("racine", "depend_de", "attente_proprietaire") if k in ancien}, **({"racine": RACINE} if RACINE and RACINE != ID else {}))
if K:                                                     # rejouable : l'ancien bloc de CE jalon est remplacé, jamais dupliqué
    i = t0.index(K); j = t0.find("\n- id:", i + 1); t0 = t0[:i] + (t0[j + 1:] if j >= 0 else "")
L = t0.rstrip("\n").split("\n"); i = L.index("milestones:"); j = next((k for k in range(i + 1, len(L)) if L[k][:1].isalpha()), len(L))  # 06/10
t0 = "\n".join(L[:i] + L[j:] + L[i:j]) + "\n"  # clés après milestones (IndexError TTSIL01) remontées AVANT ; retrait+ajout en UNE écriture (Codex 05/10)
rm.with_suffix(".tmp").write_text(t0 + "".join(("- " if k == "id" else "  ") + f"{k}: {json.dumps(v, ensure_ascii=False)}\n" for k, v in m.items()))  # pas de yaml.safe_dump
os.replace(rm.with_suffix(".tmp"), rm)
h = subprocess.run([sys.executable, str(NOYAU), "--hash-contrat", "--project", str(projet), "--milestone", ID], capture_output=True, text=True,
                   env={**os.environ, "FACTORY_NO_REAL_ENGINE": "1"}).stdout.strip().splitlines()[-1]
with open(projet / "ROADMAP.yaml", "a") as f:
    f.write(f'  valide: {{par: claude_pilote_auto, hash_contrat: "{h}", date: "2026-09-27", note: "base mécanique (rouges rouges, verts verts) ; préparation unique : sans indépendance de famille"}}\n')
to = subprocess.run([sys.executable, str(NOYAU), "--test-only", "--project", str(projet), "--milestone", ID], capture_output=True, text=True,
                    env={**os.environ, "FACTORY_NO_REAL_ENGINE": "1"}, timeout=2700)
if " VERT rc=0" not in to.stdout:                      # le contrat ÉCRIT relu par le noyau (lire_jalon, hash) et sa base rejouée 2 fois (déterminisme)
    refus(f"--test-only du noyau non VERT (contrat écrit mais NON mis en file) : {(to.stdout + to.stderr).strip()[-300:]}")
fin("ACCEPTÉ", f"hash {h[:16]} ; racine {m.get('racine', ID)} ; mise en file par le runner")
