from __future__ import annotations

from google import genai
from google.genai import types

from .base import ModelT, parse_json_response


class GeminiClient:
    """Backend Gemini natif.

    `response_schema` contraint la sortie de facon plus fiable que le shim
    OpenAI-compatible de Google, d'ou ce backend dedie.
    """

    def __init__(self, *, api_key: str, model: str):
        self._client = genai.Client(api_key=api_key)
        self._model = model

    def complete_json(self, *, system: str, user: str, schema: type[ModelT]) -> ModelT:
        response = self._client.models.generate_content(
            model=self._model,
            contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_schema=schema,
            ),
        )
        parsed = response.parsed
        if isinstance(parsed, schema):
            return parsed
        return parse_json_response(response.text or "", schema)
