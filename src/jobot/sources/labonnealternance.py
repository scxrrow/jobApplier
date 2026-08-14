"""Client de l'API La Bonne Alternance (service public, gratuit).

Documentation : https://api.apprentissage.beta.gouv.fr/explorer/recherche-offre
Schema de reference : https://api.apprentissage.beta.gouv.fr/api/swagger.json

Trois particularites qui expliquent la forme de ce module :

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
  niveau de diplome. jobot cherche par intitule libre : le tri sur les mots-cles
  se fait donc localement, apres coup.
"""

from __future__ import annotations

import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from ..models import Offer

SEARCH_URL = "https://api.apprentissage.beta.gouv.fr/api/job/v1/search"

# L'API plafonne a 60 appels/minute et par consommateur. jobot emet une requete
# par departement x mot-cle : on reste tres en dessous, mais un 429 reste
# possible en enchainant les recherches.
RATE_LIMIT_PER_MIN = 60


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
        params: dict[str, Any] = {}
        if departement:
            params["departements"] = departement

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

        payload = resp.json()
        # Seules les vraies annonces : voir l'entete du module pour `recruiters`.
        results = [job for job in (payload.get("jobs") or []) if _is_active(job)]

        if publiee_depuis:
            floor = datetime.now(timezone.utc) - timedelta(days=publiee_depuis)
            results = [job for job in results if _published_after(job, floor)]

        if mots_cles:
            terms = [t for t in _fold(mots_cles).split() if len(t) > 2]
            results = [job for job in results if _matches(job, terms)]

        return results[:max_results]


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
    identifier = raw.get("identifier") or {}

    address = ((workplace.get("location") or {}).get("address")) or ""
    postal_code = _postal_code(address)

    # `id` est nul pour les offres collectees chez un partenaire : la paire
    # partenaire + identifiant partenaire prend le relais pour garder une cle
    # de dedup stable d'une recherche a l'autre.
    native_id = identifier.get("id") or ":".join(
        filter(None, [identifier.get("partner_label"), identifier.get("partner_job_id")])
    )

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
