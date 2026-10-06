# TR_UNIVERSE_VERIFIER_V1 — spec + task file — `factory_v2/verify_tr_universe_expansion.py` — v4

Corrige apres ACCEPT_WITH_FIXES round 3 Codex (`CODEX_COUNTER_AUDIT_TR_UNIVERSE_3.md`, findings
P1-1 et P1-2 -- rounds 1/2 restent acquis, voir historique v2/v3). Ce document sert de
`task_file` pour le jalon `TR_UNIVERSE_VERIFIER_V1` (ROADMAP patch v4) ET de spec detaillee du
verificateur. A copier vers `factory/campaigns/twitter-radar/tasks/TR_UNIVERSE_VERIFIER_V1.md`
si l'amendement est applique.

## Ce qui change par rapport a v3 (round 3 ACCEPT_WITH_FIXES)
4. **(P1) Suppression de l'exception MONITORED** : round 2 tolerait qu'un compte ajoute entre
   directement en `MONITORED` si `justification` citait un chemin de preuve de corpus, verifie
   seulement par `Path.is_file()` sur le worktree -- ni committe, ni lie au handle, ni preuve
   reelle de cadence. Codex round 3 juge cette exception insuffisamment prouvee. Correctif : la
   regle devient absolue. Tout compte AJOUTE (absent du snapshot fige des 14) doit avoir
   `lifecycle == "SAMPLED"`. Tout compte ajoute avec `lifecycle == "MONITORED"` fait echouer le
   verificateur en rc=1, sans lire `justification` ni chercher un fichier de preuve.
5. **(P1) Snapshot fige des 14 comptes historiques** : round 2 ne comparait que `handle` et
   `lifecycle` (plus `root`/`never_auto_prune` pour `Frenchie` seul). Codex round 3 exige la
   comparaison de TOUS les champs pertinents pour les 14. Correctif : le verificateur embarque
   EN DUR (constante Python, jamais relue depuis un fichier externe ni depuis le fichier univers
   lui-meme) le snapshot exact des 14 comptes tel que present dans
   `research/tr_kol_universe.json` au moment de cet amendement -- `handle`, `lifecycle`, `root`,
   `never_auto_prune`, `has_corpus`, `reason` -- et rejette rc=1 toute divergence sur un seul de
   ces six champs pour un de ces 14 handles.

## Ce qui change par rapport a v2 (round 2 REJECT)
1. **(P0) Emplacement** : le fichier vit DANS LE DEPOT PRODUIT
   (`/Users/jocelyngrosjean/conductor/workspaces/twitter-radar/twitter-radar-foundation-v2/factory_v2/verify_tr_universe_expansion.py`),
   jamais dans le depot de controle. Raison prouvee : `factory_v2/authority_gate.py:343-358`
   verifie tous les `deliverables` (`required_artifacts`) via `artifact_path = e2e_cwd /
   str(artifact)`, et `e2e_cwd` est toujours resolu au `product_repo` du ROADMAP
   (`factory_v2/validation_contract.py:93,100` + `factory_v2/authority_gate.py:127-141`) — un
   chemin control-repo n'est donc jamais trouve, jamais WIRED, jamais settlable. C'est
   l'emplacement deja utilise par `factory_v2/verify_tr_weekly_operate.py` et
   `factory_v2/verify_cycle2_refresh.py`, tous deux DEJA PRESENTS a ce chemin exact dans le
   depot produit (verifie par `ls`/`find` au moment de cet audit).
2. **(P0) Autonomie** : le module est stdlib Python pur (`argparse, hashlib, json, os, re,
   subprocess, sys, tempfile, datetime` — memes imports que `verify_tr_weekly_operate.py`).
   Aucun `from factory_v2.xxx import ...`, aucune dependance a `PYTHONPATH` ni a
   `FACTORY_CONTROL_REPO` : ces variables n'existent pas forcement quand ce script s'execute
   comme fichier autonome dans le depot produit.
