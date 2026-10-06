# Jocelyn Factory Law

1. Fail-closed : ambiguïté, preuve absente ou erreur → BLOCK.
2. Exact-SHA : test, QA et review finale doivent viser le même SHA.
3. Builder != reviewer et vendors différents.
4. Aucun faux PASS : reviewer indépendant absent → `PASS_WITH_LIMITATIONS`.
5. `SIMULATED != PROMOTION`.
6. Deux repair loops maximum.
7. Secrets en credential store officiel, jamais prompts, repo ou logs.
8. Un seul writer par worktree ; un seul milestone actif par repo.
9. Paperclip est une projection ; Git + ledger restent reconstructibles.
10. GET-only et zéro coût additionnel sont des contraintes de produit Job Radar.

## LOIS DE LIVRAISON RUNTIME (anti-vent — non négociables, valent pour produit ET factory)

11. Une review qui ne FAIT PAS TOURNER le produit = `INVALID_REVIEW`. Ne valent JAMAIS livraison : `retro_done`, `TECHNICALLY_ACCEPTED` isolé, canary synthétique, ledger vert, spec/plan/evidence, worktree non-mergé.
12. Definition-of-Done FONCTIONNEL, vérifié par machine : un milestone n'est "done" que si `verify_live_product_dod.py` retourne exit 0 — sourcing N offres réelles comptées par source (N > baseline), 1 CV par offre, CV_A ≠ CV_B prouvé (sha256/headline/fit summary/skills/≥2 bullets) pour leurs JD, match score /100 affiché et maximisé par JD, Truth Gate PASS (chaque claim → fact_id, 0 invention), masters CV/cover + UI master hash inchangés, dashboard servi sur URL avec 4 téléchargements fonctionnels.
13. Intégration à CHAQUE étape dans `feat/jr-live` ; jamais laissé dans un worktree par-milestone.
14. Build INCRÉMENTAL : walking skeleton d'abord (passe l'oracle vite), puis capacité par capacité, chacune avec son gate fonctionnel. Pas de monolithe, pas d'échec après des heures.
15. VÉRIFICATION PAR ÉTAPE : à chaque capacité, Codex ET Z/GLM doivent CHACUN, dans leur checkout indépendant, FAIRE TOURNER le produit et exécuter l'oracle. Étape validée et intégrée seulement si les DEUX confirment le PASS fonctionnel. Interdit d'accumuler des capacités puis de tester à la fin.
16. CV : 1 par offre, contenu 100% personnalisé par JD, reformulation agressive des VRAIS faits (0 invention), optimisé pour le max match score ATS/IA + humain afin de décrocher l'entretien. Masters figés (CV navy Arial 831a9215… / cover O1v2 c034573a…). Générateurs legacy interdits (OKX, Times New Roman, `generate_cvs_v2.py`, `document_generator_realistic.py`).
17. Sourcing : élargir RÉELLEMENT vers l'atlas (familles ATS 25-30 + discovery boards + company-universe + import URL + feeds) ; prouver le compte d'offres avant/après par source. GET-only, $0, LinkedIn discovery/user-assisted only.
18. Factory jugée sur le RÉSULTAT via `verify_factory_dod.py` : produit-elle un produit qui MARCHE (intégré + oracle produit PASS) ? Métriques itération vs itération (temps de build, repairs, defects échappés/rattrapés, continues manuels → 0). Gain CHIFFRÉ requis sinon l'amélioration est rejetée. On juge le résultat, pas le diff.
19. SHA : `CODE_COMMIT_SHA` identique builder + reviewers (même build). PAS de SHA de sortie runtime identique exigé (sourcing live non déterministe) ; chaque run prouve la FONCTION via l'oracle ; fixtures snapshotées pour la comparaison déterministe + run live séparé comme preuve.
20. `/goal` branché EXCLUSIVEMENT sur l'exit code des deux oracles (`verify_live_product_dod.py` ET `verify_factory_dod.py`). Aucun `RUN COMPLETE` auto-déclaré sur autre chose.
21. Gates humaines UNIQUEMENT : envoi de vraie PII, envoi de candidature, merge main / push / tag / deploy / scheduler ON. Tout le reste (build, intégration `feat/jr-live`, run live, test) est REQUIS, pas gaté. Chaque run se termine sur un dashboard live (URL) testable par le Boss.
22. ZÉRO VENT : une capacité non buildée ET non fonctionnellement PASS → état `FAIL`/`WAITING` avec la raison exacte de ce qui manque. Jamais de faux "fini".
23. PRÉFLIGHT QUOTA + BUDGET DUR PAR CAPACITÉ (anti-burn) : avant CHAQUE run et CHAQUE capacité, préflight quota des providers requis ($0 strict, aucune clé payante). Chaque capacité a un plafond dur tokens ET temps. Dépassement → STOP immédiat + `WAITING_INFRA` avec l'état exact, JAMAIS de burn silencieux. Un reviewer requis à sec → brain-switch entre abonnements $0 uniquement, sinon `PASS_WITH_LIMITATIONS` (NON promotable). On ne crame jamais du quota sur un build sans borne.
24. WALKING-SKELETON OBLIGATOIRE AVANT TOUT BUILD LOURD : interdiction de lancer une capacité coûteuse tant que la tranche end-to-end minimale n'a pas PASSÉ l'oracle produit. On doit savoir en < 5 min si ça marche avant d'investir du quota. Pas de monolithe, pas d'échec après des heures. Chaque capacité = build → test fonctionnel (oracle) → intégration `feat/jr-live` → retour chiffré → capacité suivante.
25. LIEN PRODUIT CLIQUABLE, PENSÉ DÈS LE DÉBUT DU RUN (non négociable) : chaque run doit garantir, dès sa conception (pas en rattrapage), que le Boss teste le produit live en UN CLIC. Au lancement du dashboard, le run écrit l'URL tokenisée COMPLÈTE (`http://127.0.0.1:<port>/#token=<...>`) dans un fichier connu `<deliverables>/DASHBOARD_URL.txt` — jamais seulement dans le scrollback du terminal. `verify_live_product_dod.py` n'est PASS que si ce fichier existe, contient l'URL complète, et qu'une requête tokenisée renvoie HTTP 200. Interdit d'exiger du Boss qu'il scrolle un terminal ou cherche un jeton. Sécurité : `DASHBOARD_URL.txt` est local-only, jamais commité ni mis dans le ledger/evidence git (le jeton reste hors repo) ; il vit uniquement dans le dossier livrables sur la machine du Boss.

