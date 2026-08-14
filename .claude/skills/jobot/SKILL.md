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
| `config.py` | `.env` → objet `settings` unique | Tout nouveau reglage passe par ici, jamais `os.environ` direct ailleurs. `write_env_values()`/`reload_settings()` permettent l'edition a chaud depuis l'UI (ecran Reglages) — voir *Pieges* |
| `models.py` | `Offer`, `Channel`, `Status` | `Offer.channel` et `Offer.has_full_description` sont des `@property` calculees, pas des colonnes |
| `sources/francetravail.py` | Client API officielle (OAuth2) | Pagination `range=0-149`, plafond ~1150/requete. Deux pieges de routage traites dans `parse_offer` — voir *Pieges deja rencontres* |
| `sources/apec.py` | Scraping des endpoints internes apec.fr | **Pas d'API publique** — anti-bot DataDome, voir docstring du fichier avant d'y toucher |
| `filters.py` | Filtrage a regles, avant tout appel LLM | Le matching mots-cles est **desactive** pour les offres a description tronquee (`has_full_description=False`) — voir plus bas |
| `db.py` | SQLite : upsert, dedup, transitions de statut | Migrations additives dans `_migrate()` (ALTER TABLE) — jamais de migration destructive |
| `pipeline.py` | Orchestration complete (fetch → filtre → score → generation → envoi), `SearchParams`, presets `DOMAINES`/`DEPARTEMENTS`, `RunState` | Partage par le CLI et l'UI — toute evolution du flux passe ici, pas en double dans `cli.py`/`web.py` |
| `web.py` | API FastAPI de `jobot ui` | Routes en `def` synchrone obligatoirement (threadpool) : l'API sync de Playwright plante dans un thread a boucle asyncio |
| `webui/index.html` | UI web : recherche + validation (vanilla JS) | Design system : `electric-volt-DESIGN.md` a la racine du depot parent |
| `webui/candidatures.html` | UI web : candidatures envoyees, page separee de la recherche | JS minimal dedie, ne pas re-fusionner dans index.html — voir *Pieges* |
| `webui/style.css` | CSS partage entre les pages webui | Servi via `StaticFiles` (`/assets/...`) monte dans `web.py` |
| `cv.py` | Modeles Pydantic du CV maitre + `selectable_ids()` | **Le garde-fou central du projet**, voir invariants ci-dessous |
| `llm/` | Abstraction fournisseur (`base.py` = contrat) | `gemini.py` et `openai_compat.py` (LM Studio/Ollama/OpenAI/OpenRouter) l'implementent ; jamais d'appel direct a un SDK LLM hors de ce package |
| `scoring.py` | Note une offre + valide la selection d'id | Rejette tout id hors de `selectable_ids()` avant stockage |
| `render.py` | CV adapte → HTML (Jinja2) → PDF (Playwright) | `page.emulate_media("print")` obligatoire avant `page.pdf()`. La selection LLM **ordonne** (competences/projets en tete), elle ne **supprime plus** — un CV ampute a ete un bug signale, ne pas reintroduire le filtrage dur |
| `mailer.py` | Compose et envoie l'email | Corps depuis `templates/email.txt.jinja`, jamais du LLM |
| `assist.py` | Navigateur assiste (mode manuel du canal `form`) | L'humain fait tout |
| `autofill.py` | Remplissage + soumission auto des formulaires (mode auto du canal `form`) | Heuristiques best-effort ; navigateur toujours visible ; jamais sans validation par offre — voir invariant 4. Sur mur de connexion : pause + reprise, jamais d'abandon ni de mot de passe stocke |
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
4. **Un formulaire n'est rempli/soumis qu'apres validation explicite de
   l'offre par l'utilisateur** (clic par offre dans l'UI), et le navigateur
   reste **toujours visible** pendant l'automatisation (`autofill.py`) — pas
   de soumission headless, pas de soumission en masse sans clic par offre.
   Le mode manuel (`assist.py`, l'humain fait tout) reste disponible via le
   toggle "candidature automatique" de l'UI. Le statut final (`applied`)
   reste confirme par l'humain : la detection du succes d'une soumission
   n'est pas fiable.
5. **Aucun fournisseur LLM en dur dans la logique metier.** `scoring.py` et
   `cv.py::extract_master_cv` prennent un `LLMClient` (protocol de `llm/base.py`),
   jamais un client Gemini/OpenAI importe directement. Nouveau fournisseur =
   nouveau module dans `llm/`, pas une branche `if` dans `scoring.py`.
6. **Le filtrage a regles tourne avant le LLM, jamais apres.** Objectif :
   economiser les appels API. Ne pas deplacer cette logique en aval "pour
   simplifier le flux".
7. **Le filtrage par mots-cles s'ancre sur l'intitule, jamais sur la seule
   description.** Un mot-cle ne compte que si le titre de l'offre porte au
   moins un de ses mots ; les mots restants peuvent venir de la description
   (« administrateur reseau » doit reconnaitre « Administrateur systemes et
   reseaux »). Sur une description tronquee (APEC), le titre decide seul : ce
   qui est tronque, c'est le texte de l'annonce, jamais son intitule. Voir
   `filters.py::FilterRules.keyword_matches`.
   *Regle precedente, a ne pas retablir* : « pas de filtrage mots-cles du tout
   sur les descriptions tronquees ». Elle laissait passer **toutes** les offres
   APEC vers le scoring (assistant tresorier, gestion locative, chargee de
   developpement RH pour un profil informatique). Et chercher le mot-cle
   n'importe ou dans une description complete laissait passer les offres RH
   des ESN, dont la plaquette employeur contient « cybersecurite » et
   « devops ». Mesure sur la base : 237 -> 168 offres atteignant le LLM, sans
   perdre une seule offre notee >= 85.

## Pieges deja rencontres (evite de les reintroduire)

- **Ecran d'onboarding qui devient invisible** : la section d'import CV
  n'etait affichee que quand `cv.present` etait faux (logique d'onboarding),
  ce qui la rendait injoignable des qu'un CV existait — bug signale par
  l'utilisateur ("l'import n'existe pas") alors que l'endpoint marchait tres
  bien cote API. Toute fonctionnalite d'edition/reconfiguration doit rester
  accessible en permanence (modale ouverte par un bouton/indicateur toujours
  visible dans la nav), pas seulement pendant un etat "premiere utilisation".
  Meme logique deja appliquee aux reglages LLM/SMTP (dots cliquables).
- **Page separee = fichier HTML separe, pas une section masquee** : quand
  l'utilisateur demande une page differente ("candidatures sur une page a
  part"), une section togglee par JS sur la meme page ne repond pas au
  besoin (meme URL, pas de navigation reelle). `candidatures.html` est un
  document HTML complet avec sa propre route FastAPI (`GET /candidatures`),
  son propre JS minimal ; seul le CSS est partage (`style.css` extrait pour
  cette raison). Ne pas re-fusionner en un SPA sans qu'on le demande.

- **Rechargement a chaud de `settings`** : `reload_settings()` mute l'objet
  `Settings` existant attribut par attribut (`setattr`), au lieu de
  reassigner `config.settings = Settings()`. Necessaire car chaque module a
  fait `from .config import settings` — reassigner ne changerait que le nom
  dans `config.py`, pas les references deja liees ailleurs (pipeline.py,
  cli.py, web.py...). Toujours muter en place pour un changement de config
  a chaud, jamais reassigner.
- **Ecriture de `.env` depuis l'UI** : `write_env_values()` remplace les
  lignes existantes et preserve tout le reste (commentaires, ordre). Les
  secrets (cle LLM, mot de passe SMTP) ne sont jamais renvoyes en clair par
  l'API (`GET /api/settings` expose `*_set: bool`, pas la valeur) — champ
  vide cote UI = "ne pas changer", jamais "effacer". `.env` est gitignore,
  verifie avant toute modification de cette logique.

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
- **Bandeau cookies = clics impossibles** : le bandeau de consentement recouvre
  la page et intercepte les clics. Playwright signale l'element comme visible,
  mais `click()` expire (30 s par defaut !). `_dismiss_banner()` doit tourner
  avant toute interaction, et les clics portent un `timeout` court — sans ca,
  une page a 150 elements peut bloquer le thread des dizaines de minutes.
- **"Postuler" n'est pas toujours un `<button>`** : sur France Travail c'est un
  `<a href>` ordinaire. `_click_first_match(..., include_links=True)` couvre ce
  cas. Pour la soumission, les liens ne sont acceptes qu'avec
  `_SUBMIT_STRICT_RE` : cliquer un lien au hasard contenant "envoyer" serait
  risque, "Envoyer ma candidature" ne l'est pas.
- **Une candidature = plusieurs ecrans** : c'est le piege central de
  `autofill.py`. Sur France Travail connecte, le parcours fait trois ecrans :
  (1) fiche de l'offre, "Postuler" est un lien ; (2) recapitulatif des criteres
  **sans aucun champ**, bouton "Envoyer ma candidature" ; (3) "Postuler en
  ligne" — depot du CV, lettre de motivation **pre-remplie par le site**, case
  "Je confirme que mes coordonnees sont valides" **non marquee `required`**,
  bouton sobrement intitule "Envoyer". D'ou, dans cet ordre, chaque piece du
  dispositif :
  - `_fill_pass` retourne `(rempli, devoile)` : l'ecran 2 n'a rien a remplir,
    seul `devoile` distingue "ecran atteint" de "page illisible". Conditionner
    l'envoi au seul `rempli` faisait abandonner jobot a un clic du but.
  - `_run_steps` enchaine les ecrans au lieu d'un unique clic, et s'arrete des
    qu'un clic ne change plus `_page_state` — sinon un formulaire qui ne
    navigue pas se ferait soumettre `_MAX_STEPS` fois.
  - Le motif d'envoi passe de `_SUBMIT_STRICT_RE` a `_SUBMIT_RE` des qu'un
    ecran a ete rempli : "Envoyer" tout court n'est sur qu'une fois qu'on sait
    etre dans le formulaire.
  - Les cases a cocher ne peuvent pas se limiter a `[required]` ni au texte du
    `<label>` associe : d'ou `_CONFIRM_RE` + `_checkbox_text` (qui remonte au
    parent). Volontairement restreint a la confirmation explicite — pas
    question de cocher une case "je souhaite recevoir...".