3. **(P1) Livrable unique** : seul `factory_v2/verify_tr_universe_expansion.py` est declare comme
   `deliverables` du jalon. `tests/test_verify_tr_universe_expansion.py` reste une obligation
   reelle (le builder DOIT l'ecrire) mais n'est plus liste comme deliverable — son existence et
   sa reussite sont prouvees indirectement par `product_e2e.test_command`
   (`python3 -m pytest tests/test_verify_tr_universe_expansion.py -q`, cwd = depot produit) : si
   le test manque ou echoue, la commande echoue et le jalon n'est pas settlable, sans qu'un
   deuxieme artefact ait besoin d'etre verifie separement par le gate.

## Outcome
`factory_v2/verify_tr_universe_expansion.py` existe dans le depot PRODUIT
(twitter-radar-foundation-v2), est fail-closed, autonome (stdlib seulement), et
`python3 -m pytest tests/test_verify_tr_universe_expansion.py -q` (execute depuis la racine du
depot produit) passe (>=14 cas, dont >=10 negatifs). Ce jalon ne touche a aucun fichier du depot
de controle.

## Deliverables
- `factory_v2/verify_tr_universe_expansion.py` (seul livrable declare)

Non declare comme livrable mais requis pour que `product_e2e.test_command` passe :
- `tests/test_verify_tr_universe_expansion.py`

## Depends On
- Jalon `TR_KOL_DISCOVERY_AND_RANKING_V1` COMPLETE (les fichiers sources existent et sont stables :
  `research/tr_kol_ranking.json`, `research/X109_DISCOVERY_CANDIDATES.json` dans le depot produit).
- Moteur : build de niveau B, ENTIEREMENT dans le depot produit twitter-radar
  (`executor.cwd: product_repo`).

## CLI
```
python3 factory_v2/verify_tr_universe_expansion.py <universe_json_path> --project-repo <repo>
```
- Invocation par CHEMIN DE SCRIPT (pas par module `-m`), car le fichier vit deja dans le cwd
  d'execution (`factory_v2/verify_tr_universe_expansion.py` relatif a la racine du depot
  produit) — pas de PYTHONPATH necessaire, contrairement a v2.
- `<universe_json_path>` : `research/tr_kol_universe.json`, relatif au cwd (le depot produit).
- `--project-repo` (obligatoire) : racine git du depot produit (`.` en pratique, puisque cwd =
  depot produit), pour toutes les verifications git (tracked-at-HEAD, resolution de commit,
  contenu de blob a un commit exact).

### Preuve de la convention d'execution (fichier:ligne)
- `factory_v2/authority_gate.py:343-358` — le gate exige que TOUS les `required_artifacts`
  existent sous `e2e_cwd`, qui est toujours le `product_repo` resolu (jamais le depot de
  controle) : un livrable place dans `factory_v2/` du CONTROL-repo ne sera jamais trouve ici.
- `factory_v2/validation_contract.py:93,100` — `product_repo` du contrat gele vient
  inconditionnellement du champ racine `product_repo:` de `ROADMAP.yaml`
  (twitter-radar-foundation-v2), jamais d'un champ par-jalon.
- `factory_v2/authority_gate.py:127-141` (`_resolve_product_repo`) — le cwd d'execution de
  `product_e2e.test_command` est ce meme `product_repo`. Avec le script et le test tous deux
  situes dans ce depot, `python3 -m pytest tests/test_verify_tr_universe_expansion.py -q` et
  `python3 factory_v2/verify_tr_universe_expansion.py ...` resolvent directement, sans variable
  d'environnement.
- Precedent direct : `factory_v2/verify_tr_weekly_operate.py` et
  `factory_v2/verify_cycle2_refresh.py` existent deja a cet emplacement exact dans
  `twitter-radar-foundation-v2/factory_v2/`, et leurs tests correspondants
  (`tests/test_tr_weekly_operate_v1.py`, `tests/test_cycle2_refresh.py`) existent dans
  `twitter-radar-foundation-v2/tests/` — meme repertoire que les ~70 autres fichiers
  `tests/test_*.py` du depot produit.

## Codes de sortie
- `0` — acceptance prouvee. stdout : `TR_UNIVERSE_EXPANSION_OK: accounts=<N> new=<K> head=<sha40>`.
- `1` — fichier lisible et valide mais criteres non atteints (ex. hors plage 25-30, promotion
  MONITORED non justifiee). stderr : `TR_UNIVERSE_EXPANSION_UNMET: <raison>`.
