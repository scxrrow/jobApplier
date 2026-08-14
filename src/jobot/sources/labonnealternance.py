"""Client de l'API La Bonne Alternance (service public, gratuit).

Documentation : https://api.apprentissage.beta.gouv.fr/explorer/recherche-offre
Schema de reference : https://api.apprentissage.beta.gouv.fr/api/swagger.json

Quatre particularites qui expliquent la forme de ce module :

- **Une cle est necessaire.** L'API est gratuite mais plus anonyme : il faut
  creer un compte sur api.apprentissage.beta.gouv.fr et generer un jeton, passe
  en `Authorization: Bearer`. C'est la seule source de jobot, avec France
  Travail, a demander des identifiants.
- **Deux natures de resultats.** `jobs` sont de vraies annonces ; `recruiters`
  sont des entreprises que l'algorithme juge susceptibles de recruter en
  alternance, sans annonce derriere. Ces dernieres n'ont ni intitule de poste
  ni description : les inclure remplirait la base d'offres fantomes que le
  scoring ne saurait pas evaluer. Seules les `jobs` sont retenues.
- **Pas de recherche plein texte.** L'API ne filtre que par code ROME, RNCP ou
  niveau de diplome. C'est LE point a comprendre sur cette source : chercher
  « technicien support » par balayage departemental + filtrage local ne donne
  presque pas les memes offres que le site officiel, qui traduit d'abord
  l'intitule en codes ROME (voir `METIER_URL` et `romes_for`). jobot fait les
  deux et reunit les resultats.
- **Un plafond de reponse non documente, et aucune pagination.** Voir
  `RESULT_CAP` : le second piege de cette source.
"""

from __future__ import annotations

import time
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from ..models import Offer

SEARCH_URL = "https://api.apprentissage.beta.gouv.fr/api/job/v1/search"

# Auto-completion metier du site La Bonne Alternance : un intitule libre en
# entree, des codes ROME en sortie. Public et sans cle. C'est le maillon qui
# manquait a jobot : l'API de recherche ne sait pas lire « technicien support »,
# le site traduit d'abord cet intitule en ROME avant d'interroger la meme API.
METIER_URL = "https://labonnealternance.apprentissage.beta.gouv.fr/api/v1/metiers/intitule"

# L'API plafonne a 60 appels/minute et par consommateur. jobot emet une requete
# par departement x niveau de diplome, plus une par departement x intitule
# cherche : le total grimpe vite (une cinquantaine pour 4 departements et
# 6 intitules), d'ou `_throttle` qui attend plutot que de prendre un 429.
RATE_LIMIT_PER_MIN = 60

# Plafond de reponse, mesure et non documente : toute requete s'arrete a 450
# offres, et l'API n'expose AUCUN parametre de pagination (ni page, ni offset,
# ni limit — voir le swagger). Une recherche par departement renvoie donc une
# tranche arbitraire, et tout ce qui est au-dela est purement invisible : c'est
# la raison pour laquelle des offres bien reelles n'apparaissaient jamais dans
# jobot. Constate sur Paris : 450 renvoyees pour un departement, et le meme
# nombre exactement sans aucun filtre.
RESULT_CAP = 450

# La parade : le niveau de diplome vise est le seul axe de decoupage disponible
# qui soit sans rapport avec le metier (contrairement au ROME, qui demanderait
# de traduire les intitules libres de l'utilisateur en codes). Interroger chaque
# niveau separement decoupe le resultat en tranches qui tiennent sous le
# plafond. Mesure sur Paris : 371 offres uniques en une requete, 570 en
# partitionnant, soit +54 %.
DIPLOMA_LEVELS = ("3", "4", "5", "6", "7")


class LaBonneAlternanceError(RuntimeError):
    pass


def _fold(text: str) -> str:
    plain = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in plain if unicodedata.category(c) != "Mn")


