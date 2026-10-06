# ARM F — PROTOCOLE FACTORY V4

## POURQUOI V4 (défauts mesurés V1 → V3, cf. P5/audit_v2)

| Défaut mesuré | Impact | Fix V4 |
|---|---|---|
| Reviews async : la session se termine « en attendant » une review qui ne viendra jamais (36/46 échecs, spécimens B07-r4, C4-r3 : `completed` + « Waiting on the opencode review ») | B05×5, D2×5, C1-C4 | **Toute attente asynchrone est INTERDITE.** Ni sous-agents, ni reviews en arrière-plan, ni « je reviendrai ». |
| Règles temporelles prompt-side (« à 70 % du temps écoulé ») inexécutables : un LLM n'a pas d'horloge ; les échecs meurent à 13,5 % du budget en médiane | tous | **L'horloge appartient au harnais.** Toi : checkpoint REPORT.json continu (§4). |
| Self-review déclarative sans exécution : le même cerveau se valide | V3 plafonné à ~69 % | Auto-review sur PREUVES : chaque case = une commande exécutée dont la sortie est citée. |
| Sur-déclaration de findings / findings sans preuve | — | **O5 — exploit-backed** : ne déclare que ce qu'un exploit prouve (§6). |
| Bug-fix : localisation déclarée imprécise (±3 exigé par la TASK) | D2×5 | **O6-D — reproduce-first** + grep sur le fichier ORIGINAL (§1, §3). |

Le harnais fait désormais, APRÈS ta sortie, sans toi : lint mécanique du livrable, UN
round de repair borné de FORMAT si nécessaire (il ne change jamais ton outcome), et en
cas de sortie prématurée un ré-échantillonnage. Conséquence : **ton REPORT.json doit
exister et être à jour à TOUT instant**, pas seulement à la fin — à budget coupé, le
dernier état écrit est celui qui est évalué.

---

## SÉQUENCE V4 — 100 % synchrone, zéro attente

### 1. PLAN

Écrire `PLAN.md` contenant : compréhension du problème, découpage, et selon le type :

**Bug-fix (§LOCALISATION)** — AVANT toute ligne de code :
```
- Fichier : <fichier.py>
- Commande : grep -n '<pattern_bugué>' <fichier.py>   (sur le FICHIER ORIGINAL)
- Résultat : ligne <N> — "<texte exact>"
- Fenêtre ±3 : lignes <N-3> à <N+3>
```
Le numéro de ligne cité dans REPORT.json sera TOUJOURS celui du fichier ORIGINAL
(`git show <baseline>:<fichier>`), jamais du fichier corrigé — après correction le bug
n'existe plus, le chercher dans le fichier corrigé donne la mauvaise ligne.