- `2` — entree illisible/non verifiable : fichier absent, JSON corrompu, schema invalide, fichier
  non committe a HEAD, source citee introuvable/non committee, **handle absent de la liste
  candidates au commit cite**. Jamais un succes deguise.

## Controles fail-closed (dans l'ordre, chaque echec est terminal)
1. **Fichier et schema** : fichier existe, JSON valide, `schema == "tr-kol-universe/v2"`,
   `accounts` liste non vide de dicts → sinon rc=2.
2. **Git-tracked a HEAD** : fichier univers committe a `HEAD` du `--project-repo` (meme methode
   que `_tracked_at_head` dans `verify_tr_weekly_operate.py`) → sinon rc=2 `UNIVERSE_NOT_GIT_TRACKED`.
3. **Cardinalite** : `25 <= len(accounts) <= 30` → sinon rc=1 `ACCOUNT_COUNT_OUT_OF_RANGE:<n>`.
4. **Champs obligatoires** : `handle`, `lifecycle` in `{MONITORED, SAMPLED}`, `category` (str non
   vide, jamais egale a `"UNVERIFIED_SPECIALTY"` ni a une sous-chaine `"a confirmer"`/`"TBD"`/
   `"TODO"` insensible a la casse), `justification` (str non vide) → sinon rc=1
   `ACCOUNT_FIELDS_MISSING`/`ACCOUNT_CATEGORY_PLACEHOLDER_REJECTED:<handle>`.
5. **Retention des 14 existants — snapshot fige complet** : le verificateur embarque EN DUR
   (jamais lu depuis le fichier univers ni depuis un fichier externe) le snapshot exact des 14
   comptes ci-dessous. Pour chacun des 14 handles, TOUS les champs suivants doivent correspondre
   EXACTEMENT a ce present dans `accounts` du fichier verifie : `handle`, `lifecycle`, `root`,
   `never_auto_prune`, `has_corpus`, `reason` → toute divergence sur un seul champ, ou une
   absence du handle, → rc=1 `EXISTING_ACCOUNT_FIELD_DIVERGED:<handle>:<field>` (absence du
   handle lui-meme → `EXISTING_HANDLE_DROPPED:<handle>`).

   Snapshot fige embarque (source : `research/tr_kol_universe.json`, lu en lecture seule au
   moment de cet amendement, `generated_at: 2026-09-11T15:27:17Z`) :

   ```python
   FROZEN_EXISTING_ACCOUNTS = [
       {"handle": "BryzonX", "lifecycle": "MONITORED", "root": False, "never_auto_prune": False, "has_corpus": True, "reason": "corpus exists; refreshed this session"},
       {"handle": "CKCapitalxx", "lifecycle": "MONITORED", "root": False, "never_auto_prune": False, "has_corpus": True, "reason": "corpus exists; refreshed this session"},
       {"handle": "DeepValueBagger", "lifecycle": "MONITORED", "root": False, "never_auto_prune": False, "has_corpus": True, "reason": "corpus exists; refreshed this session"},
       {"handle": "Frenchie", "lifecycle": "MONITORED", "root": True, "never_auto_prune": True, "has_corpus": True, "reason": "corpus exists; refreshed this session"},
       {"handle": "Gubloinvestor", "lifecycle": "MONITORED", "root": False, "never_auto_prune": False, "has_corpus": True, "reason": "corpus exists; refreshed this session"},
       {"handle": "HyperTechInvest", "lifecycle": "MONITORED", "root": False, "never_auto_prune": False, "has_corpus": True, "reason": "corpus exists; refreshed this session"},
       {"handle": "LiebermanAustin", "lifecycle": "MONITORED", "root": False, "never_auto_prune": False, "has_corpus": True, "reason": "corpus exists; refreshed this session"},
       {"handle": "PhotonBull", "lifecycle": "MONITORED", "root": False, "never_auto_prune": False, "has_corpus": True, "reason": "corpus exists; refreshed this session"},
       {"handle": "RJCcapital", "lifecycle": "MONITORED", "root": False, "never_auto_prune": False, "has_corpus": True, "reason": "corpus exists; refreshed this session"},
       {"handle": "StonkChris", "lifecycle": "MONITORED", "root": False, "never_auto_prune": False, "has_corpus": True, "reason": "corpus exists; refreshed this session"},
       {"handle": "citrini", "lifecycle": "MONITORED", "root": False, "never_auto_prune": False, "has_corpus": True, "reason": "corpus exists; refreshed this session"},
       {"handle": "closenberger", "lifecycle": "MONITORED", "root": False, "never_auto_prune": False, "has_corpus": True, "reason": "corpus exists; refreshed this session"},
       {"handle": "italianinvesto", "lifecycle": "MONITORED", "root": False, "never_auto_prune": False, "has_corpus": True, "reason": "corpus exists; refreshed this session"},
       {"handle": "zephyr_z9", "lifecycle": "MONITORED", "root": False, "never_auto_prune": False, "has_corpus": True, "reason": "corpus exists; refreshed this session"},
   ]
   ```

   Comparaison insensible a la casse sur `handle` pour la correspondance, stricte (==) sur les
   cinq autres champs.