- **Ne jamais plafonner le parcours du DOM** : `_iter_matches` releve les
  textes en un seul `evaluate` par frame, precisement pour ne pas avoir a
  limiter le nombre d'elements examines. Le plafond de 150 qui existait avant
  (pour tenir le cout d'un aller-retour Playwright par element) faisait
  manquer tout le contenu utile des pages France Travail : les menus
  deroulants du bandeau contiennent des centaines de `<a href>`, tous places
  avant le formulaire dans le DOM. Symptome vecu : le bouton "Telecharger un
  CV" jamais vu, donc ni clique ni compte comme etape manquante, donc une
  candidature annoncee envoyee alors qu'elle etait restee sur l'ecran du CV.
- **`submitted` ne doit jamais etre optimiste** : un faux "envoye" classe
  l'offre comme traitee et l'utilisateur n'y revient jamais — c'est l'erreur la
  plus couteuse du projet. Ne conclure a l'envoi que sur `_looks_sent` (message
  de confirmation du site) ou sur l'absence de blocage avere. "Il reste un
  bouton d'envoi" n'est PAS une preuve d'echec (un formulaire soumis en AJAX
  garde le sien) ; un depot de CV attendu et non satisfait, si — voir
  `_blocked_on_upload`, qui croise deux signaux independants (bouton de depot
  repere, ou mention d'une etape CV dans le texte de la page). Cette
  redondance est deliberee : la version qui ne regardait que le bouton a
  produit un faux "envoyee" des que le reperage echouait.
- **Depot de CV sans `input[type=file]` atteignable** : "Telecharger un CV" sur
  France Travail ouvre un selecteur de fichier natif. Deux parades
  complementaires, les deux necessaires : `set_input_files` sur les input
  masques (le cas courant — ne pas filtrer sur `is_visible()`, ils le sont
  rarement), et l'interception de l'evenement `filechooser` dans `_attach_cv`
  quand l'input n'existe pas avant le clic. Le conteneur de l'input doit en
  revanche etre affiche (`checkVisibility()` sur le parent) : sans ce garde-fou,
  jobot joint le CV a un formulaire d'un ecran encore cache et croit tenir le
  bon formulaire sans avoir clique "Postuler".
- **Un mur de connexion se remplit tres bien** : la detection ne peut pas
  rester conditionnee a l'echec du remplissage. Sur une page "Connexion /
  Création", `_FIELD_PATTERNS` reconnait les champs email et les remplit —
  jobot croyait alors tenir le formulaire de candidature, cliquait un bouton
  de cette page et annoncait la candidature envoyee (constate : `filled`
  contenant quatre fois `email`, `notes` vide). D'ou `_has_password_field`,
  evalue AVANT tout remplissage et de nouveau avant de conclure a l'envoi : un
  champ mot de passe visible se suffit a lui-meme, contrairement aux libelles
  de `_looks_like_login` qui restent, eux, des signaux faibles.
- **Mur de connexion : pause, jamais abandon** : `auto_apply` detecte
  l'authentification requise (`_looks_like_login`, applique seulement si le
  remplissage a echoue — un lien "Connexion" en en-tete ne suffit pas), signale
  via `on_login_required`, attend `wait_for_resume()` puis recharge et reprend.
  Choix delibere : aucun identifiant tiers n'est stocke par jobot, c'est le
  profil Chrome persistant qui porte les sessions. Ne pas "simplifier" en
  ajoutant des logins/mots de passe en configuration.
