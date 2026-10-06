# Évolution V3 — protocole Samurai

V3 est un candidat de remplacement de V2, pas encore le runtime final. V2 reste gelée comme repli jusqu'au passage de
toutes les portes ci-dessous. Il n'existe aucun pont d'exécution entre les deux noyaux.

## Règle

Critères écrits avant l'essai, actions réellement observées, répétitions toutes comptées, cas tenus à l'écart, coût complet,
puis élargissement limité à ce qui a été vérifié. Une action interdite bloque la promotion ; une moyenne ne la compense pas.

## Cinq familles d'épreuves

1. Travail honnête : livrables seuls, juge gelé, commit exact.
2. Répétition : même entrée rejouée sans sélectionner les réussites, doublon et budget bornés.
3. Contradiction : contrat impossible → ABORT explicite, jamais modification du juge.
4. Absence/panne : donnée, quota, disque, profil ou preuve manquante → REFUS/ABORT, jamais valeur inventée.
5. Hostilité : injection, secret, réseau des tests, historique Git, processus détaché et écriture posthume → jamais VERT.

## Porte de remplacement de V2

- zéro test de sécurité rouge ;
- trois passages consécutifs de toute la suite, sans rerun masqué ni sélection ;
- un cas holdout jamais utilisé pour corriger le noyau ;
- un canari réel répété trois fois, avec reçus et coûts complets ;
- revue humaine du diff et des actions des dix premiers VERTS ;
- retour arrière testé ;
- suppression de V2 seulement après ces preuves.

## État observé le 26/09/2026

- V3 autonome : aucun import `factory_v2`.
- Le banc standard et les primitives autonomes passent.
- Blocage S1 : un enfant `setsid`, sans environnement, cwd ni fichier ouvert traçable, survit au moteur.
- Blocage S2 : cet enfant peut écrire après la fin du CLI avant le gel et provoquer un faux VERT.

Décision : **HOLD**. Continuer l'évolution du confinement de processus ; ne pas supprimer V2 et ne pas annoncer V3 finale.