## LOI D'IDENTITÉ DE DÉPÔT (D1 du Boss, 2026-08-07 — non négociable, préflight obligatoire)

16. **Avant tout travail et avant la première écriture**, tout agent exécute
    `python3 factory/bin/preflight_repo_identity.py --repo <dépôt>`. `rc != 0` = **STOP + alerte,
    aucune écriture**. Un dépôt absent du registre `factory/config/repo_identity.json` est
    **REFUSÉ**, pas présumé bon (loi n°1).

    **Pourquoi le commit racine, et rien d'autre.** Le 2026-08-07, un agent a travaillé des heures
    dans un dépôt dont la branche s'appelait `main`, dont `git status` répondait « à jour », et qui
    portait un remote nommé `origin`. Tout mentait : cette branche était un **orphelin d'un seul
    commit**, sans aucun ancêtre commun avec la vraie ligne (55 commits sur le vrai `origin/main`).
    Un dépôt d'archive avait été renommé `origin`, déplaçant l'amont de `main` sans le rattacher.

    Toutes les vérifications habituelles ont échoué **parce qu'elles reposent sur des NOMS** — nom
    de branche, nom de remote, message de `git status` — et que les noms sont exactement ce qui
    avait été échangé. Le commit racine, lui, ne se renomme pas. Il tient même quand les contenus
    sont identiques à l'octet près, ce qui était le cas : les deux racines portent la même empreinte
    d'arborescence `f4ec8737…` et ne diffèrent que par l'étiquette de leur commit.

    Vérifié : le garde-fou se serait déclenché le 2026-08-07 **avant le premier commit**.

## LOIS DE PREUVE (v2, 2026-08-07 — mesurées le jour même, pas déduites)

17. **Aucun « vert » ne se déclare sans la suite ENTIÈRE verte.** Jamais sur ses propres tests
    seuls. Mesuré : un câblage de 6 lignes, annoncé vert après avoir lancé les tests du module
    concerné, cassait en réalité **11 tests** — six modules qui ne compilaient plus et cinq tests
    d'une porte sans rapport. Les tests du module ne pouvaient pas le voir : ils importaient le
    module directement, jamais à travers l'outil câblé.

    Corollaire : comparer à une **baseline connue**, pas à zéro. « 2 échecs » n'est une bonne
    nouvelle que si la baseline en a 2 aussi. Le tag `factory-v1-run8-baseline` sert à ça.

18. **Un test qui n'a pas été prouvé capable d'échouer ne vaut rien — y compris les siens.**
    Test = mutation-prouvé, sinon décoratif. Annuler le correctif doit rendre le test ROUGE ; si
    le test reste vert, il ne teste pas ce qu'il prétend.

    Trois cas mesurés le même jour, **même signature** : on teste ce qui est facile à observer
    plutôt que la propriété qui compte.
    - un plafond de requêtes existant à deux endroits, la mutation n'en retirait qu'un ;
    - une ligne imprimée **avant** le point de plantage, donc présente même quand la commande
      terminait en traceback — la propriété réelle était le **code de retour** ;
    - un chargement par chemin exécuté dans le processus de test, où le fichier de test avait
      lui-même déjà mis le répertoire sur `sys.path` — le test vérifiait une condition **qu'il
      rendait vraie**.

    Ce n'est pas trois incidents, c'est un **défaut de méthode**. La règle de détection tient en
    une question : *si j'annule le correctif, qu'est-ce qui rougit exactement ?* Si la réponse est
    « rien » ou « je ne sais pas », le test est décoratif.

19. **Un accessoire ne casse jamais son socle.** Un instrument de diagnostic — registre d'usage,
    rapport de tombstones, sonde de coût — n'a pas le droit de faire tomber ce qu'il observe.
    Import protégé, dégradation déclarée, jamais d'exception qui remonte. Mesuré deux fois le même
    jour : un coût malformé qui emportait un verdict déjà calculé, et un import dur qui rendait
    l'outil inutilisable là où le sidecar n'est pas déployé.
