# jobot

Agrège des offres depuis plusieurs sites d'emploi, les note par rapport à ton
profil, puis prépare pour chacune un **dossier de candidature** : un CV adapté
à l'offre et une lettre de motivation. Tu n'as plus qu'à cliquer sur *Postuler*,
qui t'ouvre l'offre sur son site — tu y déposes les deux PDF.

**jobot ne remplit aucun formulaire de candidature.** C'est un choix : chaque
plateforme de recrutement a le sien, et aucune automatisation ne tient sur
l'ensemble. Le temps qu'elle faisait perdre est réinvesti là où il rapporte —
ratisser plus de sites, et soigner le dossier envoyé.

Une exception : quand l'offre publie une vraie adresse de contact, la
candidature part par email depuis jobot, pièces jointes comprises. Rien ne part
sans validation explicite ; la validation peut être désactivée au lancement
d'une recherche pour un mode autonome, choix explicite confirmé dans
l'interface.

## L'interface web

```bash
uv run jobot ui        # ouvre http://127.0.0.1:8321 dans ton navigateur
```

C'est la façon la plus simple d'utiliser jobot, pensée pour fonctionner sans
toucher au terminal une fois lancée :

0. **Pas encore de LLM ou de SMTP configuré ?** Le bouton **Réglages** (en
   haut à droite, ou en cliquant directement sur les indicateurs LLM/SMTP)
   ouvre un formulaire pour les renseigner sans toucher au terminal —
   fournisseur LLM, modèle, clé API, puis hôte/identifiants SMTP. Écrit dans
   `.env` (jamais versionné) et actif immédiatement, sans redémarrer jobot.
1. **Pas encore de CV maître ?** L'indicateur **CV** dans la nav (ou le
   bouton *Importer mon CV* affiché tant qu'aucun CV n'existe) ouvre une
   modale pour l'importer — fichier HTML/texte ou copier-coller, extraction
   faite par le LLM configuré. **Cet indicateur reste cliquable en
   permanence** : tu peux ré-importer un CV à tout moment pour remplacer
   l'actuel (une confirmation te le demande, l'écrasement est immédiat et
   sans historique). Relis toujours `data/master-cv.json` après un import.
2. **Choisis tes critères** : département(s), type de contrat (alternance,
   CDI, CDD, stage, intérim), intitulés de poste en saisie libre
   (« technicien support », « chargé de recrutement »…) et mots-clés.
   Tes derniers critères sont mémorisés (`data/ui-params.json`).
3. **Lance.** Tout s'enchaîne automatiquement : récupération, filtrage,
   scoring LLM, génération des CV adaptés — jusqu'à l'écran de validation.
