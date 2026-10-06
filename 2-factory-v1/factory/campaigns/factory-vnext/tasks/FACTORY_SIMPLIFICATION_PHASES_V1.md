# Exécution des phases de simplification — consigne partagée

> Mode actuel (ordre propriétaire 23/09, SPEC AC08) : audit et synthèse exécutés hors
> dispatcher sous pilotage Codex, daemon arrêté. Les CLI directes bornées sont autorisées
> pour ce périmètre ; GLM A et Claude B couvrent chacun TOUT le périmètre en isolation.
> Les copies externes écrivent leurs checkpoints et rapports sous `output/` selon leur
> manifeste. La règle historique `REPORT.json` à la racine s’applique à une adoption runtime
> ultérieure, pas à ces lectures externes ; ne pas refaire une lecture pour cet emplacement.
> Ne pas modifier de reçu/état ni déclarer un règlement à partir d’un rapport externe.


Lire STRATEGY « Direction propriétaire » et SPEC AC01–AC08 intégralement, puis le jalon actif
et ses sorties dans ROADMAP. L'identifiant du jalon détermine la phase ci-dessous. Ce fichier
n'est ni un backlog parallèle ni une autorisation de dépenser hors des allocations existantes.

## Objectif
Enchaîner les phases de simplification issues de l'audit : synthèse comparative, protocole d'évaluation, baseline descriptive, contrat du noyau minimal, challenger, comparaison/canary/rollback/promotion CAS. Chaque phase rend des preuves, pas des intentions.

## Préflight et continuité communs

Lire autorités à HEAD, position/contrat/candidat, résultats réutilisables, quotas/capacités,
budget consommé et prochain travail éligible. Pas de moteurs lancés manuellement, de nouveaux
grants implicites, de mutation directe d'état ou de code du champion. Chaque commande prévue
doit être résolue dans le dépôt à un SHA, avec une interface réellement vérifiée.
Un résultat déjà présent se vérifie et s'adopte ; il ne se reconstruit pas pour fabriquer un diff.
Les fichiers de roadmap ne sont que des précontrôles : la revue indépendante rejoue les preuves.

## FACTORY_COMPARATIVE_SYNTHESIS_V1

A et B lisent indépendamment plusieurs produits publics réellement comparables, sur les mêmes
SHAs (AC02–AC03). Suivre une mission depuis le but jusqu'à l'intégration et la reprise, relever
chaque propriétaire d'état et garantie manquante. Utiliser les recherches existantes comme
index ; ne pas scanner vingt dépôts par obligation. Sorties PRODUCTS_A/B = lectures réelles.

Après scellement seulement, confronter ces rapports et tous les lots locaux. COVERAGE_FINAL
rapproche la totalité des sources assignées et les extensions découvertes. Si des sources de
contrôle/tests restent non lues, proposer les sous-étapes bornées au contrôleur pour intégration
par la voie d'autorité existante ; ne pas cocher couvert. BRICKS/CORE_SPEC/MIGRATION_PLAN suivent
AC04–AC05 : architecture unique, responsabilités, interfaces, suppressions, risques, effort,
critères de choix et budgets. Choisir ce que l'expérience justifie ; garder le reste explicitement.

## FACTORY_EVAL_DISCIPLINE_V1 — périmètre remplacé

AC06 uniquement : protocole, matrice exigences/preuves, holdout indépendant, métriques et seuils.
Lire le journal, recalculer sources/fenêtres/dénominateurs et les dettes nominatives. Identifier
les commandes EXISTANTES du benchmark/chaos et les charges produit réellement éligibles.
Faire sceller les cas hors accès du futur builder par le rôle évaluateur indépendant existant ;
le sceau public ne contient pas les cas. Vérifier l'isolation, pas seulement le hash.
Aucun C1–C7 de l'ancien plan, aucun juge, banc v3 ou interpréteur nouveau. Les corrections
b4e3cc0a sont préservées et liées aux fiches d'audit ; ni suppression ni fusion automatique.

