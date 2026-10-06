# TR_UNIVERSE_EXPANSION_V1 — elargir la couverture SAMPLED de 14 a 25-30 comptes (niveau B) — v4

Corrige apres ACCEPT_WITH_FIXES round 3 Codex (`CODEX_COUNTER_AUDIT_TR_UNIVERSE_3.md`) : (1)
plus aucune exception MONITORED pour un compte ajoute -- tout ajout est SAMPLED, sans exception ;
(2) le verificateur embarque un snapshot fige des 14 comptes actuels avec TOUS leurs champs
(handle, lifecycle, root, never_auto_prune, has_corpus, reason) et rejette rc=1 toute divergence.
Ce jalon depend de `TR_UNIVERSE_VERIFIER_V1` (COMPLETE) qui livre ce fichier, entierement dans le
depot produit, sans PYTHONPATH/FACTORY_CONTROL_REPO. Sans lui, `product_e2e.test_command` echoue
reellement (script absent).

## Outcome
OUTCOME : `research/tr_kol_universe.json` (dans le depot produit
`/Users/jocelyngrosjean/conductor/workspaces/twitter-radar/twitter-radar-foundation-v2`) contient
entre 25 et 30 comptes au total, schema `tr-kol-universe/v2`. Les 14 comptes actuels restent
`lifecycle: MONITORED` sans changement. Tout compte ajoute entre en `lifecycle: SAMPLED`, sans aucune exception (SPEC.md:274-275 :
`DISCOVERED -> SAMPLED -> MONITORED -> QUALIFIED | DEMOTED | REJECTED`) ; le verificateur rejette
en rc=1 `NEW_ACCOUNT_MUST_BE_SAMPLED` tout nouveau compte `MONITORED`, quelle que soit la
justification fournie. Ce jalon ne pretend pas et ne peut pas
lever `INSUFFICIENT_QUALIFIED_ACCOUNTS`** (SPEC.md:300-301 : la qualification SOLID/ELITE est un
verdict distinct, fonde sur >=5 calls LONG et >=4 mois de corpus reel, jamais sur un rang
d'engagement entrant).

## Depends On
- Jalon `TR_UNIVERSE_VERIFIER_V1` COMPLETE : `factory_v2/verify_tr_universe_expansion.py` existe
  DANS LE DEPOT PRODUIT (pas le depot de controle), est autonome (stdlib seulement), et est le
  seul juge de l'acceptance de ce jalon via
  `python3 factory_v2/verify_tr_universe_expansion.py research/tr_kol_universe.json --project-repo .`
  (cwd = depot produit, aucune variable d'environnement requise).
- Aucun acces reseau garanti pendant ce build : ne PAS appeler l'API X, ne PAS inventer de handle.

## Sources autorisees (deja committees, a lire telles quelles — EXACTEMENT les deux memes que
`TR_UNIVERSE_VERIFIER_V1` autorise ; toute autre source sera rejetee rc=2 par le verificateur)
1. `research/tr_kol_ranking.json` -> cle `discovery_from_qualified_kols.candidates` : **50**
   candidats classes par engagement entrant (`reply`/`quote`/`retweet`) depuis les 5 KOL
   qualifies (`qualified_sources`: BryzonX, CKCapitalxx, Frenchie, PhotonBull, RJCcapital).
   Fichier committe au commit `e679f9567b09b4d8f5023cfc798d107d1e3b7fb3`
   (2026-09-11T17:32:16+02:00, depot produit) — utiliser cette reference comme `source` pour
   tout compte issu de cette liste.
   - **`Frenchie_` (candidat #1, 300 replies) n'est JAMAIS ajoute comme nouveau compte** :
     `@Frenchie_` est le handle X du compte racine lui-meme (identite produit / corpus_key
     `Frenchie`, root:true ; confirme par le proprietaire le 2026-09-12, capture du profil X :
     nom affiche « Frenchie », verifie, 51,6K abonnes). Il apparait dans la liste par
     auto-reference du graphe ; l'ajouter creerait un doublon de la racine. Le documenter dans
     `excluded_candidates` avec le motif « duplicate of root handle » (gabarit ci-dessous).
   - Candidats eligibles restants apres exclusion de `Frenchie_` : 49.
2. `research/X109_DISCOVERY_CANDIDATES.json` (32 candidats, committe au commit
   `65d6d622bd9e2507b7be07e9003469539b38a2cc`, 2026-09-07T15:46:36+02:00) — source secondaire si
   la source 1 ne suffit pas a atteindre 25 comptes au total.
3. `research/tr_kol_universe.json` actuel (14 comptes MONITORED, dont `Frenchie` root) — a
   conserver integralement, ne jamais retirer ni renommer un handle existant.
4. `recovery/CANONICAL_ACCOUNT_UNIVERSE.csv` — a consulter uniquement pour verifier qu'un handle
   candidat n'est pas deja marque comme compte operateur owner opaque (`@secr3t_xyz`,
   `@NFTboosters`, `@josslignioo`, `@p1studio_xyz` — jamais eligibles a l'univers KOL).

Le verificateur charge `source.file` au `source.commit` exact (`git show <commit>:<path>`,
jamais le fichier vivant) et rejette (rc=2) tout handle absent de la liste `candidates` de ce
blob exact. Ne cite donc jamais un `source.commit` que tu n'as pas toi-meme verifie contenir ce
handle a cet endroit precis.

## Deliverables
- `research/tr_kol_universe.json` mis a jour (schema `tr-kol-universe/v2`), seul fichier livrable.

## Piege ferme le 2026-09-12 (commit produit 193a0dc) -- lis ceci avant de lancer le verificateur
Deux lancements precedents ont echoue en ecrivant l'ANCIEN schema `tr-universe-expansion/v1` :
le verificateur relisait alors la baseline a `HEAD:research/tr_kol_universe.json`, donc committer
le candidat a ce meme chemin cassait la baseline (BASELINE_UNREADABLE). Cette voie est supprimee :
- seul `tr-kol-universe/v2` est accepte (`UNIVERSE_SCHEMA_INVALID` sinon) ; la baseline est une
  constante en dur (14 comptes), committer le candidat ne la change pas ;
- le fichier juge doit etre IDENTIQUE au blob HEAD : COMMITTE-LE AVANT d'executer la commande
  d'acceptance (`CANDIDATE_NOT_COMMITTED_AT_HEAD` sinon). Sequence : ecrire -> `git add` ->
  `git commit` -> `python3 factory_v2/verify_tr_universe_expansion.py research/tr_kol_universe.json --project-repo .`
- ne touche ni au verificateur ni aux tests : si le verificateur refuse, c'est ton fichier qui a tort.

Schema attendu par compte :
```json
{
  "handle": "<handle exact, casse d'origine>",
  "lifecycle": "SAMPLED",
  "category": "<valeur factuelle et verifiable, ex. 'DISCOVERY_CANDIDATE — engagement entrant depuis KOL qualifies'; jamais 'UNVERIFIED_SPECIALTY' ni equivalent invente>",
  "justification": "<phrase factuelle citant la metrique source, ex. '88 replies entrants depuis les KOL qualifies, classe #2 de discovery_from_qualified_kols'>",
  "source": {
    "file": "research/tr_kol_ranking.json#discovery_from_qualified_kols.candidates",
    "commit": "e679f9567b09b4d8f5023cfc798d107d1e3b7fb3",
    "dated": "2026-09-11T17:32:16+02:00"
  },
  "root": false,
  "never_auto_prune": false,
  "has_corpus": false
}
```
- Pour les 14 comptes deja presents : conserver `handle`, `lifecycle: MONITORED`, `root`,
  `never_auto_prune`, `has_corpus`, `reason` **strictement inchanges, champ par champ** ; ajouter
  uniquement les champs manquants (`category`, `justification`, `source`) sans jamais toucher aux
  six champs ci-dessus. Le verificateur compare ces 14 comptes a un snapshot fige EN DUR dans son
  code (pas relu depuis le fichier) et rejette rc=1 toute divergence, meme minime. `Frenchie`
  reste `root: true` / `never_auto_prune: true`.

Snapshot fige de reference (les 14 comptes actuels, tels que lus dans
`research/tr_kol_universe.json` au moment de cet amendement — le verificateur embarque
exactement ces valeurs) :

| handle | lifecycle | root | never_auto_prune | has_corpus | reason |
|---|---|---|---|---|---|
| BryzonX | MONITORED | false | false | true | corpus exists; refreshed this session |
| CKCapitalxx | MONITORED | false | false | true | corpus exists; refreshed this session |
| DeepValueBagger | MONITORED | false | false | true | corpus exists; refreshed this session |
| Frenchie | MONITORED | true | true | true | corpus exists; refreshed this session |
| Gubloinvestor | MONITORED | false | false | true | corpus exists; refreshed this session |
| HyperTechInvest | MONITORED | false | false | true | corpus exists; refreshed this session |
| LiebermanAustin | MONITORED | false | false | true | corpus exists; refreshed this session |
| PhotonBull | MONITORED | false | false | true | corpus exists; refreshed this session |
| RJCcapital | MONITORED | false | false | true | corpus exists; refreshed this session |
| StonkChris | MONITORED | false | false | true | corpus exists; refreshed this session |
| citrini | MONITORED | false | false | true | corpus exists; refreshed this session |
| closenberger | MONITORED | false | false | true | corpus exists; refreshed this session |
| italianinvesto | MONITORED | false | false | true | corpus exists; refreshed this session |
| zephyr_z9 | MONITORED | false | false | true | corpus exists; refreshed this session |
- Pour les comptes nouvellement ajoutes : `lifecycle: "SAMPLED"` **sans aucune exception**. Round
  3 Codex a supprime la voie d'exception vers `MONITORED` (elle n'etait pas verifiee au commit ni
  liee au handle) : tout compte ajoute avec `lifecycle: "MONITORED"` fait echouer le verificateur
  en rc=1, quelle que soit la preuve citee dans `justification`.
- Ajouter au niveau racine du JSON :
  - `generated_at` (ISO8601 UTC, horodatage reel du build)
  - `"expansion_batch": "TR_UNIVERSE_EXPANSION_V1"`
  - `"excluded_candidates": [{"handle": "Frenchie_", "reason": "duplicate of root handle: @Frenchie_ is the X handle of the root account (product identity Frenchie), owner-confirmed 2026-09-12", "source": {"file": "research/tr_kol_ranking.json#discovery_from_qualified_kols.candidates", "commit": "e679f9567b09b4d8f5023cfc798d107d1e3b7fb3", "dated": "2026-09-11T17:32:16+02:00"}}]`
  - `"discovery_bias_note"`: texte factuel indiquant que tous les candidats proviennent
    uniquement du graphe reply/quote/retweet des 5 comptes deja qualifies (BryzonX, CKCapitalxx,
    Frenchie, PhotonBull, RJCcapital), jamais d'une decouverte independante — ce biais de graphe
    signifie que des KOL de qualite non connectes a ces 5 comptes resteront invisibles a cette
    methode.

## Non-goals
- Ne pas lancer de scraping ni d'appel API X — ce jalon ne collecte aucun nouveau post.
- Ne pas modifier `research/tr_kol_table.json`, `research/tr_kol_ranking.json`,
  `research/tr_discovery_graph.json` ni tout autre fichier de scoring/ranking.
- Ne pas depasser 30 comptes au total.
- Ne jamais ajouter un compte avec `lifecycle: MONITORED` -- round 3 : aucune exception, tout
  ajout est `SAMPLED`, le verificateur rejette rc=1 sans regarder de preuve de corpus.
- Ne jamais ecrire "UNVERIFIED_SPECIALTY" ni formulation equivalente ("a confirmer", "TBD", "TODO")
  dans un champ du fichier livre — toute incertitude editoriale reste hors fichier, dans la revue
  proprietaire (voir Verification).
- Ne pas ajouter `Frenchie_` (c'est la racine).
- Ne pas toucher a ROADMAP.yaml, SPEC.md, STRATEGY.md, PROJECT_STATE.json (autorite canonique,
  hors perimetre builder — voir `OWNER_DIRECTIVE_CANONICAL_AUTHORITY_IMMUTABLE_2026-09-10.md:18-20`).
- Ne pas inventer, deviner ou "completer" un handle absent des quatre sources autorisees.
- Ne jamais affirmer ou laisser entendre dans un champ du fichier que ce lot resout
  `INSUFFICIENT_QUALIFIED_ACCOUNTS` ou change `qualified_count`.
- Ne modifier aucun des six champs figes (`handle`, `lifecycle`, `root`, `never_auto_prune`,
  `has_corpus`, `reason`) des 14 comptes existants -- le verificateur compare a un snapshot
  fige en dur et rejette rc=1 toute divergence.
- Ne pas toucher a `factory_v2/verify_tr_universe_expansion.py` (livrable du jalon
  `TR_UNIVERSE_VERIFIER_V1`, deja COMPLETE quand ce jalon-ci s'execute).

## Verification
- `python3 factory_v2/verify_tr_universe_expansion.py research/tr_kol_universe.json --project-repo .`
  (execute depuis la racine du depot produit, sans PYTHONPATH ni variable d'environnement) doit
  sortir rc=0 (contrat complet dans `VERIFIER_tr_universe_expansion.spec_v4.md`).
- `python3 -m pytest tests/ -q -k 'universe or kol'` doit rester vert, EN EXCLUANT les tests qui ouvrent un
  port reseau (impossibles dans le sandbox du builder, sans rapport avec ce livrable) :
  `python3 -m pytest tests/ -q -k '(universe or kol) and not web'` — 4 tests `*web*` lient 127.0.0.1 et
  echouent en PermissionError dans le sandbox ; ce n'est PAS une raison de declarer PARTIAL.
- OUTCOME : `IMPLEMENTED` des que le verificateur (commande d'acceptance) sort rc=0 et que la suite ci-dessus
  (hors web) est verte dans ta session. Le fait que le fichier ait deja ete committe par une tentative
  precedente de cette mission ne change rien : verifie-le, complete-le si besoin, declare IMPLEMENTED.
- Revue humaine attendue par le proprietaire avant tout usage produit : valider les
  `category`/`justification` des comptes `SAMPLED`, decider au cas par cas d'une promotion
  ulterieure a `MONITORED`.
