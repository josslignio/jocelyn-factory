# TR_UNIVERSE_VERIFIER_CONFORMANCE_V1 — task file — conformance gate for `factory_v2/verify_tr_universe_expansion.py`

Corrige un defaut de gate : TR_UNIVERSE_VERIFIER_V1 est COMPLETE (commit produit `80989bbb`) mais
son acceptance etait auto-referente — `product_e2e.test_command` etait le propre pytest du
builder (`tests/test_verify_tr_universe_expansion.py`), qui ne peut par construction detecter
que le comportement code ne correspond pas a la spec v4
(`VERIFIER_tr_universe_expansion.spec_v4.md`, controles 1-12, cas 1-17). Ce jalon ajoute une
suite de conformite ECRITE INDEPENDAMMENT du builder d'origine, qui prouve ou refute la
conformite reelle, et impose la correction du verificateur pour la faire passer.

## Outcome
`tests/test_verify_tr_universe_expansion_conformance.py` existe dans le depot produit
(twitter-radar-foundation-v2), est ecrit sans reutiliser le code de test existant du builder
(depots git temporaires crees dans `tmp_path`, memes techniques que
`tests/test_verify_tr_universe_expansion.py` mais assertions independantes), encode les 17 cas
de test de la spec v4 et les 6 ecarts constates dans le livrable initial (voir tableau), et
`python3 -m pytest tests/test_verify_tr_universe_expansion_conformance.py -q` passe integralement apres correction de
`factory_v2/verify_tr_universe_expansion.py`.

## Deliverables
- `tests/test_verify_tr_universe_expansion_conformance.py` (seul livrable declare)

Non declare comme livrable mais obligation reelle prouvee par `product_e2e.test_command` :
- `factory_v2/verify_tr_universe_expansion.py` corrige pour se conformer a la spec v4 (le fichier
  existe deja, livre par TR_UNIVERSE_VERIFIER_V1 ; ce jalon-ci ne le declare pas comme deliverable
  mais sa correction est requise pour que la commande de test passe).

## Depends On
- Jalon `TR_UNIVERSE_VERIFIER_V1` COMPLETE (le fichier a corriger existe).
- Moteur : build de niveau B, ENTIEREMENT dans le depot produit twitter-radar
  (`executor.cwd: product_repo`).

## Ecarts confirmes entre le livrable initial et la spec v4 (fichier:ligne, depot produit)

| # | Ecart | Constat dans le code livre | Exigence spec v4 |
|---|---|---|---|
| 1 | Schema | `verify_tr_universe_expansion.py:46` `SCHEMA = "tr-universe-expansion/v1"` ; controle applique en `verify_tr_universe_expansion.py:99-101` sur le fichier candidat | `TR_UNIVERSE_VERIFIER_V1.md` controle 1 : `schema == "tr-kol-universe/v2"` |
| 2 | Snapshot fige | `verify_tr_universe_expansion.py:115-133` (`_load_baseline`) relit `git show HEAD:research/tr_kol_universe.json` du `--project-repo` a chaque execution — au SHA candidat (le meme repo, HEAD courant), le fichier est compare a lui-meme : vacuite si le candidat EST deja committe a HEAD | `TR_UNIVERSE_VERIFIER_V1.md` controle 5 : constante Python `FROZEN_EXISTING_ACCOUNTS` embarquee en dur, jamais relue depuis un fichier |
| 3 | Provenance | `verify_tr_universe_expansion.py:157-164` lit `addition.get("source_commit")` / `addition.get("source_path")`, deux champs a plat, sans allowlist de chemins | `TR_UNIVERSE_VERIFIER_V1.md` controle 9a : objet `source {file, commit, dated}`, `file` limite aux deux valeurs exactes `research/tr_kol_ranking.json#discovery_from_qualified_kols.candidates` et `research/X109_DISCOVERY_CANDIDATES.json` |
| 4 | Parsing du blob source | `verify_tr_universe_expansion.py:181-185` : `if handle.strip().lower() not in blob.lower()` — sous-chaine brute sur le texte integral du blob | `TR_UNIVERSE_VERIFIER_V1.md` controle 9d : parser le JSON du blob, extraire la liste `candidates` (ou `discovery_from_qualified_kols.candidates`), verifier que `handle` y figure comme entree structuree |
| 5 | `category`/`justification` | `_validate_account` (`verify_tr_universe_expansion.py:64-77`) ne lit que `handle`, `lifecycle`, `root`, `never_auto_prune`, `has_corpus` — aucune lecture ni validation de `category`/`justification` | `TR_UNIVERSE_VERIFIER_V1.md` controle 4 : `category` et `justification` obligatoires, `category` jamais `"UNVERIFIED_SPECIALTY"` ni equivalent (`"a confirmer"`/`"TBD"`/`"TODO"`) |
| 6 | Chemin/schema du recu | `verify_tr_universe_expansion.py:50` `RECEIPT_OUT_REL = factory_v2/receipts/TR_UNIVERSE_EXPANSION_RECEIPT.json`, schema `verify_tr_universe_expansion.py:48` `tr-universe-expansion-receipt/v1` | `TR_UNIVERSE_VERIFIER_V1.md` controle 12 : `factory_v2/receipts/TR_UNIVERSE_EXPANSION_V1_RECEIPT.json`, schema `tr-universe-expansion/v1` |

