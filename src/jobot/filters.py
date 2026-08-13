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


# Valeurs acceptees par le critere `contrat` de FilterRules (et par l'UI).
CONTRACT_KINDS = ("alternance", "cdi", "cdd", "stage", "interim")


def matches_contract(offer: Offer, kind: str) -> bool:
    """Le contrat de l'offre correspond-il au type demande ?

    Les deux sources n'encodent pas le contrat pareil : France Travail donne un
    code (`CDI`, `CDD`, `MIS`...) plus un libelle, l'APEC un identifiant interne
    dont `contract_label` est la traduction. On matche donc code puis libelle.
    """
    kind = kind.strip().lower()
    if kind in ("", "tous"):
        return True
    if kind == "alternance":
        return offer.is_alternance

    code = (offer.contract_type or "").upper()
    label = normalize(offer.contract_label or "")

    if kind == "cdi":
        return (code == "CDI" or label.startswith("cdi")) and not offer.is_alternance
    if kind == "cdd":
        return (code == "CDD" or label.startswith("cdd")) and not offer.is_alternance
    if kind == "stage":
        return code == "STG" or "stage" in label
    if kind == "interim":
        return code in {"MIS", "TTI", "DIN"} or "interim" in label
    return True


@dataclass
class FilterRules:
    departements: list[str] = field(default_factory=list)
    mots_cles: list[str] = field(default_factory=list)
    alternance_only: bool = False
    # Un type de CONTRACT_KINDS, ou ''/'tous' pour ne pas filtrer le contrat.
    contrat: str = ""
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

        if self.contrat and not matches_contract(offer, self.contrat):
            shown = offer.contract_label or offer.contract_type or "inconnu"
            return f"contrat hors perimetre ({shown})"

        if len(offer.description) < self.min_description_chars:
            return "description trop courte pour etre scoree"

        for term in self.exclude:
            if normalize(term) in title:
                return f"titre exclu ({term})"

        # Quand la source ne renvoie qu'un extrait de l'annonce, chercher les
        # mots-cles dedans rejetterait des offres que la source a pourtant
        # trouvees en cherchant, elle, dans le texte complet.
        if self.mots_cles and offer.has_full_description:
            hits = [k for k in self.mots_cles if normalize(k) in haystack]
            if not hits:
                return "aucun mot-cle metier"

        if offer.channel == "unknown":
            return "aucun canal de candidature"

        return None