4. **Récupère tes dossiers.** Chaque offre a son CV adapté et sa lettre.
   - *Trier et filtrer* : au-dessus de la liste, une puce par job board
     présent dans les résultats (avec son nombre d'offres) masque ou réaffiche
     la source d'un clic, et le menu **Trier par** réordonne par score, par
     date de publication, par entreprise ou par intitulé. Tout se fait
     instantanément côté navigateur, sans relancer de recherche.
   - *Canal email* : tu vois le destinataire, l'objet, le corps et les pièces
     jointes avant de confirmer l'envoi, qui part depuis jobot.
   - *Canal site* : « Ouvrir le dossier » affiche les deux PDF, le texte de la
     lettre à relire, et le lien vers la page de candidature. Tu déposes, puis
     tu cliques sur *J'ai déposé ma candidature* — jobot ne peut pas le
     deviner à ta place.
5. **Retrouve tes candidatures envoyées** sur une page dédiée, **Candidatures**
   (lien dans la nav, `/candidatures`) — séparée de la recherche pour ne pas
   l'encombrer. Toutes les offres marquées `applied`, les plus récentes en
   premier, avec le CV et la lettre effectivement envoyés.

**Le mode autonome** (interrupteur « validation humaine » désactivé) envoie
tout seul les candidatures email dont le score dépasse le seuil choisi.
L'interface demande une confirmation globale au lancement. Les offres du canal
`site` t'attendent de toute façon : c'est toi qui déposes.

Le CLI ci-dessous reste disponible pour un usage étape par étape.

## Le principe

```
fetch → filtre → score → [TOI : review] → generate → send / [TOI : dépôt]
 auto     auto    auto      décision        auto      email / sur le site
```

1. **`fetch`** — récupère les offres depuis France Travail, l'APEC et La Bonne
   Alternance, et écarte les doublons entre sources.
2. **filtre** — élimine les offres hors périmètre (département, mots-clés,
   type de contrat) avant de payer le moindre appel LLM. Un mot-clé ne compte
   que si **l'intitulé** de l'offre en porte au moins un mot : la description
   présente aussi l'employeur, et « cybersécurité » y apparaît dans une offre
   de chargé de développement RH dès que la boîte est une ESN spécialisée.
3. **`score`** — un LLM note chaque offre restante par rapport à ton profil et
   choisit, dans ton CV, les éléments à mettre en avant.
4. **`review`** — *toi seul* décides quelles offres notées passent à la suite.
5. **`generate`** — construit le dossier : un CV PDF adapté à l'offre à partir
   de ta sélection validée, et une lettre de motivation.
6. **`send` / `kit`** — envoie l'email (avec confirmation), ou te donne les deux
   PDF et le lien vers l'offre pour que tu déposes toi-même.

Chaque offre progresse dans ces statuts, stockés en base : `new` →
`filtered_out` *ou* `scored` → `queued` *ou* `skipped` → `applied`.

## Ce qu'il faut avant de commencer

- **Python 3.11+** et [`uv`](https://docs.astral.sh/uv/)
- Des identifiants de sources, tous gratuits, et tous facultatifs pris un par
  un — l'APEC n'en demande aucun, donc jobot tourne sans compte nulle part :
  - [francetravail.io](https://francetravail.io), avec une souscription à
    l'API **Offres d'emploi v2**
  - [api.apprentissage.beta.gouv.fr](https://api.apprentissage.beta.gouv.fr)
    pour La Bonne Alternance (alternance uniquement)
- Un LLM au choix — un modèle local suffit, aucune clé n'est obligatoire
  (voir *Choix du LLM* plus bas)
- *Optionnel* : des identifiants SMTP si tu veux l'envoi automatique par email
- Le navigateur Playwright, installé en une commande (voir plus bas)

## Installation

```bash
uv sync
cp .env.example .env
```

Édite `.env` :
1. `FT_CLIENT_ID` / `FT_CLIENT_SECRET` — récupérés sur francetravail.io
   (Mon espace > Mes applications)
2. `LBA_API_KEY` — jeton généré sur api.apprentissage.beta.gouv.fr
3. `JOBOT_LLM_*` — voir *Choix du LLM*
4. `JOBOT_DEPARTEMENTS`, `JOBOT_MOTS_CLES`, etc. — tes critères de recherche
5. `JOBOT_SOURCES` — sources interrogées, par défaut les trois. Mets `apec`
   seul pour démarrer sans créer de compte nulle part.

Tout cela se règle aussi depuis l'écran **Réglages** de l'interface, sans
toucher au terminal.

Puis crée ton CV maître :

```bash
uv run jobot cv import mon-cv.html   # extraction automatique par un LLM
# ou : uv run jobot cv init          # partir d'un modèle vierge à remplir
uv run jobot cv check                # valider et lister les id disponibles
```

**Relis toujours le résultat d'un `cv import`** — c'est une extraction
automatique, pas un rendu garanti fidèle.

Enfin, installe le navigateur (Playwright ne sert plus qu'au rendu PDF du CV
et de la lettre) :

```bash
uv run playwright install chromium   # ~150 Mo, une seule fois
```

## Utilisation

Dans l'ordre où tu t'en sers réellement :

```bash
uv run jobot fetch --jours 7      # récupère, dédoublonne, filtre
uv run jobot reparse              # re-route les offres en base (sans appel API)
uv run jobot stats                # répartition par statut et par canal
uv run jobot list --statut new    # les offres retenues par le filtre
uv run jobot list --statut filtered_out   # et pourquoi les autres ont sauté

uv run jobot score                # note les offres 'new' + sélectionne le CV pertinent
uv run jobot list --statut scored # triées par score décroissant
uv run jobot show <id>            # détail complet d'une offre (score, description, lien)

uv run jobot review               # TOI : garder ou écarter chaque offre notée

uv run jobot generate <id>        # CV adapté -> out/<id>.html + out/<id>.pdf
uv run jobot send                 # SIMULATION des envois email (rien ne part)
uv run jobot send --envoyer       # envoi réel, avec confirmation listant les destinataires
uv run jobot kit <id>             # dossier complet : CV + lettre + lien pour postuler
uv run jobot postule <id>         # marque une offre déposée à la main
```

`review`, `send` et `kit` s'appuient sur le canal de candidature de chaque
offre (voir plus bas) : `send` ne traite que le canal `email`, `kit` prépare
le dossier de n'importe quelle offre scorée.

## Le CV maître

Ton CV existe une seule fois, en donnée structurée : `data/master-cv.json`.
Ce fichier est **gitignoré** — il contient ton nom, ton téléphone, ton email —
et n'est jamais versionné. `data/master-cv.example.json` documente le format
avec un CV fictif.

Chaque élément que le LLM pourra choisir de mettre en avant ou non porte un
`id` stable : tags de compétences, bullets d'expérience, projets et leurs
bullets. Ton identité, ta formation, tes langues et tes centres d'intérêt
sont fixes — toujours inclus, jamais sélectionnés.

C'est la pièce centrale du garde-fou anti-hallucination : `MasterCV.selectable_ids()`
(dans `cv.py`) liste tous les `id` valides, et **tout `id` renvoyé par le LLM qui
n'appartient pas à cet ensemble est rejeté avant d'être stocké**. Le LLM ne
génère jamais de texte de CV — il choisit parmi ce qui existe déjà. Zéro
invention possible par construction, quelle que soit la fiabilité du modèle
utilisé.

`jobot cv import` réutilise le LLM pour convertir un CV existant (HTML ou
texte brut) en `master-cv.json`, en découpant expériences et projets en
bullets autonomes et en générant les `id`.

## Le scoring

`jobot score` demande au LLM, pour chaque offre au statut `new` : un score de
pertinence 0-100 *spécifique à ton profil* (pas juste "est-ce un poste
tech ?"), une justification en une phrase, et la liste des `id` du CV maître
à mettre en avant pour cette offre précise.

Le scoring est non déterministe : une même offre peut varier de quelques
points d'un run à l'autre. C'est un outil de tri, pas une note absolue —
`jobot review` reste la décision finale.

## La génération du CV

`jobot generate <id>` prend la sélection stockée par le scoring et construit
un CV adapté. La sélection **met en avant, elle ne supprime pas** : un CV
amputé de ses compétences pénalise le matching ATS et donne une image
appauvrie du candidat.

- **Compétences** : toutes conservées ; dans chaque catégorie, les tags
  sélectionnés passent en tête.
- **Expériences** : toujours affichées en entier (ton identité professionnelle
  ne change pas d'une offre à l'autre), seuls les bullets sont filtrés — si
  aucun bullet d'une expérience n'a été sélectionné, ils sont tous affichés
  plutôt que de laisser une expérience vide.
- **Projets** : tous conservés, les sélectionnés en tête ; les bullets d'un
  projet sont filtrés sur la sélection, avec repli sur tous les bullets si
  aucun n'a été retenu.

Le rendu passe par un template Jinja2 (`templates/cv.html.jinja`) puis par
Playwright en mode headless pour produire le PDF.

## Revue et envoi

`jobot review` affiche score, justification et canal pour chaque offre
notée, et attend ta décision (garder / écarter / voir le détail). C'est le
seul endroit où une offre passe de `scored` à `queued` — rien ne part sans
avoir été validé ici.

**Canal `email`** — `jobot send` construit l'email (CV en pièce jointe) et,
**par défaut, ne fait que simuler** : rien n'est envoyé, tu vois exactement ce
qui partirait. L'envoi réel exige `--envoyer` *et* une confirmation qui liste
les destinataires. `Reply-To` pointe toujours vers ton adresse personnelle,
même si `SMTP_FROM` diffère.

Le corps de l'email vient de `templates/email.txt.jinja` et **n'est jamais
généré par le LLM** — une phrase inventée dans une lettre de motivation
partirait chez un vrai recruteur. Édite ce fichier pour personnaliser le
message.

**Canal `site`** — jobot prépare le dossier et s'arrête là. L'écran *Ouvrir
le dossier* donne le CV adapté, la lettre, et le lien direct vers la page de
candidature ; tu déposes toi-même, puis tu confirmes pour que l'offre passe en
`applied`.

C'est un renoncement assumé. jobot a remplacé les formulaires par Playwright
pendant un temps : détection heuristique des champs, upload du CV, clic sur
envoyer, avec reprise après les murs d'authentification. Chaque plateforme de
recrutement ayant son propre formulaire, ses propres libellés et son propre
captcha, l'heuristique ne tenait jamais bien longtemps sur plus de quelques
sites. Le temps rendu par cet abandon va à ce qui passe à l'échelle, lui :
brancher plus de sources, et soigner le dossier.

## La lettre de motivation

C'est le seul endroit où un LLM écrit une prose qui finira sous les yeux d'un
recruteur. Le garde-fou du CV — le modèle choisit parmi des `id` existants,
donc il ne peut rien inventer — ne se transpose pas à du texte libre. Trois
contraintes le remplacent (`letter.py`) :

1. **Matière première réduite.** Le modèle ne reçoit pas le CV maître entier,
   mais uniquement les éléments que le scoring a retenus pour cette offre, plus
   l'identité et la formation. Il recombine des faits, il n'en découvre pas.
2. **Structure imposée.** Sa sortie n'est pas une lettre mais trois paragraphes
   courts (accroche, adéquation, motivation). L'en-tête, l'objet, la formule
   d'appel, la formule de politesse et la signature viennent du template
   `templates/letter.html.jinja` — jamais du modèle.
3. **Relecture obligatoire, et gratuite.** Sur le canal `site`, c'est toi qui
   déposes le fichier : tu lis donc la lettre par construction. `suspect_terms()`
   te signale en plus les noms propres et les affirmations chiffrées absents à
   la fois de ton CV et de l'annonce — un employeur, un outil ou une durée
   inventés s'y logent en pratique.

Ce troisième point est la raison pour laquelle générer une lettre est devenu
acceptable : tant que jobot envoyait tout seul, une phrase inventée partait
sans que personne ne la lise. Ce n'est plus le cas.

Le détecteur reste volontairement conservateur : un mot capitalisé en début de
phrase est ignoré, parce que le français y met surtout des verbes. Il indique
où regarder, il ne valide rien — une lettre sans terme signalé peut rester
fausse.

Le bouton **Réécrire la lettre** relance la génération autant de fois que tu
veux ; le corps de l'email de candidature, lui, reste un template fixe
(`templates/email.txt.jinja`), jamais généré.

## Choix du LLM

Aucun fournisseur n'est imposé : tout se règle dans `.env`, sans toucher au
code — directement en éditant le fichier, ou depuis l'écran **Réglages** de
l'UI (voir *L'interface web*), qui écrit dans ce même fichier et recharge la
config à chaud. LM Studio, Ollama, vLLM, OpenAI et OpenRouter parlent tous le
format d'API d'OpenAI, donc un seul client les couvre tous.

| `JOBOT_LLM_PROVIDER` | `JOBOT_LLM_BASE_URL` | Clé requise |
|---|---|---|
| `lmstudio` | `http://localhost:1234/v1` (implicite) | non |
| `ollama` | `http://localhost:11434/v1` (implicite) | non |
| `openai` | implicite | oui |
| `openrouter` | implicite | oui |
| `gemini` | — (SDK natif) | oui |
| `openai_compat` | à fournir | selon l'hôte |

Tous les serveurs ne supportent pas la sortie structurée, d'où une
dégradation en cascade dans `llm/openai_compat.py` : `json_schema` strict →
mode JSON → consigne dans le prompt, avec un parsing tolérant aux
```` ```json ```` et au bavardage. Le mode qui a fonctionné est mémorisé pour
les appels suivants.

Un petit modèle local respectera moins bien le schéma, mais **il ne peut pas
pour autant inventer une ligne de CV** : la validation des `id` (voir *Le CV
maître*) reste le garde-fou, quel que soit le modèle.

## Les sources d'offres

`JOBOT_SOURCES` choisit lesquelles sont interrogées, dans l'ordre. Chacune est
appelée une fois par combinaison département × mot-clé.

| Source | Identifiants | Ce qu'elle apporte |
|---|---|---|
| `francetravail` | `FT_CLIENT_ID` / `FT_CLIENT_SECRET` (gratuits) | API officielle, annonce complète, adresse de contact quand elle existe |
| `apec` | aucun | offres cadres absentes de France Travail |
| `labonnealternance` | `LBA_API_KEY` (gratuite) | alternance uniquement — offres du service public de l'apprentissage |

Ajouter des sources est désormais l'axe principal de jobot : c'est le côté
*lecture* du problème, et il passe à l'échelle là où l'automatisation du dépôt
ne le faisait pas.

### La dédup entre sources

Une même annonce ressort souvent sur plusieurs sites — France Travail republie
chez La Bonne Alternance, et réciproquement. La clé primaire `source:id` ne les
rapproche pas. `Offer.dedup_key` normalise l'intitulé et l'employeur (accents,
ponctuation, formes juridiques, mentions `H/F`) pour les reconnaître : à
l'insertion, une offre dont le jumeau est déjà en base est ignorée. Chaque
doublon évité, c'est un scoring, un CV et une lettre de moins — donc deux
appels LLM économisés.

Sans employeur, la clé retombe sur l'id : deux annonces anonymes au même
intitulé sont trop souvent deux vraies offres distinctes pour être confondues.

### La Bonne Alternance

L'API est gratuite mais plus anonyme : il faut un compte sur
[api.apprentissage.beta.gouv.fr](https://api.apprentissage.beta.gouv.fr) et un
jeton généré depuis ton profil, passé en `Authorization: Bearer`. Trois choses
à savoir :

- Elle ne renvoie que de l'alternance : le pipeline ne l'interroge pas quand
  la recherche porte sur un autre type de contrat, plutôt que de payer des
  requêtes pour rien.
- La réponse sépare `jobs` (de vraies annonces) et `recruiters` (des
  entreprises jugées susceptibles de recruter, sans annonce derrière). Seules
  les `jobs` sont retenues : les secondes n'ont ni intitulé ni description, et
  rempliraient la base d'offres fantômes que le scoring ne saurait pas évaluer.
- **Elle ne cherche pas en plein texte, seulement par code ROME** — et c'est le
  point à comprendre sur cette source. Balayer un département puis filtrer les
  mots-clés localement ne donne presque pas les mêmes offres que le site
  officiel : lui traduit d'abord l'intitulé cherché en codes ROME. Mesuré sur
  « technicien support » : 102 offres sous les codes ROME correspondants, dont
  **99 que jobot n'avait jamais vues**. jobot interroge donc le même service
  d'auto-complétion métier que le site (`METIER_URL`) pour convertir chaque
  intitulé en codes ROME, puis réunit ces résultats avec le balayage
  départemental. Aucune table codée en dur : le référentiel reste celui de La
  Bonne Alternance.
- **Elle plafonne toute réponse à 450 offres et n'offre aucune pagination.**
  Interroger un département renvoyait donc une tranche arbitraire, et les
  offres au-delà étaient invisibles. jobot interroge maintenant chaque niveau
  de diplôme séparément et réunit les résultats, ce qui découpe la réponse en
  tranches qui tiennent sous le plafond. Comme l'API ignore de toute façon les
  mots-clés, le résultat est mis en cache pour la durée de la recherche : six
  intitulés de poste ne déclenchent plus six fois la même requête.

### L'APEC

**L'APEC n'a pas d'API publique documentée** : `sources/apec.py` utilise les
endpoints du site lui-même. Trois conséquences à connaître :

- Un anti-bot protège le domaine, donc le client charge d'abord une page du
  site pour récupérer ses cookies. Si une recherche renvoie 403, c'est lui —
  espacer les appels suffit généralement.
- Le détail d'une offre est bloqué par cet anti-bot, y compris depuis un vrai
  navigateur. **jobot ne stocke donc que l'extrait de ~280 caractères renvoyé
  par la recherche.** Le scoring travaille sur ce résumé et le sait (il lui est
  précisé de ne pas pénaliser l'offre pour ce qui manque) ; le texte complet
  reste à un clic, sur la page de l'offre.
- Comme la description est partielle, le filtre par mots-clés ne s'applique
  qu'à **l'intitulé** de ces offres, jamais à leur texte : l'extrait est
  tronqué, l'intitulé ne l'est pas (`Offer.has_full_description`, testé dans
  `filters.py`). Ne rien filtrer du tout laissait passer *toutes* les offres
  APEC vers le scoring, y compris assistant trésorier ou gestion locative.

Ces endpoints n'étant pas contractuels, ils peuvent changer sans préavis :
une source qui échoue affiche un avertissement, les autres continuent.

## Le canal de candidature

Chaque offre est routée automatiquement à partir des champs renvoyés par
l'API. Deux pièges de l'API France Travail sont traités ici :

- **L'URL à utiliser n'est pas `origineOffre.urlOrigine`.** Pour une offre
  venue d'un partenaire (le cas le plus fréquent), ce champ mène à la fiche
  sur `candidat.francetravail.fr`, dont le bouton « Postuler » ne fait que
  rediriger vers le site partenaire. `origineOffre.partenaires[].url` donne ce
  lien final directement — c'est lui que jobot suit, pour t'envoyer sur le vrai
  formulaire plutôt que sur une page intermédiaire.
- **`contact.courriel` ne contient pas toujours une adresse.** France Travail
  y met parfois une phrase (« Pour postuler, utiliser le lien suivant :
  https://… »). Router ces offres sur le canal email ferait tenter un envoi
  SMTP vers un destinataire absurde : `clean_email()` ne garde que ce qui est
  réellement une adresse, le reste bascule sur le canal `site`.

| Canal | Détecté par | Ce qui se passe |
|---|---|---|
| `email` | `contact.courriel`, si c'est bien une adresse | envoi depuis jobot — SMTP, CV et lettre joints, confirmation avant envoi |
| `site` | `contact.urlPostulation`, l'URL du partenaire, ou `origineOffre.urlOrigine` | dossier préparé, lien vers l'offre, dépôt et confirmation par toi |
| `unknown` | aucun des deux | écartée par défaut |

Ni l'APEC ni La Bonne Alternance ne publient d'adresse de contact : **toutes
leurs offres arrivent sur le canal `site`**.

`jobot stats` donne la répartition.

## Structure du code

Dans l'ordre où les données y circulent :

| Fichier | Rôle |
|---|---|
| `config.py` | Config centralisée (`.env` → objet `settings`), messages d'erreur pour les identifiants manquants |
| `models.py` | `Offer`, `Channel`, `Status` — schéma d'une offre, cycle de vie, clés de dédup |
| `pipeline.py` | Orchestration du pipeline complet (fetch → filtre → score → génération → envoi), presets domaines/départements, état d'exécution partagé avec l'UI |
| `web.py` | API FastAPI locale de `jobot ui` (recherche, validation, dossiers, import CV) |
| `webui/` | L'interface web, design Electric Volt : `index.html` (recherche + dossiers), `candidatures.html` (candidatures envoyées), `style.css` partagé |
| `sources/francetravail.py` | Client OAuth2 + pagination pour l'API France Travail |
| `sources/apec.py` | Client de la recherche apec.fr (pas d'API publique — voir *Les sources d'offres*) |
| `sources/labonnealternance.py` | Client de l'API du service public de l'apprentissage |
| `filters.py` | Filtrage à règles, sans appel LLM |
| `db.py` | Toute l'écriture SQLite (dédup, upsert, transitions de statut, migrations) |
| `cv.py` | Modèles du CV maître + `selectable_ids()`, le garde-fou anti-hallucination |
| `llm/` | Abstraction fournisseur : `base.py` (contrat), `gemini.py`, `openai_compat.py` |
| `scoring.py` | Appelle le LLM, valide sa sélection d'`id` contre `selectable_ids()` |
| `letter.py` | Rédige la lettre à partir de la seule sélection du scoring, et signale les termes suspects |
| `render.py` | Filtre le CV selon la sélection, rend le HTML (Jinja2) puis le PDF (Playwright) |
| `mailer.py` | Compose et envoie l'email de candidature |
| `templates/` | `cv.html.jinja`, `letter.html.jinja` (mises en page), `email.txt.jinja` (texte de l'email — jamais généré par le LLM) |
| `cli.py` | Toutes les commandes `jobot ...`, assemble les modules ci-dessus |

## Notes techniques

- **Pagination France Travail** : l'API plafonne à 150 résultats par appel et
  ~1150 au total par requête (`range=0-149`, puis `150-299`…). HTTP 206 =
  il en reste, 200 = fini.
- **Pagination APEC** : `range` est plafonné à 100 — au-delà l'API retombe
  silencieusement sur 20. Les résultats étant triés par date décroissante, le
  filtre `--jours` s'arrête à la première offre trop ancienne.
- **Quota La Bonne Alternance** : 60 appels par minute et par jeton.
- **Croisement département × mot-clé** : aucune de ces API ne fait de OU sur
  les mots-clés, donc jobot émet une requête par combinaison et dédoublonne
  côté client.
- **Dédup** : clé primaire `source:id`, plus `dedup_key` entre sources (voir
  *Les sources d'offres*). Un `content_hash` détecte les offres réécrites par
  l'employeur et les repasse en `new` pour un nouveau scoring — la lettre est
  alors effacée, puisqu'elle décrivait l'ancienne version de l'annonce.
- **Filtrage avant LLM** : chaque offre écartée par `filters.py` est un appel
  LLM économisé.
- **`user_id`** : la table `offers` porte une colonne `user_id`, à `local` pour
  tout le monde tant que jobot tourne pour un seul candidat. Elle existe déjà
  pour qu'un passage en multi-utilisateur soit une migration de données plutôt
  qu'une réécriture des requêtes.

## Limites connues, pistes et Bugs

- Les offres APEC n'ont qu'une description tronquée (voir *Les sources
  d'offres*), ce qui rend leur scoring moins fin que celui des offres France
  Travail.
- La dédup inter-sources compare intitulé et employeur normalisés : deux
  annonces réellement distinctes au même intitulé chez le même employeur
  (deux villes, par exemple) seront confondues.
- Pas de relance automatique après candidature.
- Prochaines sources naturelles : les job boards ATS (Greenhouse, Lever,
  Ashby, SmartRecruiters, Workable, Recruitee) publient tous un JSON public
  sans authentification — il ne manque qu'une liste d'entreprises à parcourir.
- **La Bonne Alternance plafonne ses réponses à 450 offres, sans pagination**
  (voir `RESULT_CAP`). Contourné en partitionnant par niveau de diplôme, mais
  pas complètement : la tranche « niveau 5 » atteint elle-même le plafond sur
  Paris. La recherche par code ROME (voir *Les sources d'offres*) compense
  l'essentiel, puisqu'elle ne dépend plus du balayage départemental.
- Le nombre d'appels à La Bonne Alternance croît en départements × intitulés
  (≈ 50 pour 4 départements et 6 postes, contre une limite de 60/min).
  `_throttle` attend au lieu de prendre un 429, mais une recherche très large
  finira par être lente de ce seul fait.
- Les offres déjà scorées ne repassent jamais par les filtres : `reset_filtered`
  ne réexamine que les `filtered_out`, à dessein (une décision humaine ou un
  score ne doivent pas être annulés par un changement de critères). Après un
  durcissement des règles, les offres hors-sujet déjà scorées restent donc
  visibles jusqu'à ce qu'on les écarte à la main.
