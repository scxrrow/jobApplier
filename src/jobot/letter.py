"""Lettre de motivation adaptee a une offre.

C'est le seul endroit de jobot ou un LLM ecrit une prose qui finira sous les
yeux d'un recruteur. Le garde-fou du CV (`selectable_ids`, qui interdit
d'inventer une ligne parce que le modele ne fait que choisir parmi l'existant)
ne peut pas s'appliquer tel quel a du texte libre. Trois contraintes le
remplacent :

1. **Matiere premiere reduite.** Le modele ne recoit pas le CV maitre entier,
   mais uniquement les elements que le scoring a retenus pour CETTE offre,
   plus l'identite et la formation. Il recombine des faits, il n'en decouvre
   pas.
2. **Structure imposee.** La sortie n'est pas une lettre, mais trois champs
   courts (accroche, adequation, motivation). L'en-tete, la formule d'appel,
   la formule de politesse et la signature viennent du template — jamais du
   modele.
3. **Relecture obligatoire, et gratuite.** jobot n'envoie plus rien tout seul
   sur le canal `site` : c'est le candidat qui depose le fichier. Il lit donc
   la lettre par construction. `suspect_terms()` lui signale en plus les
   affirmations chiffrees et les noms propres absents de son CV et de l'offre
   — la ou une hallucination se loge en pratique.
"""

from __future__ import annotations

import json
import re
import unicodedata

from pydantic import BaseModel, Field

from .cv import MasterCV
from .llm import LLMClient
from .models import Offer

# Longueurs visees, en caracteres. Une lettre de motivation qui deborde d'une
# page se fait survoler : le modele est cadre, et le rendu tronque au-dela.
MAX_PARAGRAPH = 700


class LetterDraft(BaseModel):
    """Les seules parties de la lettre ecrites par le modele."""

    hook: str = Field(
        description=(
            "2 a 3 phrases : pourquoi CE poste chez CET employeur. "
            "Accroche concrete, appuyee sur l'annonce."
        )
    )
    fit: str = Field(
        description=(
            "3 a 4 phrases : ce qui, dans le parcours fourni, repond aux besoins "
            "de l'annonce. Uniquement des faits presents dans le parcours."
        )
    )
    motivation: str = Field(
        description=(
            "2 a 3 phrases : ce que le candidat cherche dans ce poste et ce qu'il "
            "compte y apporter."
        )
    )

    def paragraphs(self) -> list[str]:
        return [
            _clip(self.hook),
            _clip(self.fit),
            _clip(self.motivation),
        ]

    def text(self) -> str:
        return "\n\n".join(self.paragraphs())


SYSTEM_PROMPT = """Tu rediges les trois paragraphes centraux d'une lettre de \
motivation en francais, pour le candidat decrit ci-dessous.

Regles absolues :
- N'ecris QUE a partir des elements du parcours fourni. N'invente aucun \
employeur, aucun diplome, aucune technologie, aucune duree et aucun chiffre \
qui n'y figure pas. Si le parcours ne dit pas combien d'annees d'experience le \
candidat a, ne l'ecris pas.
- Ne promets rien au nom du candidat (disponibilite, mobilite, pretentions \
salariales) : ces informations ne te sont pas fournies.
- N'ecris ni « Madame, Monsieur », ni formule de politesse finale, ni \
signature, ni coordonnees : le document les ajoute lui-meme.
- Ecris a la premiere personne, ton professionnel et sobre. Pas de \
superlatifs creux (« passionne depuis toujours », « candidat ideal »).
- Chaque paragraphe fait au plus 4 phrases."""


def _clip(text: str) -> str:
    """Coupe proprement a la derniere phrase entiere sous la limite."""
    clean = " ".join(text.split())
    if len(clean) <= MAX_PARAGRAPH:
        return clean
    cut = clean[:MAX_PARAGRAPH]
    stop = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[: stop + 1] if stop > 0 else cut.rstrip() + "…"


