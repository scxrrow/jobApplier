"""Client de recherche d'offres APEC.

L'APEC n'expose pas d'API publique documentee : les endpoints utilises ici sont
ceux que le site apec.fr appelle lui-meme. Deux consequences pratiques, qui
expliquent la forme de ce module :

- un anti-bot (DataDome) protege le domaine. La recherche ne repond que si la
  requete presente les cookies poses par une page du site : d'ou `_prime()`,
  appele une fois avant la premiere recherche.
- le detail d'une offre (`/cms/webservices/offre/public`) est, lui, bloque par
  cet anti-bot meme depuis un vrai navigateur. La description stockee est donc
  celle que renvoie la recherche, tronquee par l'API a ~280 caracteres. Le
  scoring travaille sur ce resume ; le texte complet reste accessible a
  l'humain via `origin_url` (c'est le canal `form` de toute facon).
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from ..models import Offer

PRIME_URL = "https://www.apec.fr/candidat/recherche-emploi.html/emploi"
SEARCH_URL = "https://www.apec.fr/cms/webservices/rechercheOffre"
OFFER_URL = "https://www.apec.fr/candidat/recherche-emploi.html/detail-offre/{numero}"

# `range` est plafonne a 100 : au-dela l'API retombe silencieusement sur 20.
PAGE_SIZE = 100

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Nomenclature RECHERCHE_OFFRE_TYPE_CONTRAT.
CONTRACT_LABELS = {
    101888: "CDI",
    101887: "CDD",
    20053: "Alternance",
    597137: "CDD - Alternance - Contrat d'apprentissage",
    597138: "CDD - Alternance - Contrat de professionnalisation",
    597139: "CDI - Alternance - Contrat d'apprentissage",
    597140: "CDI - Alternance - Contrat de professionnalisation",
    101930: "Interim",
    597141: "CDI interimaire",
    101889: "Mission d'interim",
    597171: "Stage",
}

ALTERNANCE_CONTRACTS = [20053, 597137, 597138, 597139, 597140]

# Identifiants a envoyer dans `typesContrat` pour chaque type de contrat
# selectionnable dans l'UI (cf filters.CONTRACT_KINDS).
CONTRACT_FILTER_IDS = {
    "alternance": ALTERNANCE_CONTRACTS,
    "cdi": [101888],
    "cdd": [101887],
    "stage": [597171],
    "interim": [101930, 597141, 101889],
}

# "Tours - 37", "Indre-et-Loire - 37", "Saint-Denis - 974"
_DEPARTMENT_RE = re.compile(r"-\s*(\d{3}|2[AB]|\d{2})\s*$")


class ApecError(RuntimeError):
    pass


class ApecClient:
    """Recherche d'offres sur apec.fr. Aucune authentification requise."""

    def __init__(self, timeout: float = 30.0):
        self._http = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "fr-FR,fr;q=0.9",
                "Referer": PRIME_URL,
            },
        )
        self._primed = False

    def __enter__(self) -> ApecClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self._http.close()

    def _prime(self) -> None:
        """Recupere les cookies anti-bot en chargeant une page publique."""
        if self._primed:
            return
        try:
            resp = self._http.get(PRIME_URL)
        except httpx.HTTPError as exc:
            raise ApecError(f"apec.fr injoignable : {exc}") from exc
        if resp.status_code != 200:
            raise ApecError(
                f"Impossible d'ouvrir apec.fr ({resp.status_code}) : "
                "les cookies necessaires a la recherche n'ont pas ete poses."
            )
        self._primed = True

    def search(
        self,
        *,
        mots_cles: str | None = None,
        departement: str | None = None,
        alternance: bool = False,
        type_contrat: str | None = None,
        publiee_depuis: int | None = None,
        max_results: int = 600,
    ) -> list[dict[str, Any]]:
        """Recherche paginee. `publiee_depuis` en jours.

        `type_contrat` : un type de `filters.CONTRACT_KINDS` ('cdi', 'stage'...),
        prioritaire sur le drapeau `alternance` conserve pour compatibilite.
        """
        self._prime()

        if type_contrat:
            contract_ids = CONTRACT_FILTER_IDS.get(type_contrat.strip().lower(), [])
        elif alternance:
            contract_ids = list(ALTERNANCE_CONTRACTS)
        else:
            contract_ids = []

        criteria: dict[str, Any] = {
            "activeFiltre": True,
            "typeClient": "CADRE",
            "sorts": [{"direction": "DESCENDING", "type": "DATE"}],
            "motsCles": mots_cles or "",
            "lieux": _location_ids(departement),
            "typesContrat": contract_ids,
            "fonctions": [],
            "niveauxExperience": [],
            "secteursActivite": [],
            "typesConvention": [],
            "salaireMinimum": 20,
            "salaireMaximum": 1000,
        }

        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=publiee_depuis)
            if publiee_depuis
            else None
        )

        results: list[dict[str, Any]] = []
        start = 0

        while len(results) < max_results:
            payload = {**criteria, "pagination": {"range": PAGE_SIZE, "startIndex": start}}
            resp = self._http.post(SEARCH_URL, json=payload)

            if resp.status_code == 429:
                time.sleep(1.0)
                continue
            if resp.status_code == 403:
                raise ApecError(
                    "Recherche refusee (403) : l'anti-bot d'apec.fr a bloque la "
                    "requete. Reessaie plus tard, ou espace les appels."
                )
            if resp.status_code != 200:
                raise ApecError(f"Recherche en echec ({resp.status_code}) : {resp.text[:300]}")

            page = resp.json().get("resultats") or []
            if not page:
                break

            # Le tri est decroissant par date : des qu'une offre passe sous le
            # seuil, tout le reste est plus ancien encore.
            reached_cutoff = False
            for raw in page:
                if cutoff and _published_at(raw) and _published_at(raw) < cutoff:
                    reached_cutoff = True
                    break
                results.append(raw)

            if reached_cutoff or len(page) < PAGE_SIZE:
                break
            start += PAGE_SIZE

        return results[:max_results]