6. **Racine owner intacte** : deja couvert par le controle 5 (`Frenchie` fait partie du
   snapshot fige) ; conserve ici comme alias explicite `ROOT_ACCOUNT_ALTERED` quand la divergence
   detectee au controle 5 concerne specifiquement `Frenchie.root` ou `Frenchie.never_auto_prune`.
7. **Frenchie_ jamais promu** : si un compte `handle` (insensible a la casse) `== "frenchie_"`
   apparait dans `accounts` (et pas seulement dans `excluded_candidates`) → rc=1
   `IMPERSONATION_RISK_HANDLE_PROMOTED:Frenchie_`.
8. **Zero doublon** : `handle` normalises en minuscules, uniques → sinon rc=1
   `DUPLICATE_HANDLE:<handle>`.
9. **Liaison de source, pour chaque compte AJOUTE (absent des 14 de reference)** :
   a. `source` est un dict avec `file` (str, EXACTEMENT
      `"research/tr_kol_ranking.json#discovery_from_qualified_kols.candidates"` ou
      `"research/X109_DISCOVERY_CANDIDATES.json"` — toute autre valeur → rc=2
      `SOURCE_PATH_NOT_ALLOWLISTED:<handle>`), `commit` (40 hex), `dated` (ISO8601 parseable).
   b. `git cat-file -e <commit>^{commit}` dans `--project-repo` reussit → sinon rc=2
      `SOURCE_COMMIT_UNKNOWN:<handle>`.
   c. Le blob est charge par **`git show <commit>:<path>`** (jamais le fichier vivant sur disque).
   d. Le JSON de ce blob est parse ; la liste `candidates` est extraite ; `handle` doit
      apparaitre, **comparaison insensible a la casse**, dans cette liste au commit exact cite →
      sinon rc=2 `HANDLE_NOT_IN_SOURCE_AT_COMMIT:<handle>`.
   e. Le sha256 du blob charge est calcule et conserve pour le receipt.
10. **Aucune exception MONITORED (round 3)** : pour tout compte AJOUTE (handle absent du
    snapshot fige du controle 5), `lifecycle` doit etre EXACTEMENT `"SAMPLED"`. Un compte ajoute
    avec `lifecycle == "MONITORED"` (ou toute autre valeur hors `{SAMPLED}`) fait echouer le
    verificateur en rc=1 `NEW_ACCOUNT_MUST_BE_SAMPLED:<handle>` -- sans lire `justification`, sans
    chercher de fichier de preuve. Il n'existe plus de voie d'exception dans ce verificateur.
11. **Aucune pretention de qualification** : recherche de sous-chaines interdites (pretendant un
    changement de `qualified_count` ou du verdict `INSUFFICIENT_QUALIFIED_ACCOUNTS`) dans toutes
    les valeurs string du JSON → sinon rc=1 `QUALIFICATION_CLAIM_FORBIDDEN`.
