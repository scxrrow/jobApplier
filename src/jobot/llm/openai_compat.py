from __future__ import annotations

from openai import OpenAI

from .base import (
    LLMError,
    ModelT,
    parse_json_response,
    schema_hint,
    strict_json_schema,
)


class OpenAICompatClient:
    """Client pour toute API parlant le format OpenAI.

    Couvre OpenAI, OpenRouter, et les serveurs locaux (LM Studio, Ollama, vLLM,
    llama.cpp) : seuls `base_url` et `model` changent.

    Les serveurs locaux ne supportent pas tous `json_schema`, d'ou la degradation
    en cascade : json_schema strict -> mode JSON -> consigne dans le prompt.
    """

    def __init__(self, *, api_key: str, base_url: str | None, model: str, timeout: float = 120.0):
        # Les serveurs locaux ignorent la cle mais le SDK en exige une non vide.
        self._client = OpenAI(api_key=api_key or "not-needed", base_url=base_url, timeout=timeout)
        self._model = model
        self._mode = "json_schema"

    def complete_json(self, *, system: str, user: str, schema: type[ModelT]) -> ModelT:
        errors: list[str] = []

        for mode in self._modes():
            try:
                text = self._call(system=system, user=user, schema=schema, mode=mode)
            except Exception as exc:
                errors.append(f"{mode}: {exc}")
                continue

            try:
                result = parse_json_response(text, schema)
            except LLMError as exc:
                errors.append(f"{mode}: {exc}")
                continue

            # On retient le mode qui a marche pour ne pas retenter les autres.
            self._mode = mode
            return result

        raise LLMError(
            f"Le modele '{self._model}' n'a pas produit de JSON exploitable.\n"
            + "\n".join(errors)
        )

    def _modes(self) -> list[str]:
        ordered = ["json_schema", "json_object", "prompt"]
        # Le mode qui a fonctionne la derniere fois passe en premier.
        ordered.remove(self._mode)
        return [self._mode, *ordered]

    def _call(self, *, system: str, user: str, schema: type[ModelT], mode: str) -> str:
        kwargs: dict = {}

        if mode == "json_schema":
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "schema": strict_json_schema(schema),
                    "strict": True,
                },
            }
        elif mode == "json_object":
            kwargs["response_format"] = {"type": "json_object"}
            system = f"{system}\n\n{schema_hint(schema)}"
        else:
            system = f"{system}\n\n{schema_hint(schema)}"

        response = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            **kwargs,
        )
        return response.choices[0].message.content or ""