## FACTORY_CHAMPION_BASELINE_48H_V1

Réutiliser une collecte valide du champion amorcée pendant l'audit, sinon le contrôleur planifie
la fenêtre avec les outils existants et une charge produit réelle (AC06). Pas de moteur en
attente pendant 48 h, pas de polling coûteux ni d'essais équivalents avant fin de fenêtre.
Consolider observations brutes, tentatives, résultats, coûts, délais et interventions dans les
sorties. Toute impossibilité de collecter devient une lacune précise ; ne pas fabriquer de reçu.
Cette phase décrit les défauts du champion et n'exige pas qu'il soit READY.

## FACTORY_MINIMAL_CORE_CONTRACT_V1

Transformer les décisions d'audit en un plan de tranches concrètes : périmètre, appels upstream,
versions/licences, suppression, tests existants résolus, migrations, risques, budget et rollback.
Figer l'oracle indépendant et les seuils avant le build, dont <=8 000 lignes finales maison.
Nommer la première tranche de bout en bout et les suivantes. Le contrôleur matérialise les
sous-missions nécessaires par son chemin normal de mise à jour d'autorité, sans question
technique routinière au propriétaire et sans lancer un worker sur un contrat non gelé.
Pas d'architecture présélectionnée ni d'estimation de délai présentée comme mesure.

## FACTORY_MINIMAL_CORE_CHALLENGER_V1

Exécuter uniquement les tranches du contrat validé en isolation. Claude porte le contrôle A/S,
GLM les missions B que le contrôleur alloue ; ne pas contourner le routeur ni lancer des agents
soi-même. Commencer par une chaîne minimale fonctionnelle, tester après chaque tranche, garder
un candidat reprenable à la limite de budget. Le champion vivant n'est pas le terrain de build.
Code public réellement appelé, garanties restantes minimales, tests conservés ou remplacés
avec preuve ; migration et retrait de l'ancien chemin dans le candidat. Produire comptage
complet, preuve d'appel/refus et statut honnête. Le gate build ne donne pas droit à promotion.

## FACTORY_MINIMAL_CORE_PROMOTION_V1

Un évaluateur indépendant rejoue AC07 et le contrat gelé sur mêmes tâches/SHA/données/budgets.
Publier les observations brutes par bras et les échecs ; la taille ne compense aucun défaut.
Exécuter canary et rollback après écritures du challenger avant le CAS. Utiliser le mécanisme
existant d'intégration/promotion ; aucun git reset forcé, fusion manuelle ou mutation du champion
par le builder. CAS perdant = requalification, pas force. Retirer l'ancien code actif à la
promotion et recompter. INCONCLUSIVE/REJECT conservent le champion et une décision durable ;
ne pas déclarer le jalon de promotion COMPLETE ni démarrer sa qualification comme si ACCEPT.

## Rapport et arrêt borné

À toute limite : enregistrer ce qui est réalisé, les preuves, le coût, la cause exacte et la
prochaine action sûre. Le contrôleur poursuit les travaux indépendants autorisés ou reprend
après la condition attendue. Pas de questions de permission pour l'enchaînement déjà autorisé ;
pas de contournement si une vraie permission ou une preuve indispensable manque.

## Complément propriétaire — données et versions (23/09)

Appliquer `projects/factory/audit/MEASUREMENT_COMPARISON_REQUIREMENTS.md`, exigence de SPEC AC06b.
Les lots, comparaisons de briques et synthèse relient leurs décisions aux benchmarks/tests,
mesures de temps et consommation, incidents/correctifs et versions historiques disponibles.
Conserver les sources et inconnues ; distinguer Factory, produit et audit. Les données nouvelles
arrivent comme complément factuel commun après scellement, sans lire le rapport adverse.
Les sorties restent celles du jalon ; ni nouveau framework ni migration autorisée.