def _location_ids(departement: str | None) -> list[int]:
    """Identifiants de lieu APEC : pour un departement, c'est son numero.

    La Corse et les collectivites non numeriques n'ont pas d'identifiant
    devinable ; on ne filtre alors pas cote serveur et `FilterRules` rejettera
    ce qui sort du perimetre.
    """
    if not departement:
        return []
    try:
        return [int(departement)]
    except ValueError:
        return []


def _published_at(raw: dict[str, Any]) -> datetime | None:
    value = raw.get("datePublication") or raw.get("dateValidation")
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_offer(raw: dict[str, Any]) -> Offer:
    """Convertit une offre brute de la recherche APEC en modele interne.

    L'APEC ne publie jamais d'adresse de contact : toutes ses offres partent
    donc sur le canal `form`, via l'URL de l'offre sur apec.fr.
    """
    numero = str(raw["numeroOffre"])
    lieu = raw.get("lieuTexte") or ""

    match = _DEPARTMENT_RE.search(lieu)
    departement = match.group(1) if match else None

    contract_id = raw.get("typeContrat")
    published = _published_at(raw)

    return Offer(
        source="apec",
        native_id=numero,
        title=raw.get("intitule") or "(sans titre)",
        company=raw.get("nomCommercial"),
        description=raw.get("texteOffre") or "",
        contract_type=str(contract_id) if contract_id else None,
        contract_label=CONTRACT_LABELS.get(contract_id),
        location=lieu or None,
        department=departement,
        salary=raw.get("salaireTexte"),
        is_alternance=contract_id in ALTERNANCE_CONTRACTS,
        origin_url=OFFER_URL.format(numero=numero),
        published_at=published.isoformat() if published else None,
    )
