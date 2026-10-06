# Consigne canonique — audit comparatif intégral, tous lots

> Mode actuel (ordre propriétaire 23/09, SPEC AC08) : audit et synthèse exécutés hors
> dispatcher sous pilotage Codex, daemon arrêté. Les CLI directes bornées sont autorisées
> pour ce périmètre ; GLM A et Claude B couvrent chacun TOUT le périmètre en isolation.
> Les copies externes écrivent leurs checkpoints et rapports sous `output/` selon leur
> manifeste. La règle historique `REPORT.json` à la racine s’applique à une adoption runtime
> ultérieure, pas à ces lectures externes ; ne pas refaire une lecture pour cet emplacement.
> Ne pas modifier de reçu/état ni déclarer un règlement à partir d’un rapport externe.


Lis les trois autorités à HEAD, puis SPEC « Contrat de campagne » AC01–AC08 intégralement.
La mission active et sa description donnent le lot, la tranche et le rôle. Ne jamais supposer
L1 à partir du nom d'une ancienne consigne. Manifeste :
`projects/factory/audit/COMPARATIVE_SCOPE_2026-09.json` (corpus et plages exactes).

## Objectif
Audit comparatif intégral par tranches : deux lectures indépendantes de notre code ET du vrai code public à un SHA figé, confrontation après scellement, expériences qui tranchent, fiches de décision exploitables. Aucun code de production modifié.

## REPORT.json — emplacement imposé par la porte (défaut constaté 23/09 06:35)

`REPORT.json` s'écrit **à la racine du dépôt candidat**, jamais dans un sous-dossier de livrables.
La porte le lit par `git show <candidate_sha>:REPORT.json` : un `REPORT.json` placé dans
`factory_v2/audit/FULL_AUDIT_2026-09/<lot>/` est invisible pour elle et produit
`BUILDER_REPORT_MISSING` — c'est ce qui a fait échouer les essais 1 et 2 de L1 et déclenché
7 cycles sans progrès. Les rapports de lecture (`a_glm.json`, `b_claude.json`,
`FICHES_DECISION.md`, `evidence_*/`) restent, eux, dans le dossier du lot.

## Préflight obligatoire de la mission

Relève mission/étape/contrat, corpus, moteur, budget et sorties attendues. Vérifie les blobs.
Réutilise les sources de l'inventaire et les recherches précédentes comme index. Vérifie le
résultat déjà disponible : pas de nouvelle lecture si elle est valide sur les mêmes entrées.
Aucune production ne peut être modifiée. Les expériences utilisent des copies temporaires,
sans moteur réel supplémentaire et sans toucher le daemon, les leases ou les données vivantes.
Les quotas réels et capacités priment sur les plafonds de temps inscrits dans la roadmap.

## Lecture A ou B d'une tranche

Lire intégralement les plages assignées, fonctions englobantes, appelants réels, état, branches,
erreurs et tests pertinents ; suivre imports dynamiques, sous-processus et configuration.
Les deux lecteurs examinent les mêmes sources upstream au SHA : composants et dépendances
porteuses. Plusieurs candidats crédibles quand ils existent ; absence documentée acceptable.
Tout ajout au corpus commun doit être examiné par les deux avant synthèse.

Avant de commencer, vérifier l'isolation AC02 dans le manifeste de dispatch. Ne pas ouvrir de
rapport adverse, de synthèse, ni d'historique qui le contient. Si les rapports ont été fusionnés
et sont accessibles dans le contexte distribué, ne pas déclarer cette mission indépendante :
remonter la preuve au contrôleur pour qu'il fournisse une copie propre via le mécanisme existant.
Le hash d'un rapport ne prouve pas à lui seul la cécité de l'autre lecture.

Rapport `projects/factory/audit/results/<LOT>/<TRANCHE>_<a_glm|b_claude>.json` :
- schema, lot, batch, engine, corpus_sha, authority_hashes, budget et coût observé ;
- unités lues path/blob/symbole/plages et cinq dimensions de couverture AC01, avec preuves ;
- rôle/accessibilité/décision séparés ; constat, source exacte, contre-exemple, limites ;
- upstream_read : repo/SHA/licence/fichiers/symboles/tests/dépendances, correspondances ;
- options par responsabilité, hypothèses d'expérience et lacunes.
Ne pas écrire le rapport de l'autre moteur ni inventer une exécution. « Aucun défaut observé »
est valide. Ne pas confondre une lecture partielle avec la totalité du fichier.

## Synthèse du lot (après les deux lectures seulement)

Confronte les rapports scellés ; consolide a_glm.json et b_claude.json en index des rapports
réels, sans inventer de lectures. Produis COVERAGE.json, FICHES_DECISION.md (AC04), et
EXPERIMENTS.json avec protocole horodaté avant exécution, commandes, sorties, exits et hashes.
Rejoue au moins une hypothèse matérielle pertinente avec les outils existants, garde les échecs.
Publie INDEPENDENCE.json depuis les manifestes/journaux du contrôleur : pas de preuve autoécrite
substituée à ces entrées. La relecture finale reconstruira ces faits indépendamment.

Avant clôture, rapproche les plages assignées et la couverture A/B. Toute extension trouvée
hors périmètre initial est nommée avec ses preuves ou ses lectures restantes ; le contrôleur
planifie les compléments bornés, sans réclamer un nouveau GO technique au propriétaire.
Aucun désaccord imposé. Pas de décision REMPLACER définitive sans expérience portant sa garantie.

## Discipline

Pas de nouvelle plateforme d'audit, pas de juge/chaos v3/interpréteur, pas de « réparations
utiles » ajoutées au champion. Ni clonage entier du champion pour le rebaptiser minimal.
Les contrôles JSON de roadmap sont des précontrôles de forme ; la revue exacte obligatoire
inspecte les sources et rejoue les expériences. Le moteur ne s'autoattribue jamais ACCEPT.
Sortie à la première limite : résultat partiel réutilisable, cause précise, coût et prochaine
action. Le contrôleur assure la continuité bornée ; ne pas lancer soi-même d'autres moteurs.