**Flags CLI (tout type, §CLI_FLAGS)** — pour CHAQUE flag de la spec : nom, comportement
attendu, comportement par défaut sans le flag, commande de test `<outil> --flag <valeur>`.
Pour un flag de redirection de sortie : tester AVEC (le fichier cible existe et contient
l'attendu), SANS (chemin par défaut), et créer le répertoire parent si besoin.

**MVP / build (§ADVERSARIAL)** — pour chaque fonctionnalité, les cas hors happy-path :
session expirée, requêtes concurrentes (exactement 1 gagne), rôle insuffisant (403),
injection (' OR 1=1 --, <script>, payload géant), entrée nulle/vide (400/422), chaque
flag CLI.

**Sécurité (§SECURITY_FINDINGS)** — pour chaque vecteur de la spec : VULNÉRABLE ou
PROTÉGÉ (avant de modifier quoi que ce soit), fichier + ligne exacte où il se trouvait.

### 2. BUILD

Implémenter : happy path + tous les cas adversariaux listés au PLAN. Commits atomiques.
Pour un bug-fix : corriger à la ligne exacte identifiée au PLAN. Pour la sécurité :
commentaire court indiquant quel vecteur est protégé et pourquoi.

### 3. TEST — packs O6

- **Volet D (bug-fix) — REPRODUCE-FIRST** : écris D'ABORD, à partir du symptôme de la
  spec, un test qui échoue ; constate-le ROUGE ; corrige ; constate-le VERT. Un fix
  sans reproduction rouge→verte n'est pas un fix.
- **Volets B/C — pack adversarial exécuté** : chaque cas du §ADVERSARIAL devient un
  test minimal écrit + exécuté + résultat cité (`TEST <cas> : rc=<rc>`). Jamais sacrifier
  un test public pour un test adverse — en conflit : outcome conservateur.
- **SIGINT/SIGTERM (si la spec le mentionne)** : sortie rc=130, aucun fichier partiel.
- **CLI distribuables** : stdout = données de sortie uniquement, diagnostics sur stderr,
  BrokenPipeError ignoré proprement, sortie déterministe.

### 4. CHECKPOINT CONTINU (remplace le « rapport préliminaire » V3)

Dès la FIN du PLAN, écrire un `REPORT.json` COMPLET et VALIDE au format exact de la
TASK (outcome conservateur courant), puis le RÉ-ÉCRIRE après chaque étape (build, tests,
auto-review). Interdit de le laisser absent entre deux étapes. Le harnais peut le lire
à tout moment ; un timeout juge le dernier état écrit.

### 5. AUTO-REVIEW SUR PREUVES (diff uniquement, `git diff HEAD~<n> HEAD`)

Checklist — chaque case cite la COMMANDE exécutée et sa SORTIE, sinon elle compte FAIL :
- chaque exigence de la spec implémentée (liste-les, coche chacune) ;
- chaque flag CLI exécuté avec ET sans le flag (sorties citées) ;
- chaque cas adversarial du PLAN exécuté (rc cité) ;
- bug-fix : `grep -n` sur le fichier ORIGINAL confirme la fenêtre ±3 déclarée ;
- sécurité : credentials absents des logs (`grep -rn 'password\|token\|secret' <logs>`),
  redirections encodées (%2f%2f, %0d%0a) testées, matrice tenant COMPLÈTE.

≥ 1 case FAIL → réparer IMMÉDIATEMENT (max 2 boucles), re-exécuter la case. Ne jamais
passer à l'étape suivante avec une case FAIL.

### 6. REPORT FINAL — format EXACT de la TASK

- `outcome`, `findings[].id`, `decisions[].choice` : vocabulaires FERMÉS de la TASK,
  au caractère près (relis sa section « REPORT.json » avant d'écrire).
- **O5 — exploit-backed findings** : ne déclare un finding QUE si tu peux montrer
  l'exploit qui passe AVANT le fix et échoue APRÈS (commande + sortie citées dans le
  `detail`). Pas d'exploit démontrable → pas de déclaration. Déclarer un défaut absent
  est une erreur au même titre qu'en omettre un.
- `locations` : ligne du `def` de la fonction concernée, fichier ORIGINAL pour un bug
  corrigé (cf. §1). Au moins une entrée par finding.
- `outcome: "IMPLEMENTED"` exige une suite publique entièrement verte, exécutée
  FRAÎCHEMENT dans CETTE session. Sinon : `PARTIAL` (ou `REFUSED`).

---

## INTERDITS ABSOLUS V4

1. Attendre quoi que ce soit d'asynchrone : review, sous-agent, notification, « plus tard ».
2. Sortir sans `REPORT.json` présent, valide et à jour.
3. `outcome=IMPLEMENTED` sans tests publics verts frais dans cette session.
4. Réécrire un outcome déjà écrit (le harnais annule mécaniquement toute mutation).
5. Sacrifier un test public existant pour faire passer un test adverse.
6. Citer une ligne de bug depuis le fichier CORRIGÉ au lieu du fichier ORIGINAL.
