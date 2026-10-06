# Jocelyn Factory — d'un orchestrateur IA de 67 000 lignes à un noyau de 724 lignes

Une usine logicielle qui fait construire du code par des IA (GLM, Codex, Claude) **sans jamais croire une IA sur parole**.
Ce dépôt montre les trois générations, côte à côte, et pourquoi chacune a remplacé la précédente.

> Instantané public au 06/10/2026 (sans historique git, secrets vérifiés absents avec gitleaks).
> Le développement continue dans un dépôt privé.

## Les trois générations

| | Période | Taille | Idée | Statut |
|---|---|---|---|---|
| **1. JOÃO.AI** | juillet 2026 | orchestrateur Python + machine d'état | « portes » de validation, un humain qui décide | remplacé → [showcase](https://github.com/josslignio/joao-ai-delivery-engine-showcase) |
| **2. Factory V1** — [`2-factory-v1/`](2-factory-v1) | août → 22 sept. 2026 | **66 826 lignes** de contrôle (`factory/` + `factory_v2/`) | superviseur, baux, quarantaine, juges maison, télémétrie | **abandonnée** |
| **3. Noyau + mini-factory** — [`3-noyau/`](3-noyau), [`4-mini-factory/`](4-mini-factory) | 24 sept. 2026 → | **724 lignes** (`run.py`) + 217 (`mf.py`) + ~1 200 autour | le moteur n'a pas les droits de tricher | **en service** (v16) |

## Pourquoi la Factory V1 a été abandonnée (faits mesurés, pas une opinion)
- **Chaque panne ajoutait un garde, aucun n'était jamais retiré.** Le 22/09 : 67 128 lignes de contrôle, 75 662 lignes
  de tests, 7 263 lignes de doctrine. Pour comparaison le même jour : mini-swe-agent tient sa boucle en 190 lignes.
- **Des fausses garanties.** Un « détecteur temporel » censé empêcher la triche a fait croire un trou réparé pendant
  deux heures ; il était cassable par un faux positif, un faux négatif, puis battu par construction. Il a été supprimé.
- **Elle réimplémentait ce qui existe** : superviseur, baux, CAS et quarantaine refaisaient launchd, Git et Temporal.
- **Elle ne produisait plus rien** : dix jours d'audits sans jalon livré ; un jalon « réglé » ne l'était qu'après que
  l'IA avait modifié le test qui la jugeait.

## Ce que fait le noyau à la place — « un noyau devient dur en devenant plus petit »
Le texte complet de la doctrine est dans [`docs/DOCTRINE.md`](docs/DOCTRINE.md). En résumé, un **entonnoir** :
1. **Contrat** fixé avant, hors du moteur IA.
2. **Frontière** : le moteur tourne dans un bac à sable macOS ([`bornage.sb`](3-noyau/factory_v3/bornage.sb)) — pas de secrets,
   pas d'autres runs, pas d'écriture sur le juge. Ce qu'il ne peut pas atteindre, il ne peut pas le falsifier.
3. **Moteur** : il dit ce qu'il veut, **rien de ce qu'il déclare n'entre dans le verdict**.
4. **Gel** : photographie immuable des livrables.
5. **Juge isolé** ([`juge_isole.py`](3-noyau/factory_v3/juge_isole.py)) dans un espace neuf, avec des épreuves que le moteur ne choisit pas.
6. **Reçu** : octets, inventaire, commandes de rejeu — recalculable par un tiers.

Règle de travail : toute faille se ferme d'abord **en retirant** une autorisation ou du code ; ajouter est le dernier recours.
Un vert ne compte que si le banc a prouvé qu'il savait devenir rouge (tests de mutation).

## Autour du noyau : la mini-factory
- [`mf.py`](4-mini-factory/mf.py) — file de jalons, un à la fois, reçu par jalon.
- [`autour/runner.py`](4-mini-factory/autour/runner.py) + disjoncteur — enchaîne seul, s'arrête après 3 abandons au lieu de boucler.
- [`autour/accepter_jalon.py`](4-mini-factory/autour/accepter_jalon.py) — un jalon n'est accepté qu'après passage du juge sur la base, une variante honnête et deux triches.

## Ce que ça a construit
- [SaveSpace Drive](https://github.com/josslignio/savespace-drive) — libérer de la place sur Mac, iCloud, Photos, Google Drive.
- [Mailbox Cleaner](https://github.com/josslignio/mailbox-cleaner) — nettoyer sa boîte mail, 100 % local.
- Pepita — vidéos TikTok automatiques (privé), Weekly Trading Radar ([tableau de bord](https://github.com/josslignio/weekly-trading-radar-dashboard)).

## Ce qui n'est PAS encore prouvé — dit franchement
- L'objectif « 48 h sans intervention humaine » n'a **jamais été tenu** : 7 tentatives, 0 réussie à ce jour.
- La Factory V1 est publiée à titre d'archive : elle n'est pas maintenue et ses tests ne sont pas inclus.

Licence : MIT.
