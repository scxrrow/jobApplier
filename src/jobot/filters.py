"""Filtrage a regles, applique AVANT tout appel LLM.

L'objectif est d'eliminer le bruit gratuitement : chaque offre ecartee ici
est un appel API economise a l'etape de scoring.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

from .models import Offer

# Une offre dont le titre contient un de ces termes est ecartee d'office.
DEFAULT_EXCLUDE = [
    "commercial",
    "vendeur",
    "telepros",
    "telephonique",
    "assurance",
    "mutuelle",
    "immobilier",
    "restauration",
    "manutention",
]

MIN_DESCRIPTION_CHARS = 120


def normalize(text: str) -> str:
    """Minuscules sans accents, pour comparer 'cybersecurite' et 'cybersécurité'."""
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")


@dataclass
class FilterRules:
    departements: list[str] = field(default_factory=list)
    mots_cles: list[str] = field(default_factory=list)
    alternance_only: bool = False
    exclude: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE))
    min_description_chars: int = MIN_DESCRIPTION_CHARS

    def check(self, offer: Offer) -> str | None:
        """Retourne None si l'offre passe, sinon la raison du rejet."""
        title = normalize(offer.title)
        haystack = f"{title} {normalize(offer.description)}"

        if self.departements and offer.department not in self.departements:
            return f"departement {offer.department} hors perimetre"

        if self.alternance_only and not offer.is_alternance:
            return "pas une alternance"

        if len(offer.description) < self.min_description_chars:
            return "description trop courte pour etre scoree"

        for term in self.exclude:
            if normalize(term) in title:
                return f"titre exclu ({term})"

        if self.mots_cles:
            hits = [k for k in self.mots_cles if normalize(k) in haystack]
            if not hits:
                return "aucun mot-cle metier"

        if offer.channel == "unknown":
            return "aucun canal de candidature"

        return None
