# TR_TEST_HYGIENE_V1

Derived task brief. NOT canonical intent — voir projects/twitter-radar/ROADMAP.yaml. Depot produit : /Users/jocelyngrosjean/conductor/workspaces/twitter-radar/twitter-radar-foundation-v2. Pas de git stash.
Ne modifie aucun fichier d'autorite ni aucun verificateur factory_v2/verify_tr_*.py.

## Outcome
Marqueur pytest `needs_socket` + hook dans tests/conftest.py : tentative de bind 127.0.0.1:0 (ou TR_FORCE_NO_SOCKET=1) →
skip type `SOCKET_BIND_REFUSED` ; tous les tests qui ouvrent un port sont marques ; la suite complete est verte avec et
sans la simulation. Outcome IMPLEMENTED seulement si les deux commandes d'acceptance sortent rc=0 dans ta session.

## Notes
- Trouver les tests : grep -rn 'socket\|bind\|http.server\|127.0.0.1\|localhost' tests/ ; les 4 tests *web* connus en font partie.
- Jamais xfail ni pass silencieux : un skip visible avec raison (-rs l'affiche).

## Raisons PARTIAL typees
TOOL_MISSING | DATA_INSUFFICIENT | BUDGET_EXHAUSTED.
