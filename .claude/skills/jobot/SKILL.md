---
name: jobot
description: Orientation projet pour jobot (pipeline de candidature automatisee). A lire avant toute tache sur ce repo - architecture, invariants a ne pas casser, pieges deja debugges. Ne remplace pas README.md (doc utilisateur complete) : ce skill est pour l'agent, pas pour l'humain.
---

# jobot — orientation agent

Avant de lire ce skill en entier, fais deux choses (30 secondes, evite de
travailler sur une image perimee du projet — plusieurs sessions Claude
interviennent sur ce repo en parallele) :

```bash
git log --oneline -15        # ce qui a change depuis la derniere fois
uv run jobot stats            # etat reel de la base
```

Puis lis `README.md` pour l'usage complet cote humain. Ce skill couvre ce que
le README ne couvre pas : structure interne, regles a ne jamais casser, et
bugs deja rencontres et corriges — pour ne pas les reintroduire.

## Ce que fait le projet, en une phrase

Recupere des offres d'emploi/alternance (France Travail + APEC), les filtre
par regles, les fait noter par un LLM qui choisit aussi des elements du CV a
mettre en avant, attend une validation humaine, genere un CV PDF cible, et
aide a candidater (email automatique ou navigateur assiste).

## Cycle de vie d'une offre (le vrai modele mental du projet)

```
new → filtered_out               (filters.py, sans LLM ; reversible : chaque
                                  recherche UI repasse les filtered_out en new
                                  via db.reset_filtered — les criteres changent)
new → scored                     (score, LLM + validation d'id)
scored → queued | skipped        (review CLI ou clic UI ; automatique UNIQUEMENT
                                  en mode autonome, choisi et confirme par
                                  l'utilisateur au lancement)
queued → applied                 (send --envoyer / assist / clic "confirmer
                                  l'envoi" dans l'UI ; auto en mode autonome,
                                  canal email seulement)
```

Statuts definis dans `models.py::Status`. Hors mode autonome explicite, ne
jamais faire sauter la decision humaine (ex : scored → applied directement).

## Carte du code

| Fichier | Role | A savoir |
|---|---|---|
| `config.py` | `.env` → objet `settings` unique | Tout nouveau reglage passe par ici, jamais `os.environ` direct ailleurs |
| `models.py` | `Offer`, `Channel`, `Status` | `Offer.channel` et `Offer.has_full_description` sont des `@property` calculees, pas des colonnes |
| `sources/francetravail.py` | Client API officielle (OAuth2) | Pagination `range=0-149`, plafond ~1150/requete |
| `sources/apec.py` | Scraping des endpoints internes apec.fr | **Pas d'API publique** — anti-bot DataDome, voir docstring du fichier avant d'y toucher |
| `filters.py` | Filtrage a regles, avant tout appel LLM | Le matching mots-cles est **desactive** pour les offres a description tronquee (`has_full_description=False`) — voir plus bas |
| `db.py` | SQLite : upsert, dedup, transitions de statut | Migrations additives dans `_migrate()` (ALTER TABLE) — jamais de migration destructive |
| `pipeline.py` | Orchestration complete (fetch → filtre → score → generation → envoi), `SearchParams`, presets `DOMAINES`/`DEPARTEMENTS`, `RunState` | Partage par le CLI et l'UI — toute evolution du flux passe ici, pas en double dans `cli.py`/`web.py` |
| `web.py` | API FastAPI de `jobot ui` | Routes en `def` synchrone obligatoirement (threadpool) : l'API sync de Playwright plante dans un thread a boucle asyncio |
| `webui/index.html` | UI web (page unique, vanilla JS) | Design system : `electric-volt-DESIGN.md` a la racine du depot parent |
| `cv.py` | Modeles Pydantic du CV maitre + `selectable_ids()` | **Le garde-fou central du projet**, voir invariants ci-dessous |
| `llm/` | Abstraction fournisseur (`base.py` = contrat) | `gemini.py` et `openai_compat.py` (LM Studio/Ollama/OpenAI/OpenRouter) l'implementent ; jamais d'appel direct a un SDK LLM hors de ce package |
| `scoring.py` | Note une offre + valide la selection d'id | Rejette tout id hors de `selectable_ids()` avant stockage |
| `render.py` | CV filtre → HTML (Jinja2) → PDF (Playwright) | `page.emulate_media("print")` obligatoire avant `page.pdf()` |
| `mailer.py` | Compose et envoie l'email | Corps depuis `templates/email.txt.jinja`, jamais du LLM |
| `assist.py` | Navigateur assiste pour le canal `form` | Ne soumet jamais un formulaire programmatiquement |
| `cli.py` | Toutes les commandes `jobot ...` | Fichier le plus gros, point d'assemblage de tout le reste |