12. Si tout passe → rc=0, ecrire (atomique, tmp+`os.replace`) un recu
    `factory_v2/receipts/TR_UNIVERSE_EXPANSION_V1_RECEIPT.json` (relatif au depot produit,
    `schema: "tr-universe-expansion/v1"`) contenant `integration_sha` (HEAD du `--project-repo`),
    le nombre total de comptes, la liste des handles ajoutes avec pour chacun `source.commit`,
    `source.file`, et le sha256 du blob source verifie (pas du fichier univers).

## Cas de test obligatoires (`tests/test_verify_tr_universe_expansion.py`, dans le depot produit,
sans reseau ni modele)
1. Fichier absent → rc=2.
2. JSON corrompu → rc=2.
3. `schema` faux (`tr-kol-universe/v1`) → rc=2.
4. Fichier valide mais non committe → rc=2.
5. 20 comptes (sous 25) → rc=1 `ACCOUNT_COUNT_OUT_OF_RANGE`.
6. 32 comptes (au-dessus de 30) → rc=1 `ACCOUNT_COUNT_OUT_OF_RANGE`.
7. 27 comptes, un `category` valant `"UNVERIFIED_SPECIALTY"` → rc=1
   `ACCOUNT_CATEGORY_PLACEHOLDER_REJECTED`.
8. 27 comptes, un des 14 handles historiques absent → rc=1 `EXISTING_HANDLE_DROPPED`.
8b. 27 comptes, les 14 handles presents mais un `reason` d'un des 14 modifie d'un seul mot →
    rc=1 `EXISTING_ACCOUNT_FIELD_DIVERGED:<handle>:reason`.
8c. 27 comptes, `has_corpus` d'un des 13 non-root passe a `false` → rc=1
    `EXISTING_ACCOUNT_FIELD_DIVERGED:<handle>:has_corpus`.
9. 27 comptes, `Frenchie` present mais `root: false` → rc=1
   `EXISTING_ACCOUNT_FIELD_DIVERGED:Frenchie:root` (alias `ROOT_ACCOUNT_ALTERED`).
10. 27 comptes dont `Frenchie_` promu en `SAMPLED` → rc=1 `IMPERSONATION_RISK_HANDLE_PROMOTED`.
11. 27 comptes, deux handles identiques en casse differente → rc=1 `DUPLICATE_HANDLE`.
12. 27 comptes, un `source.file` hors des deux chemins autorises → rc=2
    `SOURCE_PATH_NOT_ALLOWLISTED`.
13. 27 comptes, un `source.commit` qui n'existe pas dans l'historique git → rc=2
    `SOURCE_COMMIT_UNKNOWN`.
14. 27 comptes, un `handle` absent de `candidates` au commit cite (fabrique/invente) → rc=2
    `HANDLE_NOT_IN_SOURCE_AT_COMMIT` — cas central du correctif P0-2 (round 1).
15. 27 comptes, un compte AJOUTE avec `lifecycle: MONITORED` (meme avec une `justification`
    tres detaillee citant un fichier de corpus reel) → rc=1 `NEW_ACCOUNT_MUST_BE_SAMPLED` --
    round 3 : cette entree ne beneficie plus d'aucune exception.
16. Une valeur du JSON contient une pretention de resolution de `qualified_count` → rc=1
    `QUALIFICATION_CLAIM_FORBIDDEN`.
17. Cas nominal : 27 comptes, 14 existants intacts, 13 nouveaux en `SAMPLED` avec sources
    verifiables au commit exact, `Frenchie_` documente dans `excluded_candidates` uniquement,
    `Frenchie` root intact, zero doublon → rc=0, receipt ecrit avec les bons champs.

## Non-goals
- Ne construit ni ne modifie `research/tr_kol_universe.json` — c'est le role de
  `TR_UNIVERSE_EXPANSION_V1`, un jalon separe qui depend de celui-ci.
- N'implemente aucune logique de scoring/qualification KOL (deja fait par
  `TR_KOL_DISCOVERY_AND_RANKING_V1`).
- N'importe rien depuis le paquet `factory_v2` du depot de controle : ce module doit continuer a
  fonctionner meme si le depot de controle n'est pas accessible.
