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

    def keyword_matches(self, offer: Offer, title: str, haystack: str) -> bool:
        """Un mot-cle decrit-il le METIER de l'offre ?

        La regle tient en une phrase : **le titre doit porter au moins un mot du
        mot-cle**. Chercher le mot-cle n'importe ou dans l'annonce ne marche
        pas, parce que la description presente aussi l'employeur — « cybersecurite »
        apparait dans une offre de charge de developpement RH des que la boite est
        une ESN qui se decrit comme telle. C'est exactement ainsi que des offres
        sans aucun rapport avec le CV arrivaient jusqu'au scoring, et donc
        jusqu'au quota LLM.

        Les mots restants peuvent, eux, venir de la description : « administrateur
        reseau » doit continuer de reconnaitre « Administrateur systemes et
        reseaux informatique », dont le titre ne porte pas les deux mots cote a
        cote. L'ancrage porte sur un mot, pas sur la locution entiere.

        Sur une description tronquee (APEC), le titre decide seul. C'est la
        version juste de l'ancienne regle « pas de filtrage mots-cles sur les
        extraits » : ce qui est tronque, c'est le texte de l'annonce, jamais son
        intitule. Ne rien filtrer du tout laissait passer *toutes* les offres
        APEC, y compris assistant tresorier et gestion locative.
        """
        for keyword in self.mots_cles:
            folded = normalize(keyword).strip()
            if not folded:
                continue
            # Un mot-cle entierement fait de mots courts ('SOC') reste utilisable
            # tel quel plutot que de se retrouver sans aucun token.
            tokens = [t for t in folded.split() if len(t) > 2] or [folded]
            if not any(t in title for t in tokens):
                continue
            if offer.has_full_description and not all(t in haystack for t in tokens):
                continue
            return True
        return False

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

        if self.mots_cles and not self.keyword_matches(offer, title, haystack):
            return "aucun mot-cle dans l'intitule"

        if offer.channel == "unknown":
            return "aucun canal de candidature"

        return None
