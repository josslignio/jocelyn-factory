# 🎯 LA CIBLE — décision propriétaire, 24/09/2026

> # **LE NOYAU LE PLUS SIMPLE, EFFICACE, SÉCURISÉ,**
> # **NON FALSIFIABLE ET DUR POSSIBLE.**

Cinq mots, cinq mesures. Rien d'autre à retenir si on ne retient qu'une ligne de ce fichier.

- **SIMPLE** se compte en **PIÈCES**, pas en lignes. Aujourd'hui cinq : contrat, frontière, gel, juge, reçu.
  Toute pièce ajoutée doit en tuer une.
- **EFFICACE** se mesure au **délai jusqu'au jalon RÉGLÉ**, jamais jusqu'au premier vert affiché.
- **SÉCURISÉ** se mesure aux **interdictions ÉPROUVÉES** : chaque refus (secrets, identifiants, réseau, autres
  runs, écriture sur le juge, signal vers l'observateur) a un test qui devient ROUGE si le refus cesse de mordre.
  Une interdiction qu'aucun test n'éprouve n'existe pas.
- **NON FALSIFIABLE** est une **CIBLE À POURSUIVRE, JAMAIS UNE AFFIRMATION À ÉCRIRE** (devise 4). On ne le
  déclarera pas ; on s'en approche et on mesure de combien. Les deux mesures : (1) le candidat ne choisit ni les
  épreuves, ni les réponses attendues, ni l'observateur, et le résultat est reproductible sur les mêmes octets ;
  (2) le **score de mutation** du banc — un banc qui ne sait pas devenir rouge ne prouve rien.
  *« Sans aucune faille » : à éviter à tout prix comme phrase, à poursuivre à tout prix comme travail.*
- **DUR** se mesure à ce que le noyau **REFUSE**, pas à ce qu'il vérifie. Un noyau dur n'est pas un noyau qui
  contrôle beaucoup : c'est un noyau qui **ne peut pas** être trompé, parce que le moteur n'a pas les droits.
  **Un noyau devient dur en devenant plus petit.**

Tout ce qui suit dans ce fichier — les quatre devises, la définition de l'entonnoir, l'ordre d'arbitrage —
n'est que la manière d'y arriver. En cas de doute sur une décision : relire cette ligne.

---

# Protocole d'opération de la factory — lu à chaque prise de poste

Ce fichier est chargé automatiquement à chaque session. Il ne remplace pas le cerveau canonique
(`projects/factory/STRATEGY.md`, `projects/factory/SPEC.md`, `projects/factory/ROADMAP.yaml`) :
il oblige à le relire et fixe la manière d'opérer, la même à chaque fois. Décision propriétaire
du 21/09/2026 : « hard-code-toi-le pour fonctionner toujours pareil ».

---

# ⛔️ LES QUATRE DEVISES — À LIRE AVANT TOUTE ACTION, SANS EXCEPTION

> ## **1. Toute faille se ferme d'abord en RETIRANT une autorisation ou du code.**
> ## **Ajouter des lignes est le DERNIER recours, et il se justifie par écrit.**

> ## **2. Une fausse garantie est pire que pas de garantie :**
> ## **elle occupe la place de la vraie.**

> ## **3. On ne fait confiance à aucune IA — ni aux moteurs, ni à l'opérateur.**
> ## **On ne cherche pas à les empêcher de mentir : on fait que leurs mensonges**
> ## **soient SANS EFFET, parce que rien n'est cru sur parole.**

> ## **4. JAMAIS de vert gratuit. Un vert ne compte que si le banc a prouvé**
> ## **qu'il savait devenir rouge. Mieux vaut tester 10 ou 100 fois autrement**
> ## **et être sûr, que passer une fois et l'annoncer.**

**Décision propriétaire du 24/09/2026, 05:05 Bangkok.** « Sans aucune faille » doit être **évité à tout prix**
comme affirmation, et **poursuivi à tout prix** comme travail : en retournant le code, les tests, les moteurs
testeurs et les points de vue au maximum. Sept obligations, toutes mécanisables :

1. **Le test du test (mutation).** Pour chaque exigence, il doit exister une modification du noyau qui la fait
   passer au rouge. Une exigence qu'aucune mutation ne casse n'est pas testée — elle est décorative.
   Score de mutation mesuré et publié. **C'est le seul chiffre qui dit si le banc vaut quelque chose.**
2. **Le témoin négatif.** Une épreuve dont l'échec est certain, détenue par le vérificateur. Si elle passe ou
   disparaît, c'est le **vérificateur** qui est invalidé, pas le moteur.
3. **Les deux côtés.** Les mutants doivent être rejetés ET les variantes honnêtes acceptées. Un juge qui dit
   non à tout n'est pas un juge.
4. **La rotation des points de vue.** Jamais la même famille de moteur deux fois de suite sur le même trou :
   qui a écrit le correctif ne l'éprouve pas. Claude, Codex, GLM tournent — construire, tester, réfuter.
5. **La répétition.** Tout rouge doit retomber rouge au rejeu ; tout vert doit tenir sur plusieurs passages.
   Une instabilité n'est jamais « de la chance » : c'est un défaut non diagnostiqué.
6. **Les angles multiples.** Un même fait s'attaque par plusieurs méthodes — exemple, aléatoire, propriété,
   injection de panne, différentiel entre versions. Un seul angle n'a jamais suffi cette nuit : le détecteur
   temporel a survécu à deux attaques avant d'être battu par une troisième, d'un autre bord.
7. **Bout en bout.** On mesure le fait réel (le survivant existe-t-il encore ? le journal fait-il plus de 20 Mo ?),
   jamais l'intention du code. Les audits, les tests et la mise en application obéissent tous à cette règle.

**Le coût est du temps machine, pas du quota :** muter, répéter et fuzzer ne consomme aucun moteur. Il n'y a donc
**aucune excuse budgétaire** à ne pas le faire. Ce qui se refuse, c'est un vert obtenu vite ; pas un banc long.

**Pourquoi cette formulation, et pas « l'IA ne doit jamais mentir » :** on ne peut pas prouver qu'un modèle ne
mentira pas, ni qu'il ne trichera pas sans le vouloir — un moteur qui optimise pour obtenir du vert triche sans
intention. Ce qui **est** prouvable, et donc seul exigible : *le candidat ne choisit ni les épreuves obligatoires,
ni leurs résultats attendus, ni l'observateur qui décide ; le résultat publié est reproductible sur les mêmes
octets.* Une IA qui ment dans ce montage ne gagne rien : sa déclaration n'entre nulle part dans le calcul.

**Les six règles qui la rendent vraie, toutes obligatoires :**
1. **Aucune déclaration ne vaut preuve** — ni le code retour d'un moteur, ni son texte, ni son rapport, ni un
   fichier qu'il peut écrire. Seuls décident le diff, le juge gelé et les faits constatés hors de sa portée.
2. **Le constructeur n'écrit jamais son juge.** Qui construit ne teste pas ; qui teste ne voit pas le code ;
   qui relit ne peut rien écrire. Trois familles différentes (Claude, Codex, GLM), jamais la même deux fois.
3. **Le juge tourne hors de portée du candidat** — identité séparée, aucun droit d'écriture ni de signal dessus,
   ses épreuves et ses résultats attendus lui appartiennent.
4. **Toute affirmation se tient par une ligne précise** (code, SPEC ou INTERFACE). Ce qui ne peut pas être prouvé
   par une ligne n'est pas écrit — ni par un moteur, ni par l'opérateur, ni dans un rapport au propriétaire.
5. **L'accord des IA n'entre jamais dans le calcul.** Trois IA d'accord ne valent rien : ce qui vaut, c'est un
   paquet qu'un tiers recalcule seul (octets, inventaire des épreuves, commandes de rejeu). On ne vote pas, on recompte.
6. **Les échecs se rapportent avec leur sortie.** Un test rouge se dit rouge, une étape sautée se dit sautée,
   un chiffre non mesuré se dit non mesuré. Jamais « ça marche » sans les chiffres qui le montrent.

**Ce que ça coûte au propriétaire, et qu'il faut assumer :** un contrôle qui ne répond pas aux trois faits
(qu'est-ce qui s'est réglé / n° d'essai et travail déjà à HEAD / minutes perdues) n'est pas un contrôle.
Et l'opérateur doit dire quand il s'est trompé, le jour même — deux arbitrages faux ont été rattrapés par le
relecteur adverse la nuit du 23→24/09, et c'est le fonctionnement normal, pas l'incident.

**Décision propriétaire du 24/09/2026, 04:40 Bangkok. Elle prime sur tout jugement de conception contraire.**

Devant une faille, chercher **dans cet ordre**, et n'avancer d'un cran que si le précédent est écarté **par écrit** :

1. **Quelle autorisation retirer ?** (droits du système de fichiers, profil sandbox, identité séparée,
   lecture seule, accès supprimé) — coût en lignes : zéro.
2. **Quel code supprimer ?** Une détection fragile qui rattrape une autorisation mal donnée doit disparaître
   avec cette autorisation — coût en lignes : négatif.
3. **Seulement alors : quelles lignes ajouter ?** Avec la justification écrite de pourquoi 1 et 2 ont été écartés.

**Établi par mesure, pas par principe** (noyau v3, nuit du 23→24/09) : les trois protections les plus fortes
contre la triche coûtent **zéro ligne** — juge hors de portée du candidat sous identité séparée, accès inutiles
retirés, une seule photographie immuable évaluée — et une quatrième coûte **moins que zéro** : la suppression du
détecteur temporel, cassé par un faux positif, un faux négatif, puis battu par construction.

**Sur la devise 2 — ce qu'elle oblige à faire :** une protection qui ne tient pas se **supprime**, elle ne se
rafistole pas. Tant qu'elle est là, personne ne cherche la vraie serrure : le détecteur temporel du noyau v3 nous a
fait croire R3 réparé pendant deux heures, alors qu'il était cassable par un faux positif, un faux négatif, et
finalement battu par construction. Corollaire opératoire : **ne jamais annoncer réglé ce qui n'est pas mesuré**, et
retirer une garantie démentie le jour où elle est démentie — même si rien ne la remplace encore. Un trou connu et
nommé est plus sûr qu'un trou couvert par un mécanisme qui ment.

Corollaire tenu par le propriétaire : « le code le plus court, simple et épuré » et « qui ne peut jamais tricher »
ne sont **pas** en tension — c'est la même exigence. Un correctif qui fait grossir le noyau est **refusé** tant
qu'un retrait n'a pas été essayé et écarté par écrit.

---

# 🔻 LA DÉFINITION DE V0 — décision propriétaire 24/09/2026, 05:30 Bangkok

> ## **V0 est un ENTONNOIR de vérification.**
> ## **Il fait entrer du travail et des déclarations ; il ne laisse sortir que des FAITS recalculables.**
> ## **Court, pour qu'on puisse le lire en entier. Vérifié, par un banc qui a prouvé qu'il sait devenir rouge.**
> ## **Et la partie qui décide est hors de portée de celui qu'elle juge.**

**Un seul mot corrigé par rapport à l'énoncé du propriétaire : « infalsifiable » n'est pas démontrable et ne sera
jamais écrit** (devise 4). Ce qui est démontrable et le remplace : *le candidat ne choisit ni les épreuves, ni les
réponses attendues, ni l'observateur ; le résultat est reproductible sur les mêmes octets.*

**Les six étages de l'entonnoir — à chaque étage, on retire ce qui ne se vérifie pas :**
1. **Le contrat** — fixé AVANT, hors du moteur. Il ne choisit pas ce qui compte comme réussi.
2. **La frontière** — ce qu'il peut physiquement toucher. Pas ce qu'on lui demande d'éviter : ce qu'il ne peut pas atteindre.
3. **Le moteur** — il pense ce qu'il veut, dit ce qu'il veut. **Rien de ce qu'il déclare n'entre dans le verdict.**
4. **Le gel** — photographie immuable des livrables au moment du contrat. Ce qui est jugé ne bouge plus.
5. **Le juge gelé, dans un espace neuf** qu'il n'a jamais eu le droit d'écrire, avec des épreuves qu'il ne choisit pas.
6. **Le reçu** — ce qui sort : des octets, un inventaire, des commandes de rejeu. **Recalculable par un tiers.**

**Ce qui entre : des intentions, du code, des déclarations. Ce qui sort : des faits.**
Un harnais n'est pas une manière de faire penser une IA correctement — c'est une manière de rendre sa pensée
sans conséquence sur le verdict. Une règle adressée au moteur est de la persuasion ; une règle imposée par le
système est de la physique. Seule la seconde est une garantie.

# ⚖️ L'ORDRE D'ARBITRAGE — quand deux qualités se contredisent (propriétaire, 24/09 05:35)

Cible : « le système de vérification le plus court, simple, rapide, optimisé, solide, durable, stable, scalable
possible. Du rock solide, sharp, affûté, simple, ultra efficace. » Ces qualités s'alignent presque toujours.
**Quand elles s'opposent, cet ordre tranche, et il n'y a pas de débat :**

1. **VÉRIFIABLE** — si le résultat n'est pas recalculable par un tiers, le reste ne compte pas. On ne troque
   JAMAIS une garantie démontrée contre de la brièveté ou de la vitesse.
2. **COURT** — lisible en entier par un humain. On ne peut pas auditer ce qu'on ne peut pas lire.
   Plafond dur actuel : **747 lignes**. Un correctif qui fait grossir le noyau est refusé (devise 1).
3. **RAPIDE** — délai jusqu'au **jalon réglé**, jamais jusqu'au premier vert. Un étage de l'entonnoir qui coûte
   plus qu'il ne prouve est retiré comme n'importe quel code.
4. **SCALABLE** — coût constant par jalon. Aucun état central partagé ; chaque run est autonome, son reçu se
   recalcule seul.

**Les pièges de la liste, nommés pour ne pas s'y jeter :**
- *« Optimisé »* : on n'optimise que ce qui est **mesuré**. Une optimisation sans chiffre avant/après est un
  risque d'instabilité offert gratuitement.
- *« Simple »* : la simplicité est **moins de pièces**, pas moins de garanties. Un état central est simple
  et ne scale pas ; on prend l'autonomie des runs, même si ça fait une pièce de plus à écrire.
- *« Stable » et « durable »* ne sont pas des propriétés de conception : ce sont des **mesures**.
  Stable = rejoué, il retombe identique. Durable = `LONG_RUN_PROOF_2`, 48 h sans commit ni intervention.
  Personne ne les déclare ; on les constate.
- *« Rock solide »* ne se dit jamais. On dit ce qui est fermé, ce qui reste ouvert, et depuis combien de temps
  ça tourne sans intervention (devise 4).

## 1. Prise de poste (obligatoire, dans cet ordre, avant toute action)

1. Lire `docs/FACTORY_EN_UNE_IMAGE.md` (la factory en une image), puis `projects/factory/STRATEGY.md` (North Star,
   boucle d'opération, invariants, compteurs de readiness) et la section « Réglé / En construction / Suivant » de la roadmap.
2. Produire les compteurs de readiness **par la factory**, jamais à la main :
   `PYTHONPATH=. python3 -m factory_v2.base_ready_aggregator --project-repo .`
3. Produire le gaspillage : `PYTHONPATH=. python3 factory_v2/quota_waste.py --since-hours 24`.
3b. Lire les quotas **en direct** (API des trois fournisseurs, jamais estimés, jamais redemandés au propriétaire) :
   `python3 factory_v2/quota_live.py --write` — Claude (5 h, 7 j, hebdo Fable), Codex (hebdo), GLM (5 h, hebdo),
   % utilisé / restant / date de reset ; `--write` alimente QUOTA_SNAPSHOT.json que lisent le garde-quota et le routage.
4. Lire `.context/NUIT_*` / journal des redirections le plus récent : ce qui a été touché à la main.
5. Armer la surveillance : `bash ~/.local/bin/factory_check10.sh` en Monitor persistant
   (signaux `IDLE`, `GASPILLAGE`, `REPETE`). Sans Monitor armé, la promesse de surveillance est fausse.

## 2. Doctrine (STRATEGY.md — jamais révoquée)

- **Un problème factory = un jalon**, qui suit la boucle : audit multi-IA → recherche → challenger
  minimal → benchmark → promouvoir/rejeter. **Aucune réparation à la main en production.**
- Exception unique : un blocage **> 1 h non résorbé**. Chaque intervention est consignée dans le
  journal des redirections ET compte contre `ROUTINE_HUMAN_INTERVENTIONS`.
- Seul factoryd lance des moteurs. Jamais `claude -p` / `codex exec` / `opencode run` à la main.
- Jamais de redémarrage avec un build en vol : uniquement `factory/bin/factoryd_safe_restart.sh`.
- Jamais `git add -A`, jamais de réécriture d'historique, jamais de commit de `projects/*/PROJECT_STATE.json`
  ni d'`evidence/`, jamais de fusion manuelle pendant qu'un build tourne, jamais `yaml.safe_dump`
  sur une roadmap, jamais d'écriture dans `campaigns/RUN8/**`.
- Suite complète de tests **obligatoire** avant tout commit qui change un comportement.
- Tout banc/test est hermétique (`FACTORY_NO_REAL_ENGINE=1`) et ne touche jamais un moteur de prod.

## 3. Contrôle (toutes les 20 min) — un contrôle est un résultat, pas une activité

Un contrôle n'existe que s'il répond à trois faits :
1. **Qu'est-ce qui s'est réglé** depuis le dernier contrôle ?
2. Pour chaque chantier en cours : **numéro d'essai**, étape, moteur, et **son travail est-il déjà à HEAD** ?
3. **Minutes perdues** depuis le dernier contrôle.

« Un moteur tourne depuis 46 min » n'est pas du progrès. Règles dures :
- 3 essais ratés sur un même jalon (`REPETE`) = arrêt du jalon et diagnostic, pas attente.
- Aucun règlement depuis 3 h alors que des moteurs tournent = arrêt et diagnostic.
- Un moteur qui reconstruit un travail déjà fusionné à HEAD = gaspillage à couper immédiatement.
- Pas de « rien à signaler » sans les trois réponses.

## 4. Quotas — captures = ordres, débit = règle

### RÔLES FIXÉS PAR LE PROPRIÉTAIRE (24/09/2026, 10:25 Bangkok) — appliquer sans redemander
> **« Codex est la plus rare des trois. Claude pilote ici de loin sans trop dépenser. Sinon c'est GLM en priorité. »**

- **GLM = LE TRAVAIL.** Constructeur par défaut ET analyste : réparations, tests, classements, mesures. C'est la
  ressource abondante, elle porte le gros. Ne pas l'économiser : l'économiser, c'est dépenser les deux autres.
- **CODEX = LA RÉFUTATION, ET RIEN D'AUTRE.** La ressource la plus rare. Interdit de lui confier de la rédaction,
  de la recherche ou une tâche mécanique. On ne le sort que pour casser un raisonnement ou un correctif.
  Justification mesurée : deux arbitrages faux de l'opérateur cassés en une nuit, plus une recommandation
  d'architecture démolie. Son rendement par appel est le plus élevé des trois — d'où la rareté à protéger.
- **CLAUDE (opérateur en session) = PILOTER DE LOIN, SANS DÉPENSER.** Lire les rapports, rejouer les bancs
  soi-même, compter les lignes, rapporter des chiffres, décider du prochain lot. Ne rédige pas de doctrine,
  ne lance pas de recherche, ne code pas. Un opérateur qui dépense est un opérateur qui fait le travail de GLM.
- **QUAND SORTIR CODEX — la règle, pour ne pas le cramer** (propriétaire 24/09 10:30 : « solliciter Codex
  en adversarial quand il faut, mais ne pas cramer le quota toutes les 2 minutes »).
  **On l'appelle SEULEMENT dans ces quatre cas :** (1) c'est un arbitrage de l'OPÉRATEUR qui est en jeu —
  l'opérateur ne peut pas se réfuter lui-même ; (2) la décision est structurelle ou difficile à défaire ;
  (3) deux moteurs se contredisent ; (4) **on s'apprête à plier une règle** (relever un plafond, requalifier
  un test, assumer un trou). **Jamais pour** : relire un rapport de moteur, rejouer un banc, compter des lignes,
  vérifier une empreinte — tout ça, l'opérateur le fait lui-même à coût quasi nul.
  **Et on GROUPE : un seul appel porte toutes les questions en attente, jamais une question par appel.**
  Une question qui peut attendre la prochaine relecture groupée attend.

- **LA BASCULE À 98 % DE CLAUDE** (propriétaire 24/09 16:35). Quand l'hebdomadaire Claude atteint **98 %** :
  **Codex prend la gestion et la relecture, GLM construit, l'opérateur Claude se tait jusqu'au renouvellement**
  (vendredi 17:00 Bangkok). Ce n'est pas un basculement automatique de moteur — c'est un **changement de régime**.
  **CE QUI DOIT ÊTRE FAIT AVANT D'ATTEINDRE 98 %, pas après :** charger la file de travail détachée de TOUT ce
  qui doit tourner sans opérateur — lots de réparation enchaînés, bancs, mutation, relectures groupées, bilan —
  parce qu'une fois Claude épuisé, **plus personne ne peut décider du lot suivant**. Un opérateur qui atteint
  98 % sans avoir chargé la file laisse la factory à l'arrêt, pas en autonomie.
  **Seuil d'alerte : 95 %.** À 95 %, arrêter toute autre activité et écrire le programme autonome.

- **LE TEMPS MACHINE EST GRATUIT** (bancs, mutation, répétition, sondes) : y basculer tout ce qui peut l'être.
  Une vérification qui ne consomme aucun moteur n'a aucune raison d'être rationnée.


- Les quotas se lisent **par l'API** (`factory_v2/quota_live.py`, toutes les 20 min par le moniteur) — on ne demande
  plus de capture au propriétaire ; une capture reçue reste un ordre et met à jour le routage
  (`factory/bin/owner_routing_policy.py`) **immédiatement**.
- Calculer le débit (% par heure et par builder) et fixer le parallélisme **avant** la coupure.
- Un moteur refusé (≥ 90 %) ne bride pas les autres ; les relectures respectent le garde-quota.

## 4b. Lancement des moteurs — pannes CONNUES, jamais à rediagnostiquer

Ces pannes ont déjà coûté des lancements. Les redécouvrir est du gaspillage, pas du diagnostic.
**Avant de lancer un moteur, appliquer ces règles ; après, vérifier la cause de fin avant de conclure.**

### GLM / opencode — deux pannes connues, deux parades obligatoires

**PANNE 1 — coupure pour longueur (`"reason": "length"` dans le dernier `step_finish`).**
Une consigne trop grosse fait réfléchir GLM puis le coupe **avant qu'il n'écrive quoi que ce soit** : sortie
utile de quelques dizaines de tokens, aucun fichier, aucun rapport. Constaté le 24/09 à 02:13 (banc v3) puis
**re-constaté à 05:37** (expérience R2) — deux lancements perdus pour la même cause.
**Parade obligatoire, éprouvée :** découper en **3 passes courtes maximum**, enchaînées par un petit script shell,
et mettre dans CHAQUE consigne, mot pour mot : *« écris chaque fichier IMMÉDIATEMENT avec l'outil d'écriture,
un fichier à la fois, ≤ N lignes ; ne rédige AUCUNE réponse longue ; ne mets jamais plusieurs fichiers dans une
seule réponse »*. Terminer chaque passe par l'écriture d'un `_DONE_<i>.txt` listant les fichiers écrits : c'est
le seul moyen de savoir si la passe a abouti sans lire le flux.

**PANNE 2 — consigne vide.** Passer la consigne par une variable non transmise au sous-shell donne un prompt
**vide** : le moteur tourne, coûte du quota, ne fait rien. Constaté le 24/09 vers 02:13.
**Parade obligatoire :** la consigne vit dans un **fichier**, lue à l'appel par `"$(cat "$B/P<i>.txt")"`.
Jamais une variable, jamais un heredoc inline dans la commande de lancement.

### PANNE 3 — TOUT CE QUI EST LANCÉ DEPUIS LA SESSION MEURT AVEC ELLE
**Constat 24/09, deux fois : à 06:15 puis à 10:00.** La session Claude s'arrête (crash, redémarrage, fin de
contexte) et **tout son arbre est tué** : moteurs en cours, bancs, mesures. `nohup &` NE SUFFIT PAS — le
processus reste dans la session du shell et part avec elle. Coût mesuré : **3 h 45 perdues**, un lot de
réparations qui n'a jamais produit une seule passe, une mesure de mutation arrêtée à 36/60.

**PARADE OBLIGATOIRE — ne plus jamais lancer un moteur ou une mesure directement depuis le shell de session :**
1. Écrire le travail dans une **file de travail** (`.context/<chantier>/file_travail.sh`) qui enchaîne TOUT
   ce qui doit tourner, avec un journal horodaté à chaque étape.
2. La lancer par `.context/<chantier>/detache.py`, qui fait un `Popen(..., start_new_session=True)` :
   le processus obtient **sa propre session**, il survit à la mort de la session Claude.
3. Vérifier immédiatement après : `ps -eo pid,sess,command | grep file_travail` doit montrer le processus.
4. À la reprise de session, **premier réflexe** : lire le journal de la file, pas relancer à l'aveugle.

**La règle derrière la parade :** un travail qui dure plus longtemps que la conversation ne doit pas dépendre
de la conversation. Si la file doit survivre à un redémarrage de la machine, c'est le rôle de `factoryd`, pas
d'un lancement à la main — et « seul factoryd lance des moteurs » (§2) redevient alors la bonne règle.

### PANNE 4 — RIEN DE DURABLE NE VIT DANS `/tmp`
**Constat 24/09 : macOS a vidé `/tmp` au redémarrage de la machine et effacé la copie épinglée du banc de
mutation. Coût : ~2 h de mesure perdues, et le plantage n'est apparu qu'à la fin (`FileNotFoundError`).**
C'est la même famille que les pannes 1 à 3 : **quelque chose de temporaire qu'on avait pris pour durable.**

**PARADE OBLIGATOIRE :** tout arbre épinglé, toute copie de travail, tout résultat intermédiaire vit sous
`.context/<chantier>/` — jamais dans `/tmp`, `/var/folders`, ni aucun répertoire temporaire du système.
`.context/` est ignoré par git : c'est durable sans polluer le dépôt. Les scripts ne doivent contenir aucun
chemin `/tmp` en dur ; une mesure de plusieurs heures qui repose sur un répertoire temporaire est une mesure
qu'on refera.

**La règle générale derrière les pannes 1 à 4 :** avant de lancer quoi que ce soit de long, se demander
**« qu'est-ce qui, là-dedans, ne survit pas à un redémarrage ? »** — la session (panne 3), le répertoire
temporaire (panne 4), la consigne passée en variable (panne 2). Ce qui ne survit pas doit être écrit sur
disque, sous `.context/`, avant de commencer.

### PANNE 5 — L'AUTHENTIFICATION DES MOTEURS, DEUX PROBLÈMES DIFFÉRENTS À NE PAS CONFONDRE
**Constat 24-25/09 : trois arrêts de chantier pour « authentification », deux causes distinctes.**

**(a) Le noyau exige une clé en variable d'environnement ; le CLI, lui, est authentifié autrement.**
`factory_v3` refuse un moteur si `ZAI_API_KEY` / `CODEX_API_KEY` manque (`REFUS rc=3 — authentification absente`),
alors que `opencode` et `codex` fonctionnent par leur propre magasin d'identifiants. Le refus est CORRECT —
le noyau refuse avant de dépenser — mais il bloque un moteur qui marche par ailleurs.
**PARADE OBLIGATOIRE, rejouable :** `sh .context/<chantier>/identifiants.sh` reconstruit `.env_reel`
(droits 600) depuis les magasins des CLI, et **dit ce qui manque**. Tout lanceur de vrai run fait
`. "$B/.env_reel"` avant d'appeler `run.py`. À relancer dès qu'un moteur est refusé pour authentification —
**ne jamais rediagnostiquer, ne jamais aller chercher une clé à la main.**

**(b) Codex sur un plan ChatGPT n'a PAS de clé API : il est en OAuth.** Son profil
(`factory_v3/moteurs/codex.json`) interdit par conception de copier `auth.json`. **Conséquence à assumer :
Codex ne peut pas être moteur de travail dans la factory tant qu'il n'y a pas de vraie clé API.**
Il reste parfaitement utilisable comme relecteur en session. Ce n'est pas un bug à réparer, c'est une
limite à connaître — mesurée le 25/09 à 10:02 (`vrai run moteur=codex rc=3 fix=NON`).

**(c) Claude Code lui-même peut se déconnecter** (`loggedIn: false`) et emporter la session en cours.
Remède : `claude auth login` dans le terminal du workspace, puis nouveau chat. **La vraie protection n'est
pas d'éviter la déconnexion, c'est la panne 3 : rien de long ne doit dépendre de la session.**

### Vérification obligatoire APRÈS chaque lancement
Avant d'annoncer quoi que ce soit : lire la **cause de fin** dans le dernier `step_finish` du JSONL et vérifier
la présence du fichier de rapport attendu. « Le moteur a fini » n'est pas « le moteur a produit ».
Un moteur qui finit sans son rapport = **panne connue jusqu'à preuve du contraire**, à rapprocher de cette liste
avant tout autre diagnostic.

### Codex
Le nom de modèle doit venir de `~/.codex/config.toml` ou de `factory_v3/moteurs/codex.json`, jamais de mémoire :
un nom inventé échoue en 2 s (`model is not supported when using Codex with a ChatGPT account`). Constaté à 03:10.

## 5. Rapport au propriétaire — français simple, sans jargon

- Tableau **construit / en construction / suivant** en premier (seuls les jalons `COMPLETE` comptent).
- Puis : compteurs de readiness (calculés), minutes perdues vs utiles, poids du code
  (`dispatcher_v3.py`, `factoryd.py`, `runtime.py`, total), interventions et pourquoi.
- Un rapport par jour + alerte immédiate si un critère casse. Jamais de promesse hors chiffres.
- Heure locale Bangkok (UTC+7). **Toute heure citée dans un rapport se LIT à l'horloge (`TZ=Asia/Bangkok date`),
  jamais de tête.** Panne répétée : la session du 23/09 a annoncé « 05:00 » et « 05:40 » sortis de sa tête
  (propriétaire : « il est 2h17 oui pas 5h ») ; la session du 24/09 a recommencé et dérivé d'environ 90 minutes
  sur plusieurs contrôles. Une heure inventée décrédibilise tous les autres chiffres du rapport.

## 5b. Gel du code maison (décision propriétaire 22/09 16:00)

- **Rien n'est ajouté à la factory.** Pas de nouveau juge, garde, banc, script ; un jalon qui augmente le nombre
  de lignes de `factory/` + `factory_v2/` est refusé.
- **Comparer puis prendre, pas s'inspirer.** Quatre comparaisons à l'état de l'art existent déjà
  (`FACTORY_VENDORED_CODE.md` 26/07, `factory_v2/audit/FACTORY_VS_UPSTREAM_MATRIX.json` 07/09, audit relu 18/09,
  recherche `research/deep_research/FACTORY_SCALE_LOCAL_2026-09` 22/09) et n'ont jamais été appliquées : les
  décisions ADAPT (mini-swe-agent, SWE-ReX, Temporal, Spec Kit, harness.lol, Harbor, Omnigent, llama.cpp) se
  réalisent en **mettant le code upstream dans l'arbre et en supprimant le nôtre**.
- **Un jalon « ADAPT » n'est réglé que lorsque du code maison a disparu** (lignes retirées > lignes ajoutées, mesuré
  par le juge). Un document de comparaison de plus n'est pas un livrable.
- L'opérateur (Claude en session) ne code plus dans la factory : il lit, mesure, rapporte, et n'intervient qu'en cas
  de blocage > 1 h, consigné.
- **Règle de marche (décision propriétaire 24/09/2026 04:40) : toute faille se ferme d'abord en RETIRANT une
  autorisation ou du code. Ajouter des lignes est le dernier recours, et il se justifie.** Établi par mesure :
  les trois protections les plus fortes du noyau v3 coûtent zéro ligne (juge hors de portée du candidat sous
  identité séparée, accès inutiles retirés, une seule photographie immuable évaluée) et une coûte moins que
  zéro (supprimer le détecteur temporel, fausse garantie). Un correctif qui fait grossir le noyau est refusé
  tant qu'une suppression ou un retrait d'autorisation n'a pas été essayé et écarté par écrit.

## 6. Définition de « ça marche »

`LONG_RUN_PROOF_2` : 48 h sans commit ni intervention, compteurs de readiness à zéro **par mesure**,
benchmark apparié rejoué après chaque promotion, poids du code qui baisse. Avant cette preuve,
la factory « tient », elle ne « fonctionne » pas — ne pas dire le contraire.
