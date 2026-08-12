from __future__ import annotations

from ..config import Settings
from .base import LLMClient, LLMError, parse_json_response

# base_url pretes a l'emploi, pour que .env reste lisible.
PRESETS = {
    "lmstudio": "http://localhost:1234/v1",
    "ollama": "http://localhost:11434/v1",
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}


def build_client(settings: Settings) -> LLMClient:
    """Instancie le backend LLM decrit par la configuration."""
    provider = settings.jobot_llm_provider.strip().lower()

    if provider == "gemini":
        from .gemini import GeminiClient

        if not settings.jobot_llm_api_key:
            raise LLMError(_missing_key_message("gemini"))
        return GeminiClient(
            api_key=settings.jobot_llm_api_key,
            model=settings.jobot_llm_model or "gemini-flash-latest",
        )

    if provider in PRESETS or provider == "openai_compat":
        from .openai_compat import OpenAICompatClient

        base_url = settings.jobot_llm_base_url or PRESETS.get(provider)
        if not base_url:
            raise LLMError(
                "JOBOT_LLM_BASE_URL est requis avec JOBOT_LLM_PROVIDER=openai_compat.\n"
                f"  Raccourcis disponibles : {', '.join(PRESETS)}"
            )
        if not settings.jobot_llm_model:
            raise LLMError(
                "JOBOT_LLM_MODEL est requis (nom exact du modele servi par l'API)."
            )
        # Les serveurs locaux n'ont pas de cle ; les APIs distantes en exigent une.
        if not settings.jobot_llm_api_key and "localhost" not in base_url:
            raise LLMError(_missing_key_message(provider))

        return OpenAICompatClient(
            api_key=settings.jobot_llm_api_key,
            base_url=base_url,
            model=settings.jobot_llm_model,
        )

    raise LLMError(
        f"JOBOT_LLM_PROVIDER='{provider}' inconnu.\n"
        f"  Valeurs acceptees : gemini, openai_compat, {', '.join(PRESETS)}"
    )


def _missing_key_message(provider: str) -> str:
    urls = {
        "gemini": "https://aistudio.google.com/apikey",
        "openai": "https://platform.openai.com/api-keys",
        "openrouter": "https://openrouter.ai/keys",
    }
    hint = f"\n  Cree une cle sur {urls[provider]}" if provider in urls else ""
    return f"JOBOT_LLM_API_KEY absent (requis pour provider={provider}).{hint}"


__all__ = ["LLMClient", "LLMError", "build_client", "parse_json_response", "PRESETS"]
