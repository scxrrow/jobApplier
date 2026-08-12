from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import BaseModel, Field

from .cv import MasterCV
from .llm import LLMClient
from .models import Offer


class _ScoreOutput(BaseModel):
    score: int = Field(description="Pertinence de 0 (aucun rapport) a 100 (ideale).")
    reason: str = Field(description="Une ou deux phrases en francais justifiant le score.")
    selected_ids: list[str] = Field(
        description="Ids du CV maitre a mettre en avant pour cette offre."
    )


TASK_PROMPT = """Tu evalues la pertinence d'offres d'emploi/alternance pour un candidat, \
a partir de son CV maitre structure en JSON (fourni ci-dessous).

Le candidat est etudiant en cybersecurite (EPITA), actuellement en alternance \
Developpeur/DevOps. Son profil est oriente securite des systemes, infrastructure, \
automatisation de tests, administration Linux/Windows.

Pour l'offre donnee dans le message :
1. Attribue un score de pertinence (0-100) par rapport a CE profil specifique — pas \
seulement "est-ce un poste tech ?" mais "ce candidat en particulier serait-il un bon \
candidat pour ce poste precis ?". Une offre generique de developpement web ou un poste \
RH/recrutement mentionnant "devops" en passant doit recevoir un score bas.
2. Justifie en 1-2 phrases, en francais.
3. Choisis, parmi les id du CV maitre ci-dessous, ceux a mettre en avant pour un CV \
adapte a cette offre precise : les tags de competences les plus pertinents, les \
bullets d'experience les plus pertinents pour chaque experience, et les projets \
(avec leurs bullets) les plus pertinents parmi les trois proposes. N'invente jamais \
un id : n'utilise que ceux presents dans le CV maitre ci-dessous."""


@dataclass
class ScoreResult:
    score: int
    reason: str
    selected_ids: list[str]
    dropped_ids: list[str]


def _system_instruction(cv: MasterCV) -> str:
    return f"{TASK_PROMPT}\n\nCV maitre (JSON) :\n{cv.model_dump_json()}"


def _offer_prompt(offer: Offer) -> str:
    payload = {
        "titre": offer.title,
        "entreprise": offer.company,
        "lieu": offer.location,
        "contrat": offer.contract_label or offer.contract_type,
        "experience_requise": offer.experience,
        "description": offer.description,
    }
    return f"Offre a evaluer (JSON) :\n{json.dumps(payload, ensure_ascii=False)}"


def score_offer(client: LLMClient, offer: Offer, cv: MasterCV) -> ScoreResult:
    data = client.complete_json(
        system=_system_instruction(cv),
        user=_offer_prompt(offer),
        schema=_ScoreOutput,
    )

    valid_ids = cv.selectable_ids()
    selected = [i for i in data.selected_ids if i in valid_ids]
    dropped = [i for i in data.selected_ids if i not in valid_ids]

    return ScoreResult(
        score=int(data.score),
        reason=data.reason,
        selected_ids=selected,
        dropped_ids=dropped,
    )
