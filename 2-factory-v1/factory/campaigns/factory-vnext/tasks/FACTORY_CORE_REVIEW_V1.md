# FACTORY_CORE_REVIEW_V1 — relecture ligne à ligne des cœurs, puis retrait de ce qui boucle

## Objectif
Les trois cœurs ont été réécrits (dispatcher_v3.py, factoryd.py, runtime.py + factory_v2/advance/*, authority_gate.py,
settlement_truth.py, worktree_manager.py ≈ 10 400 lignes) et jugés par la suite, le banc de chaos et le benchmark —
mais **jamais relus ligne à ligne par un moteur indépendant**. Le 22/09, cinq boucles réelles ont coûté 17 interventions
humaines : porte rejouant le même vieux SHA toutes les 20 min, sonde HEAD répondant « non » sans écrire de preuve, candidat
juge-vert jeté à la rotation de moteur, quarantaine incapable de sortir seule (4 levées manuelles), diagnostic de
quarantaine lancé (4 × Codex Astra) sans droit de modifier quoi que ce soit. Le propriétaire : « on n'a pas besoin de tant
de code maison » — cette relecture doit **retirer** du code, pas en ajouter.

## La question centrale, module par module (ordre propriétaire 22/09 15:50)
Pour CHAQUE module relu : « quel projet gratuit, prouvé, déjà construit fait déjà ça ? » — répondre dans le constat
`HOMEMADE_REPLACEABLE` avec `replaceable_by: {project, url, what_it_replaces, loc_removed_estimate, effort_days, risk}`.
Candidats à confirmer ou infirmer sur notre code réel (recherche FACTORY_SCALE_LOCAL_2026-09) : Temporal (reprise
durable, quarantaine, re-liaison), Harbor (format des cas, validation en conteneur propre), harness.lol (flux moteur
NDJSON unifié), Omnigent (worktrees + relecteur d'un autre fournisseur), llama.cpp server (slots locaux). Un module sans
réponse (« rien n'existe » doit être argumenté) = constat refusé. Livrable : `BRICKS.md`, tableau brique / lignes
retirées / effort / risque / avant ou après LONG_RUN_PROOF_2 — base de la décision propriétaire du mercredi 24/09.

**Partir de l'existant, pas de zéro** : `factory_v2/audit/FACTORY_VS_UPSTREAM_MATRIX.json` (07/09) a déjà décidé ADAPT
pour mini-swe-agent (boucle d'exécution, `agents/default.py` 160 lignes), SWE-ReX (actions typées), Temporal (reprise) et
Spec Kit ; `FACTORY_VENDORED_CODE.md` (26/07) a déjà le code SWE-bench prêt à coller. Ces décisions n'ont jamais été
appliquées. Pour chacune, `BRICKS.md` dit : code upstream (repo, chemin, SHA, licence) → fichiers maison supprimés →
lignes retirées. Une brique « à adapter plus tard » sans ce triplet = constat refusé.

## Consigne centrale (propriétaire, 22/09 17:10)
Examiner TOUT le code de contrôle au SHA fixé, suivre ses chemins d'exécution réels et comparer chaque responsabilité aux
implémentations publiques pertinentes. Pour chaque remplacement retenu : code upstream exact (version épinglée, fonctions,
tests, limites connues, licence, maintenance, dépendances), garanties conservées démontrées, branchement dans le challenger,
code maison devenu inutile supprimé. Aucun remplacement n'est terminé sur la seule base d'un document ou d'un import.
Cinq résultats exigés :
1. **Couverture complète** — chaque fichier et chaque fonction classés : utilisé / mort / dupliqué / remplaçable /
   spécifique à conserver. « Ligne par ligne » = aucune zone oubliée ; décisions par responsabilité cohérente.
2. **Comparaison avec le vrai code des autres** — pas la popularité : ce que le composant couvre du besoin, et ce qu'il laisse.
3. **Architecture cible unique** — qui possède l'état ? qui relance ? qui valide ? qui règle ? Un propriétaire par
   responsabilité ; six bibliothèques qui se recouvrent = le problème reproduit.
4. **Preuve de câblage** — une vraie mission suivie du jalon au règlement et à la reprise après panne : le composant est
   appelé, son échec bloque au bon endroit, l'ancien chemin n'est plus actif. Des tests unitaires verts ne suffisent pas.
5. **Bilan de suppression réel** — fichiers supprimés, adaptation restante, dépendances, coût d'exploitation ; un
   remplacement qui exige presque autant de raccordement que l'ancien système est rejeté.
Conclusion autorisée et attendue : « cette fonction n'a plus besoin d'exister ».
Contre-exemple à ne pas reproduire : `FACTORY_VS_UPSTREAM_MATRIX.json` classe Temporal ADAPT mais garde l'état maison,
ajoute un drapeau de replay et recrée une RetryPolicy maison sans dépendance — des idées empruntées, aucune responsabilité
transférée. Par ligne de BRICKS.md : garantie requise → composant upstream épinglé → responsabilité maison restante →
test de panne → fichiers supprimés → coût d'exploitation.
7e boucle du 22/09 (18:20) : un jalon parqué 6 h (3 empreintes d'échec identiques) ignore une AUTORISATION PROPRIÉTAIRE
valide — `_ss1_adopt_or_skip_pass` teste `parked_until` avant que `_cnp_peek` ne consulte l'allocation OWNER, et la seule
sortie est l'expiration. Un grant propriétaire doit purger le parc (sa raison d'être) ; sinon la seule issue est l'édition
d'état à la main, ce qui est interdit.
6e boucle du 22/09 (17:10) : la relecture multi-IA a deux chemins (legacy « un envoi selon la table » / roster « N relecteurs
distincts ») ; le legacy ignore le refus de quota et a lancé GLM à 100 % — un seul chemin doit rester.

## Modalités d'exécution (relecture indépendante du 22/09 17:00)
- Exception au gel bornée : `verify_core_review.py` existe déjà ; publier sa taille finale et son delta réel ; seules les
  corrections d'acceptance listées ci-dessous sont autorisées.
- Contrat fixé avant construction : juge corrigé → relu indépendamment (Codex, lecture seule) → SHA épinglé dans la roadmap
  → puis seulement le builder. Le candidat ne modifie jamais `verify_core_review.py` (vérifié au diff en relecture).
- Rouge avant, vert après : même test dans deux checkouts isolés (SHA de base, SHA candidat) ; l'échec initial doit être
  l'assertion du défaut visé — test absent, erreur d'import ou environnement cassé ne sont pas une preuve.
- Budget et sortie : 2 lancements de relecture (Codex, GLM) + 1 de correction (GLM), 10 800 s par étape, fin mercredi 24/09
  12:00 Bangkok ; les constats alimentent BRICKS.md et le challenger, jamais une campagne de réparation du champion.
- Juge à corriger avant épinglage : loc_before recompté au SHA de base par le juge ; périmètre = tout factory/ + factory_v2/
  (trois totaux publiés : code maison de production, tests/juges/bancs maintenus, upstream + adaptations + services) ;
  BRICKS.md et la spec du noyau minimal exigés ; couverture des modules et invariants à la place d'un quota de constats ;
  fichiers de tests exclus listés avec raison ou réintégrés.

## À construire (trois passes : Codex lecture, GLM lecture, Claude niveau A)
1. **Relectures indépendantes Codex (Sol) puis GLM**, chacune en lecture seule, produisant `factory_v2/audit/CORE_REVIEW_2026-09/<moteur>.json` :
   liste de constats `{file, line, kind, severity, evidence, fix}` avec `kind ∈ {LOOP, SILENT_FAILURE, DEAD_CODE,
   DUPLICATE, RACE, HOMEMADE_REPLACEABLE}` ; chaque constat cite la ligne et le scénario concret d'échec ; aucun constat
   sans scénario. Les 5 boucles du 22/09 doivent y figurer avec leur cause racine (ou être explicitement infirmées).
2. **Correction — liste FERMÉE de 1** (test rouge au SHA de base, par assertion, et vert à HEAD) :
   - `REVIEW_SINGLE_PATH_QUOTA_GUARD` : un seul chemin de relecture multi-IA ; l'ancien (un envoi selon la table, sans
     garde-quota) est supprimé — c'est lui qui a lancé GLM à 100 % le 22/09.
   `ONE_VALIDATION_PER_SHA` est **retirée** de la liste (décision propriétaire 22/09 23:50) : elle contredit le scénario
   `IDLE_NO_PROGRESS` du banc chaos v1, réglé et intouchable (§5b) — base 12/12, candidat 11/12. Elle rejoint les
   **6 autres boucles du 22/09** (sonde HEAD muette, candidat vert déclaré PARTIAL puis jeté, quarantaine sans sortie
   quand la porte est verte à HEAD, diagnostic lancé sans droit d'écriture, parc de 6 h qui ignore une autorisation
   propriétaire, builder qui lance lui-même son relecteur) comme **constat `LOOP` avec cause racine** pour le
   challenger : aucun correctif ici.
   `tests/test_p0h_destructive_f5.py::F5_S4_IndependentValidatorOnly::test_milestone_gate_blocked_if_validator_fails`
   est un **rouge préexistant** reproduit au SHA de base : hors périmètre, listé dans `SUMMARY.json.test_exclusions`
   avec sa raison. Le juge lance pytest avec `-o addopts=""` ; le candidat ne touche jamais `pytest.ini`.
   **Vocabulaire de sortie (cause de l'échec a13, 23/09 00:16)** : `REPORT.json.outcome` doit valoir **`IMPLEMENTED`**
   quand le travail de la passe est fait — c'est la seule valeur que la porte du dispatcher accepte
   (`dispatcher_v3.py:1067`). `DONE`, `OK`, `COMPLETE`, `M1_ADOPTION_COMPLETE` sont refusés
   (`BUILDER_DECLARED_DONE` → non promotable). Une adoption réussie est donc `outcome: IMPLEMENTED`
   avec `completed` décrivant l'adoption.
   **Sorties manquantes, par passe** (état du candidat `eb2a2e17`, vérifié 23/09 00:10) :
   - **M1 (passe 1) : rien à produire** — `codex.json` existe avec 13 constats ; la mission passe par **adoption**
     (`MODEL_CALL_SKIPPED_EXISTING_PRODUCT_VALID`), **aucun lancement Codex**. Si un `codex exec` apparaît sur M1,
     c'est un signal d'arrêt immédiat, pas un essai de plus.
   - **M2 (GLM, lecture seule)** : `glm.json` est `WAITING_INFRA` avec **0 constat** (opencode muet 4 min, erreur
     FSEvents, disque à 300 Mo à 22:03) → relecture GLM **réelle** + `coverage.json` complet. C'est le prochain
     résultat utile.
   - **M3 (Claude, niveau A)** : **retirer du candidat le code ET le test de `ONE_VALIDATION_PER_SHA`** (≈ 11 lignes
     dans `runtime.py` / `advance/`, plus `tests/test_core_review_repairs.py`) — la retirer de la liste ne retire pas
     son implémentation ; `SUMMARY.json` à 1 correctif ; `test_exclusions` (aujourd'hui `{}`) renseignée avec
     `test_p0h_destructive_f5` et sa raison ; `BRICKS.md` et `CORE_SPEC.md` finaux ; juge vert.
   **Codex est réservé** à une contre-relecture finale ciblée sur un candidat déjà vert (points à falsifier :
   `codex.json` final ≡ celui de M1, code `ONE_VALIDATION` bien retiré, `pytest.ini` intact, `loc_control` HEAD < base).
   Budget Codex jusqu'au 26/09 21:53 : **1 lancement**. Tout le reste sur GLM puis Claude.
   **Adopter le travail déjà produit** par l'essai Codex (worktree `FACTORY_CORE_REVIEW_V1#M1` : constats, BRICKS.md,
   CORE_SPEC.md, correction, loc_control 67 208 → 67 203, chaos v2 vert) — ne pas repartir de zéro.
3. **Trois passes, une responsabilité chacune** : Codex (lecture seule) → constats + BRICKS v1 ; GLM (lecture seule) →
   constats indépendants + couverture complète ; Claude (niveau A) → CORE_SPEC.md, BRICKS final, la correction,
   le retrait, SUMMARY.json. `CORE_SPEC.md` contient le **bloc YAML de FACTORY_MINIMAL_CORE_CHALLENGER_V1 prêt à coller**,
   avec un juge exprimé uniquement par des outils existants (aucun nouveau script).
4. **Retrait** (et `BRICKS.md` ci-dessus, livré avec SUMMARY.json) : tout DEAD_CODE / DUPLICATE confirmé est supprimé ; `factory_v2/audit/CORE_REVIEW_2026-09/SUMMARY.json`
   donne LOC avant/après par fichier — la somme des cœurs doit **baisser**.

## Interdits
Ajouter un mécanisme nouveau (garde, juge, banc) ; compresser des lignes pour tricher (garde longueur ≤ 200) ; toucher aux
juges des jalons réglés ; réécrire l'historique ; modifier STRATEGY/SPEC/ROADMAP.

## Juge
`python3 factory_v2/verify_core_review.py --project-repo .` → `CORE_REVIEW_VERIFIED` : 2 fichiers moteur distincts, couverture
complète de factory/ + factory_v2/, 7 boucles du 22/09 constatées ou infirmées, les 2 corrections du §2 avec test nommé,
LOC cœurs après < avant, suite complète + chaos v1/v2 verts. Niveau A, budget 10 800 s.
