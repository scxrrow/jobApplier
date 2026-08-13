from __future__ import annotations

import re
import time
from typing import Any

import httpx

from ..models import Offer

# France Travail remplit parfois `contact.courriel` avec une phrase plutot
# qu'une adresse ("Pour postuler, utiliser le lien suivant : https://...").
# Router ces offres sur le canal email ferait tenter un envoi SMTP vers un
# destinataire absurde : on ne garde que ce qui ressemble a une vraie adresse.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.I)


def clean_email(value: str | None) -> str | None:
    """Retourne l'adresse si le champ en contient une, sinon None."""
    if not value:
        return None
    candidate = value.strip()
    return candidate if _EMAIL_RE.match(candidate) else None

TOKEN_URL = (
    "https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=%2Fpartenaire"
)
SEARCH_URL = "https://api.francetravail.io/partenaire/offresdemploi/v2/offres/search"
SCOPE = "api_offresdemploiv2 o2dsoffre"

# Contraintes de pagination imposees par l'API :
PAGE_SIZE = 150  # nb max de resultats par appel
MAX_START = 1000  # premier element de `range` plafonne a 1000
MAX_END = 1149  # second element plafonne a 1149 -> ~1150 offres par requete


class FranceTravailError(RuntimeError):
    pass


class FranceTravailClient:
    """Client OAuth2 client_credentials pour l'API Offres d'emploi v2."""

    def __init__(self, client_id: str, client_secret: str, timeout: float = 30.0):
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = httpx.Client(timeout=timeout)
        self._token: str | None = None
        self._token_expiry: float = 0.0

    def __enter__(self) -> FranceTravailClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self._http.close()

    def _access_token(self) -> str:
        # 60 s de marge pour ne pas partir avec un token qui expire en vol.
        if self._token and time.monotonic() < self._token_expiry - 60:
            return self._token

        resp = self._http.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "scope": SCOPE,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code != 200:
            raise FranceTravailError(
                f"Echec de l'authentification ({resp.status_code}). "
                f"Verifie FT_CLIENT_ID / FT_CLIENT_SECRET et que l'application "
                f"est bien souscrite a 'Offres d'emploi v2'.\n{resp.text[:300]}"
            )

        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expiry = time.monotonic() + float(payload.get("expires_in", 1500))
        return self._token

    def search(
        self,
        *,
        mots_cles: str | None = None,
        departement: str | None = None,
        type_contrat: str | None = None,
        alternance: bool = False,
        publiee_depuis: int | None = None,
        max_results: int = 600,
    ) -> list[dict[str, Any]]:
        """Recherche paginee. `publiee_depuis` en jours (1, 3, 7, 14 ou 31)."""
        base: dict[str, Any] = {}
        if mots_cles:
            base["motsCles"] = mots_cles
        if departement:
            base["departement"] = departement
        if type_contrat:
            base["typeContrat"] = type_contrat
        if alternance:
            base["alternance"] = "true"
        if publiee_depuis:
            base["publieeDepuis"] = publiee_depuis

        results: list[dict[str, Any]] = []
        start = 0

        while start <= MAX_START and len(results) < max_results:
            end = min(start + PAGE_SIZE - 1, MAX_END)
            params = {**base, "range": f"{start}-{end}"}

            resp = self._http.get(
                SEARCH_URL,
                params=params,
                headers={"Authorization": f"Bearer {self._access_token()}"},
            )

            if resp.status_code == 204:  # aucun resultat
                break
            if resp.status_code == 429:  # 10 req/s max, on laisse respirer
                time.sleep(1.0)
                continue
            if resp.status_code not in (200, 206):
                raise FranceTravailError(
                    f"Recherche en echec ({resp.status_code}) : {resp.text[:300]}"
                )

            page = resp.json().get("resultats") or []
            results.extend(page)

            # 200 = tout tient dans cette page, 206 = il en reste.
            if resp.status_code == 200 or len(page) < PAGE_SIZE or end >= MAX_END:
                break
            start = end + 1

        return results[:max_results]


def parse_offer(raw: dict[str, Any]) -> Offer:
    """Convertit une offre brute de l'API en modele interne.

    Le routage de candidature sort directement des champs de l'API :
    `contact.courriel` (valide) -> canal email, sinon canal formulaire via
    `contact.urlPostulation`, l'URL du partenaire, ou `origineOffre.urlOrigine`.
    """
    lieu = raw.get("lieuTravail") or {}
    contact = raw.get("contact") or {}
    origine = raw.get("origineOffre") or {}
    salaire = raw.get("salaire") or {}

    code_postal = lieu.get("codePostal")
    departement = code_postal[:2] if code_postal and len(code_postal) >= 2 else None
    if departement is None:
        # Certaines offres n'ont qu'un libelle de type "35 - Ille-et-Vilaine",
        # sans codePostal exploitable.
        prefix = (lieu.get("libelle") or "").split(" - ")[0].strip()
        if prefix:
            departement = prefix

    # Pour une offre issue d'un partenaire (origine "2"), `urlOrigine` ne mene
    # qu'a la fiche francetravail.fr, dont le bouton "Postuler" redirige vers
    # le partenaire. `partenaires[].url` donne ce lien final directement :
    # une etape de moins, et un formulaire atteignable par l'automatisation.
    partenaire_url = next(
        (p.get("url") for p in (origine.get("partenaires") or []) if p.get("url")),
        None,
    )

    nature = (raw.get("natureContrat") or "").lower()
    type_contrat = raw.get("typeContrat")
    is_alternance = (
        "apprentissage" in nature
        or "professionnalisation" in nature
        or type_contrat in {"E2", "FS"}
    )

    return Offer(
        source="francetravail",
        native_id=str(raw["id"]),
        title=raw.get("intitule") or "(sans titre)",
        company=(raw.get("entreprise") or {}).get("nom"),
        description=raw.get("description") or "",
        contract_type=type_contrat,
        contract_label=raw.get("typeContratLibelle"),
        location=lieu.get("libelle"),
        postal_code=code_postal,
        department=departement,
        rome_code=raw.get("romeCode"),
        rome_label=raw.get("romeLibelle"),
        salary=salaire.get("libelle"),
        experience=raw.get("experienceLibelle"),
        is_alternance=is_alternance,
        apply_email=clean_email(contact.get("courriel")),
        apply_url=contact.get("urlPostulation") or partenaire_url,
        origin_url=origine.get("urlOrigine"),
        published_at=raw.get("dateCreation"),
    )
