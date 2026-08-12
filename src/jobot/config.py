from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


def _split(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    ft_client_id: str = ""
    ft_client_secret: str = ""

    # LLM : n'importe quelle API OpenAI-compatible (LM Studio, Ollama, OpenAI,
    # OpenRouter...) ou Gemini natif. Voir llm/__init__.py pour les raccourcis.
    jobot_llm_provider: str = "gemini"
    jobot_llm_model: str = "gemini-flash-latest"
    jobot_llm_api_key: str = ""
    jobot_llm_base_url: str = ""

    # Criteres bruts (chaines separees par des virgules, cf .env.example)
    jobot_departements: str = "37"
    jobot_mots_cles: str = ""
    jobot_types_contrat: str = ""
    jobot_alternance_only: bool = False

    db_path: Path = ROOT / "jobot.db"
    cv_path: Path = ROOT / "data" / "master-cv.json"

    @property
    def departements(self) -> list[str]:
        return _split(self.jobot_departements)

    @property
    def mots_cles(self) -> list[str]:
        return _split(self.jobot_mots_cles)

    @property
    def types_contrat(self) -> list[str]:
        return _split(self.jobot_types_contrat)

    def require_ft_credentials(self) -> None:
        if not self.ft_client_id or not self.ft_client_secret:
            raise RuntimeError(
                "FT_CLIENT_ID / FT_CLIENT_SECRET absents.\n"
                "  1. Cree un compte sur https://francetravail.io\n"
                "  2. Souscris a l'API 'Offres d'emploi v2'\n"
                "  3. Copie .env.example vers .env et colle tes identifiants"
            )

    def require_master_cv(self) -> None:
        if not self.cv_path.exists():
            raise RuntimeError(
                f"CV maitre absent ({self.cv_path.name}).\n"
                "  jobot cv init            # partir du modele vierge\n"
                "  jobot cv import <fichier>  # extraire depuis un CV existant (HTML/texte)"
            )


settings = Settings()
