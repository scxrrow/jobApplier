from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, Field

_PUNCT = re.compile(r"[^a-z0-9]+")
# Formes juridiques et mentions de genre : le meme employeur et le meme poste
# s'ecrivent differemment d'un site a l'autre ("ACME SAS" / "Acme",
# "Developpeur (H/F)" / "Developpeur F/H").
_NOISE = re.compile(r"\b(sas|sasu|sarl|eurl|sa|scop|sci|hf|fh|h|f|m|w|x)\b")


def _normalize(text: str) -> str:
    plain = unicodedata.normalize("NFD", text.lower())
    plain = "".join(c for c in plain if unicodedata.category(c) != "Mn")
    plain = _PUNCT.sub(" ", plain)
    return " ".join(_NOISE.sub(" ", plain).split())


def dedup_key_for(offer_id: str, title: str, company: str | None) -> str:
    """Cle de rapprochement inter-sources. Voir `Offer.dedup_key`."""
    normalized = _normalize(company or "")
    if not normalized:
        return offer_id
    return f"{_normalize(title)}|{normalized}"


class Channel(StrEnum):
    """Comment on postule a cette offre.

    jobot ne remplit plus aucun formulaire : il prepare le dossier (CV adapte
    + lettre) et, sauf pour le canal email, laisse le candidat deposer lui-meme
    sur le site de l'offre. Le canal ne decrit donc plus un niveau
    d'automatisation, mais l'endroit ou la candidature atterrit.
    """

    EMAIL = "email"  # une vraie adresse : SMTP + PDF en piece jointe
    SITE = "site"  # une URL : le candidat depose son dossier sur place
    UNKNOWN = "unknown"  # ni adresse ni URL exploitable


class Status(StrEnum):
    NEW = "new"
    FILTERED_OUT = "filtered_out"
    SCORED = "scored"
    QUEUED = "queued"
    APPLIED = "applied"
    SKIPPED = "skipped"


# Sources dont l'API de recherche ne renvoie qu'un extrait de l'annonce.
EXCERPT_SOURCES = {"apec"}


class Offer(BaseModel):
    source: str
    native_id: str

    title: str
    company: str | None = None
    description: str = ""
    contract_type: str | None = None
    contract_label: str | None = None
    location: str | None = None
    postal_code: str | None = None
    department: str | None = None
    rome_code: str | None = None
    rome_label: str | None = None
    salary: str | None = None
    experience: str | None = None
    is_alternance: bool = False

    # Routage de candidature
    apply_email: str | None = None
    apply_url: str | None = None
    origin_url: str | None = None

    published_at: str | None = None
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def id(self) -> str:
        return f"{self.source}:{self.native_id}"

    @property
    def has_full_description(self) -> bool:
        return self.source not in EXCERPT_SOURCES

    @property
    def apply_link(self) -> str | None:
        """Ou envoyer le candidat pour deposer sa candidature.

        `apply_url` d'abord : pour une offre venue d'un partenaire, c'est le
        lien direct vers le vrai formulaire, la ou `origin_url` ne mene qu'a la
        fiche intermediaire de l'agregateur (cf. sources/francetravail.py).
        """
        return self.apply_url or self.origin_url

    @property
    def channel(self) -> Channel:
        if self.apply_email:
            return Channel.EMAIL
        if self.apply_link:
            return Channel.SITE
        return Channel.UNKNOWN

    @property
    def dedup_key(self) -> str:
        """Identifie une meme annonce republiee sur plusieurs sites.

        Agreger beaucoup de sources fait remonter la meme offre plusieurs fois
        (une annonce France Travail ressort telle quelle chez La Bonne
        Alternance). La cle `source:id` ne les rapproche pas : celle-ci le fait
        sur l'intitule et l'employeur normalises, ce qui coute un CV, une
        lettre et deux appels LLM de moins par doublon.

        Sans employeur, la cle retombe sur l'id : deux annonces anonymes au
        meme intitule sont trop souvent deux vraies offres distinctes pour
        qu'on ose les confondre.
        """
        return dedup_key_for(self.id, self.title, self.company)

    @property
    def content_hash(self) -> str:
        """Detecte une reecriture de l'offre cote employeur, pour re-scorer."""
        payload = "|".join(
            [self.title, self.company or "", self.description, self.salary or ""]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
