# jobot

Automatise une recherche d'alternance/emploi : récupère des offres, les note
par rapport à ton profil, génère un CV adapté à chacune, et t'aide à
candidater — par email automatiquement, ou en te préparant le formulaire
quand un humain doit cliquer.

Par défaut, rien ne part sans validation explicite : le pipeline propose,
tu confirmes chaque envoi. La validation peut être désactivée au lancement
d'une recherche pour un mode entièrement autonome — c'est un choix explicite,
confirmé dans l'interface.

## L'interface web

```bash
uv run jobot ui        # ouvre http://127.0.0.1:8321 dans ton navigateur
```

C'est la façon la plus simple d'utiliser jobot, pensée pour fonctionner sans
toucher au terminal une fois lancée :

1. **Pas encore de CV maître ?** L'interface te propose d'importer ton CV
   existant (fichier HTML/texte ou copier-coller) — l'extraction est faite
   par le LLM configuré. Relis toujours `data/master-cv.json` ensuite.
2. **Choisis tes critères** : département(s), type de contrat (alternance,
   CDI, CDD, stage, intérim), type de poste (support technique, systèmes &
   réseaux, RH, compta…) et mots-clés libres. Tes derniers critères sont
   mémorisés (`data/ui-params.json`).
3. **Lance.** Tout s'enchaîne automatiquement : récupération, filtrage,
   scoring LLM, génération des CV adaptés — jusqu'à l'écran de validation.
4. **Valide.** Chaque candidature attend ta décision : pour le canal email,
   tu vois le destinataire, l'objet, le corps et le CV joint avant de
   confirmer l'envoi ; pour le canal formulaire, un navigateur s'ouvre et
   c'est toi qui cliques sur envoyer.

**Le mode autonome** (interrupteur « validation humaine » désactivé) envoie
tout seul les candidatures email dont le score dépasse le seuil choisi.
Deux garde-fous demeurent : l'interface demande une confirmation globale au
lancement, et les candidatures par formulaire ne sont **jamais** soumises
automatiquement — jobot ne clique pas à ta place sur un site de recruteur.

Le CLI ci-dessous reste disponible pour un usage étape par étape.

## Le principe

```
fetch → filtre → score → [TOI : review] → generate → send / assist
 auto     auto    auto      décision        auto      auto / [TOI]
```

1. **`fetch`** — récupère les offres depuis France Travail et l'APEC.
2. **filtre** — élimine les offres hors périmètre (département, mots-clés,
   type de contrat) avant de payer le moindre appel LLM.
3. **`score`** — un LLM note chaque offre restante par rapport à ton profil et
   choisit, dans ton CV, les éléments à mettre en avant.
4. **`review`** — *toi seul* décides quelles offres notées passent à la suite.
5. **`generate`** — construit un CV PDF adapté à l'offre, à partir de ta
   sélection validée.
6. **`send` / `assist`** — envoie l'email (avec confirmation) ou ouvre le
   formulaire dans un navigateur pour que tu termines toi-même.

Chaque offre progresse dans ces statuts, stockés en base : `new` →
`filtered_out` *ou* `scored` → `queued` *ou* `skipped` → `applied`.

## Ce qu'il faut avant de commencer

