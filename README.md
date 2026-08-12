# jobot

Pipeline de candidature : récupération d'offres → filtrage → scoring IA → CV adapté → revue humaine.

**État : étape 3 sur 5 (scoring LLM) terminée et validée sur données réelles.**

## Démarrage

1. `uv sync`
2. Crée un compte sur [francetravail.io](https://francetravail.io), souscris à
   l'API **Offres d'emploi v2**, récupère `client_id` / `client_secret`.
3. `cp .env.example .env` puis colle les identifiants et choisis ton LLM
   (voir *Choix du LLM* ci-dessous — un modèle local suffit).
4. Crée ton CV maître :
   ```bash
   uv run jobot cv import mon-cv.html   # extraction automatique par le LLM
   uv run jobot cv init                 # ou : partir d'un modèle vierge à remplir
   uv run jobot cv check                # valider et lister les id disponibles
   ```

```bash
uv run jobot fetch --jours 7      # récupère, dédoublonne, filtre
uv run jobot stats                # répartition par statut et par canal
uv run jobot list --statut new    # les offres retenues
uv run jobot list --statut filtered_out   # et pourquoi les autres ont sauté
uv run jobot score                # scoring LLM + sélection de bullets sur les offres 'new'
uv run jobot list --statut scored # triées par score décroissant
uv run jobot show <id>            # détail complet, y compris score et id sélectionnés
```

## Choix du LLM

Aucun fournisseur n'est imposé : tout se règle dans `.env`, sans toucher au code.
LM Studio, Ollama, vLLM, OpenAI et OpenRouter parlent tous le format d'API
d'OpenAI, donc un seul client les couvre.

| `JOBOT_LLM_PROVIDER` | `JOBOT_LLM_BASE_URL` | Clé requise |
|---|---|---|
| `lmstudio` | `http://localhost:1234/v1` (implicite) | non |
| `ollama` | `http://localhost:11434/v1` (implicite) | non |
| `openai` | implicite | oui |
| `openrouter` | implicite | oui |
| `gemini` | — (SDK natif) | oui |
| `openai_compat` | à fournir | selon l'hôte |

Tous les serveurs ne supportent pas la sortie structurée, d'où une dégradation
en cascade dans `llm/openai_compat.py` : `json_schema` strict → mode JSON →
consigne dans le prompt, avec un parsing tolérant aux ```` ```json ```` et au
bavardage. Le mode qui a fonctionné est mémorisé pour les appels suivants.

Un petit modèle local respectera moins bien le schéma, mais **il ne peut pas
pour autant inventer une ligne de CV** : la validation des `id` (ci-dessous)
reste le garde-fou.

## Le canal de candidature

Chaque offre est routée automatiquement à partir des champs de l'API :

| Canal | Détecté par | Automatisation prévue |
|---|---|---|
| `email` | `contact.courriel` | 100 % auto — SMTP + PDF joint |
| `form` | `contact.urlPostulation` ou `origineOffre.urlOrigine` | assisté — Playwright pré-remplit, clic humain |
| `unknown` | aucun des deux | écarté par défaut |

`jobot stats` te donne la répartition : c'est elle qui dira combien de
candidatures pourront réellement partir sans intervention.

## Notes d'implémentation

- **Pagination** : l'API plafonne à 150 résultats par appel et ~1150 au total
  par requête (`range=0-149`, puis `150-299`…). HTTP 206 = il en reste, 200 = fini.
- **Croisement département × mot-clé** : l'API ne fait pas de OU sur les
  mots-clés, donc on émet une requête par combinaison et on dédoublonne côté client.
- **Dédup** : clé primaire `source:id`. Un `content_hash` détecte les offres
  réécrites par l'employeur et les repasse en `new` pour re-scoring.
- **Filtrage avant LLM** : chaque offre écartée par `filters.py` est un appel
  d'API économisé à l'étape de scoring.

## Master CV

Le CV maître est **une donnée utilisateur, pas du code** : `data/master-cv.json`
est gitignoré (il contient nom, téléphone, email). Le format est documenté par
`data/master-cv.example.json`, versionné et fictif.

Chaque unité que le LLM pourra choisir d'inclure ou non porte un `id` stable :
tags de compétences, bullets d'expérience, projets et leurs bullets. L'identité,
la formation, les langues et les centres d'intérêt sont fixes (toujours inclus).

`cv.py` expose `load_master_cv()` (modèles Pydantic + validation) et
`MasterCV.selectable_ids()`, qui sert à valider programmatiquement une
sélection du LLM : tout `id` renvoyé qui n'appartient pas à cet ensemble est
rejeté. Zéro hallucination possible par construction.

`jobot cv import` réutilise le LLM pour convertir un CV existant (HTML ou
texte) en `master-cv.json`, en découpant expériences et projets en bullets
autonomes. Le HTML est réduit à son texte avant l'envoi pour ne pas payer le
balisage en tokens. **Relis toujours le résultat** : l'extraction est fiable
mais rien ne garantit qu'elle n'a pas mal découpé ou omis un élément.

## Scoring LLM (étape 3)

`jobot score` demande au LLM, pour chaque offre au statut `new` : un score de
pertinence 0-100 spécifique au profil (pas juste "est-ce un poste tech ?"),
une justification, et une sélection d'`id` du CV maître à mettre en avant.
Chaque `id` qui n'existe pas dans `master-cv.json` est rejeté avant d'être
stocké (`scoring.py::score_offer`).

Le scoring est non déterministe : une même offre peut varier de quelques points
d'un run à l'autre. C'est un outil de tri, pas une note absolue.

## Suite

- [x] Étape 2 — `master-cv.json` (+ `jobot cv init/import/check`)
- [x] Étape 3 — scoring LLM des offres `new` + sélection de bullets
- [ ] Étape 4 — génération CV ciblé → HTML → PDF (Playwright)
- [ ] Étape 5 — queue de revue + envoi (SMTP / Playwright assisté)