## Invariants — a ne jamais casser sans en discuter explicitement

Ces regles viennent de decisions deliberees, pas d'oublis. Si une tache
semble en demander la violation, c'est probablement le signe qu'il faut
reformuler la tache, pas contourner la regle.

1. **Le LLM ne genere jamais de texte de CV ni de lettre de motivation.** Il
   choisit des `id` existants (`scoring.py`), et `cv.selectable_ids()` valide
   chaque id avant stockage. Meme logique pour l'email : `templates/email.txt.jinja`
   est ecrit a la main, jamais passe au LLM. Une hallucination dans une
   candidature part chez un vrai recruteur — zero tolerance ici.
2. **`data/master-cv.json` est une donnee utilisateur, pas du code.** Gitignore,
   jamais commite, jamais suppose present sans verifier (`settings.require_master_cv()`).
   `data/master-cv.example.json` est le seul fichier CV versionne.
3. **Aucun envoi sans decision humaine explicite.** CLI : simulation par
   defaut, `--envoyer` + confirmation listant les destinataires. UI : la
   modale de validation montre destinataire/objet/corps avant le clic
   "confirmer l'envoi". Le mode autonome de l'UI est la seule exception,
   voulue par l'utilisateur : validation humaine desactivee au lancement
   + confirmation globale — active par defaut, jamais par defaut inverse.
4. **Un formulaire de candidature n'est jamais soumis automatiquement**,
   meme en mode autonome. Le navigateur s'ouvre visible (`assist.py`),
   l'humain remplit et clique. C'est une decision produit, pas une
   limitation technique a "corriger".
5. **Aucun fournisseur LLM en dur dans la logique metier.** `scoring.py` et
   `cv.py::extract_master_cv` prennent un `LLMClient` (protocol de `llm/base.py`),
   jamais un client Gemini/OpenAI importe directement. Nouveau fournisseur =
   nouveau module dans `llm/`, pas une branche `if` dans `scoring.py`.
6. **Le filtrage a regles tourne avant le LLM, jamais apres.** Objectif :
   economiser les appels API. Ne pas deplacer cette logique en aval "pour
   simplifier le flux".
7. **Offres a description tronquee (APEC) : pas de filtrage par mots-cles
   sur le texte.** L'API APEC a deja cherche server-side sur le mot-cle ;
   re-filtrer client-side sur un extrait de ~280 caracteres rejetterait des
   offres pertinentes a tort. Voir `Offer.has_full_description` et son usage
   dans `filters.py::FilterRules.check`.

## Pieges deja rencontres (evite de les reintroduire)

- **Elisions en francais dans les templates** : `"le poste de {{ offer.title }}"`
  et `"au sein de {{ offer.company }}"` cassent des que la variable commence
  par une voyelle ou est un nom propre quelconque. Utiliser des tournures qui
  ne s'elident jamais (`"le poste suivant : ..."`, `"chez ..."`).
- **Emoji dans le CV genere** : `📞`/`📧` etc. s'affichaient en cases vides
  sur un environnement headless Linux sans police d'emoji. `cv.html.jinja`
  utilise du texte simple pour rester portable sur n'importe quelle machine.
- **PDF Playwright sans style d'impression** : `page.pdf()` n'applique pas
  les regles `@media print` par defaut — il faut `page.emulate_media("print")`
  avant, sinon le CSS d'impression du template est ignore silencieusement.
- **Departement absent dans l'API France Travail** : certaines offres ne
  renvoient qu'un `libelle` ("35 - Ille-et-Vilaine") sans `codePostal`. Le
  parsing doit avoir un repli sur le prefixe du libelle, sinon ces offres
  sont perdues silencieusement par le filtre departement (voir
  `sources/francetravail.py::parse_offer`).
- **SMTP** : `smtp_tls` doit rester configurable (`starttls`/`ssl`/`none`) —
  forcer STARTTLS casse les relais locaux/internes sans chiffrement.

## Config multi-source et multi-LLM

- `JOBOT_SOURCES` (defaut `francetravail,apec`) — `cli.py::SOURCE_NAMES` liste
  les sources valides, `_fetch_source()` route vers le bon client.
- `JOBOT_LLM_PROVIDER` — voir tableau complet dans `README.md` (section
  *Choix du LLM*). Un modele local (LM Studio/Ollama) ne demande aucune cle.

## Que faire si le README et le code divergent

Fais confiance au code. Le README peut avoir ete ecrit ou edite par une
session anterieure ; le code est la verite. Si tu corriges une divergence,
mets aussi ce skill a jour si l'invariant ou le piege qu'il decrit a change.
