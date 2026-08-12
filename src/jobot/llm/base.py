from __future__ import annotations

import json
import re
from typing import Protocol, TypeVar

from pydantic import BaseModel

ModelT = TypeVar("ModelT", bound=BaseModel)

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


class LLMError(RuntimeError):
    pass


class LLMClient(Protocol):
    """Interface minimale attendue par le pipeline.

    Toute la logique metier passe par `complete_json` : on demande au modele une
    reponse conforme a un schema Pydantic. Chaque backend se debrouille pour
    contraindre la sortie avec les moyens du bord (json_schema natif, mode JSON,
    ou simple consigne dans le prompt pour les petits modeles locaux).
    """

    def complete_json(self, *, system: str, user: str, schema: type[ModelT]) -> ModelT: ...


def parse_json_response(text: str, schema: type[ModelT]) -> ModelT:
    """Valide une reponse texte contre le schema, en tolerant les bavardages.

    Les modeles locaux entourent souvent leur JSON de ```json ... ``` ou d'une
    phrase d'introduction. On nettoie avant de valider plutot que d'echouer.
    """
    if not text or not text.strip():
        raise LLMError("Reponse vide du modele.")

    candidate = _FENCE.sub("", text).strip()

    try:
        return schema.model_validate_json(candidate)
    except Exception:
        pass

    # Dernier recours : isoler le premier objet JSON complet du texte.
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start != -1 and end > start:
        try:
            return schema.model_validate_json(candidate[start : end + 1])
        except Exception as exc:
            raise LLMError(f"JSON invalide : {exc}\nReponse brute : {text[:400]}") from exc

    raise LLMError(f"Aucun JSON trouve dans la reponse : {text[:400]}")


def strict_json_schema(schema: type[BaseModel]) -> dict:
    """Schema JSON compatible avec le mode strict d'OpenAI.

    Le mode strict exige `additionalProperties: false` et que chaque propriete
    soit listee dans `required`, y compris les champs optionnels.
    """
    raw = schema.model_json_schema()

    def tighten(node: object) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"])
            for value in node.values():
                tighten(value)
        elif isinstance(node, list):
            for item in node:
                tighten(item)

    tighten(raw)
    return raw


def schema_hint(schema: type[BaseModel]) -> str:
    """Consigne de repli pour les backends sans sortie structuree native."""
    return (
        "Reponds UNIQUEMENT avec un objet JSON valide, sans texte autour et sans "
        "bloc de code, conforme a ce schema :\n"
        f"{json.dumps(schema.model_json_schema(), ensure_ascii=False)}"
    )
