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

    # Sources d'offres interrogees par `jobot fetch`, dans l'ordre.
    jobot_sources: str = "francetravail,apec"

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

    # SMTP (canal 'email' uniquement). Pour Gmail/Outlook : mot de passe
    # d'application, jamais le mot de passe du compte.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_tls: str = "starttls"  # starttls | ssl | none

    db_path: Path = ROOT / "jobot.db"
    cv_path: Path = ROOT / "data" / "master-cv.json"
    out_dir: Path = ROOT / "out"
    chrome_profile: Path = ROOT / "chrome-profile"

    @property
    def sources(self) -> list[str]:
        return _split(self.jobot_sources)

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

    def require_smtp(self) -> None:
        checks = [("SMTP_HOST", self.smtp_host)]
        # Un relais local sans chiffrement n'exige generalement pas d'authentification.
        if self.smtp_tls.strip().lower() != "none":
            checks += [("SMTP_USER", self.smtp_user), ("SMTP_PASSWORD", self.smtp_password)]
        missing = [name for name, value in checks if not value]
        if missing:
            raise RuntimeError(
                f"Configuration SMTP incomplete : {', '.join(missing)} absent(s).\n"
                "  Renseigne-les dans .env (cf .env.example).\n"
                "  Gmail / Outlook : utilise un mot de passe d'application."
            )

    @property
    def sender_address(self) -> str:
        return self.smtp_from or self.smtp_user

    def require_master_cv(self) -> None:
        if not self.cv_path.exists():
            raise RuntimeError(
                f"CV maitre absent ({self.cv_path.name}).\n"
                "  jobot cv init            # partir du modele vierge\n"
                "  jobot cv import <fichier>  # extraire depuis un CV existant (HTML/texte)"
            )


settings = Settings()