- **URL de candidature France Travail** : `origineOffre.urlOrigine` mene a la
  fiche sur `candidat.francetravail.fr`, PAS au formulaire — son bouton
  "Postuler" redirige vers le partenaire, ce qui rendait l'automatisation
  impossible. `origineOffre.partenaires[].url` contient le lien direct (present
  sur 100 % des offres partenaires observees). `parse_offer` le prefere donc a
  `urlOrigine` ; ne pas revenir en arriere en "simplifiant" le parsing.
- **URL d'offre APEC : le segment `/emploi` est une route, pas un ornement** :
  `apec.fr` est une application Angular ; `detail-offre/{numero}` est une route
  **enfant** de `recherche-emploi.html/emploi`. Ecrite sans ce segment, l'URL
  n'est reconnue par aucune route : le routeur retombe sur la route par defaut
  et **reecrit la barre d'adresse** en `/candidat/recherche-emploi.html/` —
  numero perdu, page vide, aucun message d'erreur. Symptome vecu : survol de
  « Ouvrir l'offre » affichant bien l'URL avec le numero, mais onglet ouvert
  sur une page blanche sans offre, donc candidature impossible. Le diagnostic
  spontane (« il faudrait etre connecte a son compte APEC ») est faux : aucune
  authentification n'est en jeu. Verification faite en relevant les `href` que
  la page de recherche d'apec.fr genere elle-meme — c'est la seule source de
  verite pour cette forme d'URL, le JSON de `rechercheOffre` ne contient aucun
  champ d'URL. Deux signes distinguent les deux formes en cas de doute : la
  bonne conserve l'URL demandee et monte le composant de detail (il repond, y
  compris pour dire « offre plus disponible ») ; la mauvaise redirige
  silencieusement. Ne pas « simplifier » l'URL en retirant `/emploi`.