class LaBonneAlternanceClient:
    """Recherche d'offres en alternance. Requiert une cle API."""

    def __init__(self, api_key: str, timeout: float = 30.0):
        if not api_key:
            raise LaBonneAlternanceError(
                "LBA_API_KEY absente.\n"
                "  1. Cree un compte sur https://api.apprentissage.beta.gouv.fr\n"
                "  2. Genere un jeton depuis ton profil\n"
                "  3. Colle-le dans .env (LBA_API_KEY) ou dans l'ecran Reglages"
            )
        self._http = httpx.Client(
            timeout=timeout,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        # Offres deja recuperees, par departement puis par jeu de codes ROME.
        self._cache: dict[str, list[dict[str, Any]]] = {}
        # Intitule libre -> codes ROME. Voir `romes_for`.
        self._romes: dict[str, list[str]] = {}
        # Horodatage des appels, pour `_throttle`.
        self._calls: list[float] = []

    def __enter__(self) -> LaBonneAlternanceClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self._http.close()

    def search(
        self,
        *,
        mots_cles: str | None = None,
        departement: str | None = None,
        type_contrat: str | None = None,
        publiee_depuis: int | None = None,
        max_results: int = 600,
    ) -> list[dict[str, Any]]:
        """Meme signature que les autres sources, pour un appel uniforme.

        `type_contrat` est ignore : la source ne publie que de l'alternance
        (le pipeline ne l'interroge donc que pour ce type de contrat).
        """
        # Seules les vraies annonces : voir l'entete du module pour `recruiters`.
        dump = [job for job in self._department_jobs(departement) if _is_active(job)]
        if mots_cles:
            terms = [t for t in _fold(mots_cles).split() if len(t) > 2]
            dump = [job for job in dump if _matches(job, terms)]

        # Recherche par metier : le chemin qu'emprunte le site lui-meme, et le
        # seul qui atteigne les offres hors de la tranche renvoyee par le dump.
        # Pas de filtrage texte ici : le code ROME *est* la correspondance
        # metier, bien plus fiable qu'une recherche de sous-chaine. C'est
        # `filters.py` qui tranche ensuite, et lui sait etre rejoue.
        by_rome = [job for job in self._rome_jobs(mots_cles, departement) if _is_active(job)]

        merged: dict[str, dict[str, Any]] = {}
        for job in (*dump, *by_rome):
            merged.setdefault(native_id_of(job), job)
        results = list(merged.values())

        if publiee_depuis:
            floor = datetime.now(timezone.utc) - timedelta(days=publiee_depuis)
            results = [job for job in results if _published_after(job, floor)]

        return results[:max_results]

    def _throttle(self) -> None:
        """Tient la cadence sous `RATE_LIMIT_PER_MIN`, en attendant s'il le faut.

        Depuis que chaque intitule declenche une requete par metier *en plus* du
        dump departemental, le nombre d'appels croit avec departements x postes :
        la configuration courante en emet deja une cinquantaine. Un 429 en plein
        milieu ferait perdre la recherche entiere pour une poignee de secondes
        d'attente — mieux vaut ralentir tout seul.
        """
        now = time.monotonic()
        self._calls = [t for t in self._calls if now - t < 60.0]
        if len(self._calls) >= RATE_LIMIT_PER_MIN - 2:
            time.sleep(max(0.0, 60.0 - (now - self._calls[0])) + 0.2)
            now = time.monotonic()
            self._calls = [t for t in self._calls if now - t < 60.0]
        self._calls.append(now)

    def _get(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        self._throttle()
        resp = self._http.get(SEARCH_URL, params=params)
        if resp.status_code == 401:
            raise LaBonneAlternanceError(
                "Cle API refusee (401). Verifie LBA_API_KEY sur "
                "https://api.apprentissage.beta.gouv.fr"
            )
        if resp.status_code == 429:
            raise LaBonneAlternanceError(
                f"Quota atteint ({RATE_LIMIT_PER_MIN} appels/min) : espace les recherches."
            )
        if resp.status_code != 200:
            raise LaBonneAlternanceError(
                f"Recherche en echec ({resp.status_code}) : {resp.text[:300]}"
            )
        return resp.json().get("jobs") or []

    def romes_for(self, label: str) -> list[str]:
        """Codes ROME correspondant a un intitule libre ('technicien support').

        Passe par le service d'auto-completion du site de La Bonne Alternance,
        celui-la meme qui alimente sa barre de recherche. Deux raisons de ne pas
        coder la table en dur : elle suivrait mal le referentiel ROME, et surtout
        ce service *est* la definition de ce que le site entend par un intitule
        — c'est ce qui garantit que jobot cherche la meme chose que lui.

        Un echec n'est jamais fatal : sans code ROME, il reste le dump
        departemental. La recherche perd en couverture, pas en validite.
        """
        key = _fold(label).strip()
        if not key:
            return []
        if key in self._romes:
            return self._romes[key]

        codes: list[str] = []
        try:
            resp = self._http.get(METIER_URL, params={"label": label}, timeout=15.0)
            if resp.status_code == 200:
                for couple in resp.json().get("coupleAppellationRomeMetier") or []:
                    code = couple.get("code_rome")
                    if code and code not in codes:
                        codes.append(code)
        except Exception:  # noqa: BLE001 - source d'appoint, jamais bloquante
            codes = []

        self._romes[key] = codes
        return codes

    def _rome_jobs(self, mots_cles: str | None, departement: str | None) -> list[dict[str, Any]]:
        """Offres du/des metiers correspondant a l'intitule cherche."""
        if not mots_cles:
            return []
        codes = self.romes_for(mots_cles)
        if not codes:
            return []

        cache_key = f"{departement or '*'}|{','.join(codes)}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        params: dict[str, Any] = {"romes": ",".join(codes)}
        if departement:
            params["departements"] = departement
        try:
            jobs = self._get(params)
        except LaBonneAlternanceError:
            # Un ROME refuse ne doit pas faire tomber toute la recherche.
            jobs = []
        self._cache[cache_key] = jobs
        return jobs

    def _department_jobs(self, departement: str | None) -> list[dict[str, Any]]:
        """Toutes les offres d'un departement, plafond de l'API contourne.

        Deux raisons a cette methode plutot qu'un simple GET dans `search` :

        1. **Le plafond** (`RESULT_CAP`) : une requete par niveau de diplome,
           plus une sans filtre, puis union dedoublonnee. Aucune tranche ne
           couvre tout, mais leur reunion couvre bien plus qu'une requete seule.
        2. **Le cache** : le pipeline appelle `search()` une fois par mot-cle, or
           l'API ne sait pas chercher en plein texte — ces requetes etaient donc
           rigoureusement identiques, et jobot payait six allers-retours pour six
           fois la meme reponse. Le tri par mot-cle se fait en local, sur cette
           liste. Le cache vit le temps du client, c'est-a-dire d'une recherche.
        """
        key = departement or "*"
        if key in self._cache:
            return self._cache[key]

        base = {"departements": departement} if departement else {}
        found: dict[str, dict[str, Any]] = {}
        for level in (None, *DIPLOMA_LEVELS):
            params = dict(base)
            if level:
                params["target_diploma_level"] = level
            for job in self._get(params):
                # La meme offre revient d'une tranche a l'autre, et l'API se
                # repete deja a l'interieur d'une seule reponse.
                found.setdefault(native_id_of(job), job)

        self._cache[key] = list(found.values())
        return self._cache[key]


def native_id_of(raw: dict[str, Any]) -> str:
    """Identifiant stable d'une offre LBA.

    `id` est nul pour les offres collectees chez un partenaire : la paire
    partenaire + identifiant partenaire prend le relais pour garder une cle
    stable d'une recherche a l'autre.
    """
    identifier = raw.get("identifier") or {}
    return str(
        identifier.get("id")
        or ":".join(
            filter(None, [identifier.get("partner_label"), identifier.get("partner_job_id")])
        )
    )


def _is_active(job: dict[str, Any]) -> bool:
    status = ((job.get("offer") or {}).get("status") or "").lower()
    # Un statut absent est traite comme actif : mieux vaut une offre expiree
    # que rater toutes les offres si l'API cesse de remplir le champ.
    return status in ("", "active")


def _published_after(job: dict[str, Any], floor: datetime) -> bool:
    raw = ((job.get("offer") or {}).get("publication") or {}).get("creation")
    if not raw:
        return True  # sans date, on garde plutot que d'ecarter a tort
    try:
        published = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return True
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    return published >= floor


def _matches(job: dict[str, Any], terms: list[str]) -> bool:
    """Filtre plein texte local : l'API ne cherche que par code ROME."""
    if not terms:
        return True
    offer = job.get("offer") or {}
    workplace = job.get("workplace") or {}
    haystack = _fold(
        " ".join(
            [
                offer.get("title") or "",
                offer.get("description") or "",
                " ".join(offer.get("rome_codes") or []),
                workplace.get("name") or "",
            ]
        )
    )
    return any(term in haystack for term in terms)


def _postal_code(address: str) -> str | None:
    for token in address.replace(",", " ").split():
        if len(token) == 5 and token.isdigit():
            return token
    return None


def parse_offer(raw: dict[str, Any]) -> Offer:
    """Convertit une offre LBA en modele interne.

    Toutes ces offres arrivent sur le canal `site` : `apply.recipient_id` n'est
    pas une adresse de contact mais un identifiant interne, utilisable seulement
    par la route de candidature de LBA. Le candidat passe donc par `apply.url`.
    """
    offer = raw.get("offer") or {}
    workplace = raw.get("workplace") or {}
    apply_block = raw.get("apply") or {}
    contract = raw.get("contract") or {}

    address = ((workplace.get("location") or {}).get("address")) or ""
    postal_code = _postal_code(address)
    native_id = native_id_of(raw)

    duration = contract.get("duration")
    contract_label = f"Alternance ({duration} mois)" if duration else "Alternance"

    # La description de l'employeur complete souvent une annonce laconique :
    # le scoring travaille sur les deux.
    description = offer.get("description") or ""
    if workplace.get("description"):
        description = f"{description}\n\n{workplace['description']}".strip()

    return Offer(
        source="labonnealternance",
        native_id=str(native_id),
        title=offer.get("title") or "(sans titre)",
        company=workplace.get("name") or workplace.get("legal_name"),
        description=description,
        contract_type="alternance",
        contract_label=contract_label,
        location=address or None,
        postal_code=postal_code,
        department=postal_code[:2] if postal_code else None,
        rome_code=next(iter(offer.get("rome_codes") or []), None),
        rome_label=None,
        salary=None,
        experience=None,
        is_alternance=True,
        apply_email=None,
        apply_url=apply_block.get("url"),
        origin_url=apply_block.get("url"),
        published_at=(offer.get("publication") or {}).get("creation"),
    )