## Cas de test de la spec v4 (`TR_UNIVERSE_VERIFIER_V1.md`, section "Cas de test obligatoires") —
statut attendu du livrable INITIAL (avant correction), a encoder dans la suite de conformite

Ces predictions sont analytiques (deduites de la lecture du code livre, non executees dans ce
document) — le test de conformite est ce qui les rend verifiables mecaniquement. Le fichier livre
implemente un sous-ensemble des controles de la spec (proche d'un round anterieur) ; la plupart
des cas positifs qui utilisent le schema/format reels de la spec v4 echouent aujourd'hui parce
que le controle de schema (ecart 1) rejette tout candidat conforme a la spec avant meme
d'atteindre les autres controles.

| # | Cas | Attendu par la spec v4 | Etat predit sur le livrable initial |
|---|---|---|---|
| 1 | Fichier absent | rc=2 | VERT (deja implemente, independant du schema) |
| 2 | JSON corrompu | rc=2 | VERT (deja implemente) |
| 3 | `schema` faux (`tr-kol-universe/v1`) | rc=2 `UNIVERSE_SCHEMA_INVALID` | VERT par accident (toute valeur != `tr-universe-expansion/v1` est rejetee) |
| 4 | Fichier valide mais non committe | rc=2 | ROUGE probable — aucun controle "git-tracked a HEAD" du fichier candidat dans le code lu (seul le fichier baseline est lu via git ; le fichier candidat est lu par `Path.read_text`, jamais verifie comme tracked) |
| 5 | 20 comptes (sous 25) | rc=1 `ACCOUNT_COUNT_OUT_OF_RANGE` | ROUGE — aucune borne de cardinalite 25-30 dans le code lu |
| 6 | 32 comptes (au-dessus de 30) | rc=1 `ACCOUNT_COUNT_OUT_OF_RANGE` | ROUGE — idem |
| 7 | `category` = `"UNVERIFIED_SPECIALTY"` | rc=1 `ACCOUNT_CATEGORY_PLACEHOLDER_REJECTED` | ROUGE — `category` non lu (ecart 5) |
| 8 | Un des 14 handles historiques absent | rc=1 `EXISTING_HANDLE_DROPPED` | ROUGE (schema, ecart 1) bloque avant d'atteindre ce controle pour un candidat spec-conforme ; PARTIEL si on isole le controle de continuite (`BASELINE_ACCOUNT_MISSING` existe mais compare a HEAD live, ecart 2) |
| 8b | `reason` d'un des 14 modifie | rc=1 `EXISTING_ACCOUNT_FIELD_DIVERGED:...:reason` | ROUGE — `reason` n'est pas compare (`_account_identical`, `verify_tr_universe_expansion.py:136-138`, ne compare que `lifecycle` + les 3 booleens) |
| 8c | `has_corpus` d'un non-root passe a `false` | rc=1 `EXISTING_ACCOUNT_FIELD_DIVERGED:...:has_corpus` | VERT partiel possible (has_corpus est compare) mais sous reserve de l'ecart 1/2 |
| 9 | `Frenchie` present avec `root: false` | rc=1 `EXISTING_ACCOUNT_FIELD_DIVERGED:Frenchie:root` | VERT partiel (root est compare) sous reserve ecart 1/2 |
| 10 | `Frenchie_` promu en `SAMPLED` | rc=1 `IMPERSONATION_RISK_HANDLE_PROMOTED` | ROUGE — aucun controle special du handle `Frenchie_` dans le code lu |
| 11 | Deux handles identiques en casse differente | rc=1 `DUPLICATE_HANDLE` | VERT (deja implemente, `verify_tr_universe_expansion.py:108-111`) |
| 12 | `source.file` hors allowlist | rc=2 `SOURCE_PATH_NOT_ALLOWLISTED` | ROUGE — pas d'allowlist, pas de champ `source.file` (ecart 3) |
| 13 | `source.commit` inexistant | rc=2 `SOURCE_COMMIT_UNKNOWN`/`SOURCE_COMMIT_NOT_FOUND` | VERT partiel (verifie `source_commit`, nom d'erreur different mais rc=2 correct) sous reserve ecart 3 (nom de champ) |
| 14 | Handle absent de `candidates` au commit cite | rc=2 `HANDLE_NOT_IN_SOURCE_AT_COMMIT` | ROUGE de fond — verifie une sous-chaine brute (ecart 4), pas la structure `candidates`, donc accepte a tort un handle cite dans un texte libre du meme blob qui ne fait pas partie de la liste |
| 15 | Ajout `MONITORED` avec justification detaillee | rc=1 `NEW_ACCOUNT_MUST_BE_SAMPLED` | VERT (deja implemente, `verify_tr_universe_expansion.py:244-247`, ignore bien `justification`) |
| 16 | Pretention de resolution `qualified_count` | rc=1 `QUALIFICATION_CLAIM_FORBIDDEN` | ROUGE — aucun scan de sous-chaines interdites dans le code lu |
| 17 | Cas nominal (27 comptes conformes) | rc=0, receipt correct | ROUGE — le schema du cas nominal spec v4 (`tr-kol-universe/v2`, `source{file,commit,dated}`, `category`/`justification`) est rejete par le livrable initial (ecarts 1, 3, 5) avant meme d'atteindre l'ecriture du receipt (ecart 6) |

## Non-goals
- Ne modifie pas `research/tr_kol_universe.json` (role de `TR_UNIVERSE_EXPANSION_V1`, qui depend
  de ce jalon-ci une fois COMPLETE).
- Ne reecrit pas `tests/test_verify_tr_universe_expansion.py` du builder d'origine — celui-ci doit
  rester vert (`product_e2e.test_command` execute les deux fichiers de test ensemble).
- Ne relaxe aucun controle fail-closed existant (rc=2 reste rc=2 partout ou le livrable initial
  est deja correct — colonnes VERT du tableau ci-dessus).
- N'importe rien depuis le paquet `factory_v2` du depot de controle (meme contrainte
  d'autonomie que le verificateur qu'il teste).

## Verification
- `python3 -m pytest tests/test_verify_tr_universe_expansion_conformance.py -q` (execute depuis la racine du depot produit) doit
  passer integralement — c'est `product_e2e.test_command` du jalon.
- Revue humaine attendue par le proprietaire : confirmer que la suite de conformite n'a pas ete
  ecrite en reutilisant les fixtures/assertions du test du builder d'origine (independance reelle,
  pas seulement nominale).


## Contrôle de régression séparé (non bloquant pour ce jalon)

`python3 -m pytest tests/test_verify_tr_universe_expansion.py -q` — la suite du builder initial doit rester verte ou être corrigée dans le même candidat ; elle n'est pas le juge de ce jalon (contre-audit Codex : le juge doit être indépendant du builder d'origine).