- **`contact.courriel` peut ne pas etre une adresse** : France Travail y met
  parfois une phrase ("Pour postuler, utiliser le lien suivant : https://...").
  Sans validation, ces offres partaient sur le canal email et un `send
  --envoyer` aurait tente un envoi SMTP vers cette chaine. `clean_email()`
  filtre sur une vraie regex d'adresse ; volontairement strict (une adresse
  noyee dans une phrase est rejetee plutot qu'extraite au jugé).
- **La Bonne Alternance ne cherche pas en plein texte — il faut passer par le
  ROME** : c'est le piege majeur de cette source, et il a produit le symptome
  « le site affiche 17 offres pour technicien support, jobot n'en a que 2 ».
  Balayer un departement puis filtrer les mots-cles en local ne donne quasiment
  pas le meme ensemble que le site officiel, qui traduit d'abord l'intitule en
  codes ROME avant d'interroger la **meme** API. Mesure : 102 offres sous les
  ROME de « technicien support », dont **99 jamais vues par jobot**. La
  traduction se fait via le service d'auto-completion metier du site
  (`METIER_URL`, public, sans cle) — surtout ne pas coder la table en dur, ce
  service *est* la definition de ce que le site entend par un intitule.
  Le balayage departemental est conserve en complement, pas en remplacement.
- **La Bonne Alternance plafonne a 450 offres, sans pagination** : mesure, non
  documente, et invisible — la reponse est simplement tronquee. Une requete par
  departement renvoyait donc une tranche arbitraire, et des offres bien reelles
  n'apparaissaient jamais dans jobot. Symptome vecu : 450 offres pour Paris,
  450 pour trois departements reunis, et **450 sans aucun filtre** — c'est ce
  dernier chiffre qui trahit le plafond. La parade est de partitionner par
  `target_diploma_level` (seul axe disponible sans rapport avec le metier) et
  de reunir les tranches : 371 -> 570 offres uniques sur Paris. Le pool est
  mis en cache par departement, parce que l'API ignore les mots-cles : sans ca,
  six intitules de poste declenchaient six fois exactement la meme requete.
  Ne pas « simplifier » en revenant a un GET unique par departement.
- **Corrections de parsing retroactives** : le JSON brut de chaque offre est
  conserve en colonne `raw`. `pipeline.reparse_routing()` (expose par `jobot
  reparse`, et appele au debut de chaque run UI) re-route les offres deja en
  base sans rappeler l'API. Y penser apres toute amelioration de `parse_offer`.
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

## Tests

`.venv/bin/python tests/test_autofill.py` — script autonome (ni pytest ni
dependance en plus), 8 parcours reproduits en local avec Playwright headless.
**A lancer apres toute modification d'`autofill.py`** : chacun de ces cas
correspond a un faux "candidature envoyee" reellement survenu, et rien d'autre
ne les rattrape.