- **Python 3.11+** et [`uv`](https://docs.astral.sh/uv/)
- Un compte sur [francetravail.io](https://francetravail.io), avec une
  souscription à l'API **Offres d'emploi v2** (gratuit). L'APEC, la seconde
  source, ne demande aucun identifiant.
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
2. `JOBOT_LLM_*` — voir *Choix du LLM*
3. `JOBOT_DEPARTEMENTS`, `JOBOT_MOTS_CLES`, etc. — tes critères de recherche
4. `JOBOT_SOURCES` — sources interrogées, par défaut `francetravail,apec`.
   Mets `apec` seul pour démarrer sans compte francetravail.io.

Puis crée ton CV maître :

```bash
uv run jobot cv import mon-cv.html   # extraction automatique par un LLM
# ou : uv run jobot cv init          # partir d'un modèle vierge à remplir
uv run jobot cv check                # valider et lister les id disponibles
```

**Relis toujours le résultat d'un `cv import`** — c'est une extraction
automatique, pas un rendu garanti fidèle.

Enfin, installe le navigateur (nécessaire pour le PDF et le mode assisté) :

```bash
uv run playwright install chromium   # ~150 Mo, une seule fois
```

## Utilisation

Dans l'ordre où tu t'en sers réellement :

```bash
uv run jobot fetch --jours 7      # récupère, dédoublonne, filtre
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
uv run jobot assist <id>          # canal form : ouvre le navigateur, tu termines toi-même
```

`review`, `send` et `assist` s'appuient sur le canal de candidature de
chaque offre (voir plus bas) : `send` ne traite que le canal `email`,
`assist` que le canal `form`.

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
un CV filtré :

- **Compétences** : uniquement les tags sélectionnés ; une catégorie vidée de
  tous ses tags disparaît entièrement plutôt que d'afficher un titre vide.
- **Expériences** : toujours affichées en entier (ton identité professionnelle
  ne change pas d'une offre à l'autre), seuls les bullets sont filtrés — si
  aucun bullet d'une expérience n'a été sélectionné, ils sont tous affichés
  plutôt que de laisser une expérience vide.
- **Projets** : retenu si son `id` ou au moins un de ses bullets a été
  sélectionné ; mêmes règles de repli que les expériences.

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

**Canal `form`** — `jobot assist <id>` génère le CV, affiche les informations
à recopier, puis ouvre l'offre dans un navigateur visible à profil
persistant (tes sessions restent connectées d'une candidature à l'autre).
**jobot ne soumet jamais un formulaire** : tu remplis, tu vérifies, tu
cliques. À la fermeture du navigateur, il te demande si la candidature est
partie pour mettre à jour le statut.

## Choix du LLM

Aucun fournisseur n'est imposé : tout se règle dans `.env`, sans toucher au
code. LM Studio, Ollama, vLLM, OpenAI et OpenRouter parlent tous le format
d'API d'OpenAI, donc un seul client les couvre tous.

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
appelée une fois par combinaison département × mot-clé, et la dédup se fait
ensuite sur la clé `source:id`.

| Source | Identifiants | Ce qu'elle apporte |
|---|---|---|
| `francetravail` | `FT_CLIENT_ID` / `FT_CLIENT_SECRET` | API officielle, annonce complète, adresse de contact quand elle existe |
| `apec` | aucun | offres cadres absentes de France Travail |

**L'APEC n'a pas d'API publique documentée** : `sources/apec.py` utilise les
endpoints du site lui-même. Trois conséquences à connaître :

- Un anti-bot protège le domaine, donc le client charge d'abord une page du
  site pour récupérer ses cookies. Si une recherche renvoie 403, c'est lui —
  espacer les appels suffit généralement.
- Le détail d'une offre est bloqué par cet anti-bot, y compris depuis un vrai
  navigateur. **jobot ne stocke donc que l'extrait de ~280 caractères renvoyé
  par la recherche.** Le scoring travaille sur ce résumé et le sait (il lui est
  précisé de ne pas pénaliser l'offre pour ce qui manque) ; le texte complet
  reste à un clic, dans le navigateur ouvert par `jobot assist`.
- Comme la description est partielle, le filtre par mots-clés n'est pas
  réappliqué localement à ces offres — l'APEC a déjà cherché, elle, dans le
  texte entier. Les rejeter sur un extrait tronqué écarterait des offres
  pertinentes (`Offer.has_full_description`, testé dans `filters.py`).

Ces endpoints n'étant pas contractuels, ils peuvent changer sans préavis :
une source qui échoue affiche un avertissement, les autres continuent.

## Le canal de candidature

Chaque offre est routée automatiquement à partir des champs renvoyés par
l'API :

| Canal | Détecté par | Automatisation |
|---|---|---|
| `email` | `contact.courriel` | quasi complète — SMTP + PDF joint, confirmation avant envoi |
| `form` | `contact.urlPostulation` ou `origineOffre.urlOrigine` | assistée — navigateur pré-rempli, clic humain final |
| `unknown` | aucun des deux | écartée par défaut |

L'APEC ne publie jamais d'adresse de contact : **toutes ses offres arrivent sur
le canal `form`** et passent donc par `jobot assist`.

`jobot stats` donne la répartition : c'est elle qui indique combien de
candidatures peuvent réellement partir sans intervention manuelle sur le
formulaire.

## Structure du code

Dans l'ordre où les données y circulent :

| Fichier | Rôle |
|---|---|
| `config.py` | Config centralisée (`.env` → objet `settings`), messages d'erreur pour les identifiants manquants |
| `models.py` | `Offer`, `Channel`, `Status` — le schéma d'une offre et son cycle de vie |
| `pipeline.py` | Orchestration du pipeline complet (fetch → filtre → score → génération → envoi), presets domaines/départements, état d'exécution partagé avec l'UI |
| `web.py` | API FastAPI locale de `jobot ui` (recherche, validation, assistant, import CV) |
| `webui/` | L'interface web (page unique, design Electric Volt) |
| `sources/francetravail.py` | Client OAuth2 + pagination pour l'API France Travail |
| `sources/apec.py` | Client de la recherche apec.fr (pas d'API publique — voir *Les sources d'offres*) |
| `filters.py` | Filtrage à règles, sans appel LLM |
| `db.py` | Toute l'écriture SQLite (dédup, upsert, transitions de statut) |
| `cv.py` | Modèles du CV maître + `selectable_ids()`, le garde-fou anti-hallucination |
| `llm/` | Abstraction fournisseur : `base.py` (contrat), `gemini.py`, `openai_compat.py` |
| `scoring.py` | Appelle le LLM, valide sa sélection d'`id` contre `selectable_ids()` |
| `render.py` | Filtre le CV selon la sélection, rend le HTML (Jinja2) puis le PDF (Playwright) |
| `mailer.py` | Compose et envoie l'email de candidature |
| `assist.py` | Ouvre le navigateur pour le canal `form` |
| `templates/` | `cv.html.jinja` (mise en page du CV), `email.txt.jinja` (texte de l'email — jamais généré par le LLM) |
| `cli.py` | Toutes les commandes `jobot ...`, assemble les modules ci-dessus |

## Notes techniques

- **Pagination France Travail** : l'API plafonne à 150 résultats par appel et
  ~1150 au total par requête (`range=0-149`, puis `150-299`…). HTTP 206 =
  il en reste, 200 = fini.
- **Pagination APEC** : `range` est plafonné à 100 — au-delà l'API retombe
  silencieusement sur 20. Les résultats étant triés par date décroissante, le
  filtre `--jours` s'arrête à la première offre trop ancienne.
- **Croisement département × mot-clé** : aucune des deux API ne fait de OU sur
  les mots-clés, donc jobot émet une requête par combinaison et dédoublonne
  côté client.
- **Dédup** : clé primaire `source:id`. Un `content_hash` détecte les offres
  réécrites par l'employeur et les repasse en `new` pour un nouveau scoring.
- **Filtrage avant LLM** : chaque offre écartée par `filters.py` est un appel
  LLM économisé.

## Limites connues et pistes

- Les offres APEC n'ont qu'une description tronquée (voir *Les sources
  d'offres*), ce qui rend leur scoring moins fin que celui des offres France
  Travail.
- Pas de relance automatique après candidature.