def selected_profile(cv: MasterCV, selected_ids: list[str]) -> dict:
    """Le parcours reduit a ce que le scoring a juge pertinent pour l'offre.

    C'est toute la matiere premiere du modele. Les experiences gardent leur
    intitule et leur employeur meme si aucun de leurs bullets n'a ete retenu :
    une lettre qui ignore un employeur du CV sonne faux. Mais seuls les bullets
    selectionnes sont transmis — le modele ne peut pas broder sur le reste.
    """
    selected = set(selected_ids)

    skills = [
        item.label
        for category in cv.skills
        for item in category.items
        if item.id in selected
    ]

    experiences = []
    for exp in cv.experiences:
        bullets = [b.text for b in exp.bullets if b.id in selected]
        experiences.append(
            {
                "poste": exp.title,
                "entreprise": exp.company,
                "dates": exp.dates,
                "points_retenus": bullets,
            }
        )

    projects = [
        {
            "titre": p.title,
            "points_retenus": [b.text for b in p.bullets if b.id in selected],
        }
        for p in cv.projects
        if p.id in selected or any(b.id in selected for b in p.bullets)
    ]

    return {
        "titre_actuel": cv.personal.headline,
        "resume": cv.summary,
        "formation": [{"intitule": e.title, "ecole": e.school} for e in cv.education],
        "langues": [f"{lang.language} ({lang.level})" for lang in cv.languages],
        "competences_retenues": skills,
        "experiences": experiences,
        "projets_retenus": projects,
    }


def _offer_prompt(offer: Offer) -> str:
    payload = {
        "titre": offer.title,
        "entreprise": offer.company,
        "lieu": offer.location,
        "contrat": offer.contract_label or offer.contract_type,
        "description": offer.description,
    }
    prompt = f"Offre visee (JSON) :\n{json.dumps(payload, ensure_ascii=False)}"
    if not offer.has_full_description:
        prompt += (
            "\n\nSeul le debut de l'annonce est disponible. Reste general sur les "
            "missions plutot que d'extrapoler ce qu'elle ne dit pas."
        )
    return prompt


def write_letter(
    client: LLMClient, offer: Offer, cv: MasterCV, selected_ids: list[str]
) -> LetterDraft:
    profile = selected_profile(cv, selected_ids)
    system = (
        f"{SYSTEM_PROMPT}\n\nParcours du candidat (JSON) — ta seule source de "
        f"faits :\n{json.dumps(profile, ensure_ascii=False)}"
    )
    return client.complete_json(system=system, user=_offer_prompt(offer), schema=LetterDraft)


# ---------------------------------------------------------------------------
# Detection d'hallucinations. Volontairement grossiere et non bloquante : elle
# signale au candidat les endroits ou regarder, elle ne pretend pas valider la
# lettre. Une lettre sans terme suspect peut rester fausse.

# Un mot, apostrophes exclues : l'elision francaise ("L'opportunite", "J'ai")
# collerait sinon une majuscule de determinant au mot qui suit.
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
# Une affirmation chiffree : "5 ans", "3 years", "120 clients", "98 %".
_FIGURE = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:%|[^\W\d_]+)", re.UNICODE)
# Fin de phrase, pour ne pas prendre un verbe capitalise en tete pour un nom
# propre. Un debut de paragraphe compte aussi comme un debut de phrase.
_SENTENCE_START = re.compile(r"(?:^|[.!?:;]\s|\n)\s*$")


def _fold(text: str) -> str:
    plain = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in plain if unicodedata.category(c) != "Mn")


def suspect_terms(letter: str, offer: Offer, cv: MasterCV) -> list[str]:
    """Termes de la lettre absents a la fois du CV maitre et de l'annonce.

    Deux familles, ce sont celles ou se logent les inventions couteuses : les
    noms propres (un employeur, un outil, une certification que le candidat n'a
    jamais eus) et les affirmations chiffrees (« 5 ans d'experience », « une
    equipe de 12 »).

    Volontairement conservateur sur les noms propres : un mot capitalise en
    debut de phrase est ignore, parce que le francais y met surtout des verbes
    et des determinants. Un employeur invente cite uniquement en tete de phrase
    passera donc au travers — c'est le prix a payer pour que la liste reste
    assez courte pour etre lue. Elle signale ou regarder, elle ne valide rien :
    une lettre sans terme signale peut rester fausse.
    """
    known = _fold(
        " ".join(
            [
                cv.model_dump_json(),
                offer.title,
                offer.company or "",
                offer.description,
                offer.location or "",
            ]
        )
    )

    flagged: list[str] = []
    seen: set[str] = set()

    def flag(term: str) -> None:
        key = _fold(term)
        if key and key not in seen and key not in known:
            seen.add(key)
            flagged.append(term)

    for match in _FIGURE.finditer(letter):
        flag(match.group(0).strip())

    for match in _WORD.finditer(letter):
        word = match.group(0)
        if len(word) <= 2 or not word[0].isupper():
            continue
        if _SENTENCE_START.search(letter[: match.start()]):
            continue
        flag(word)

    return flagged
