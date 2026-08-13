import re
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = ROOT / ".env"


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


def _quote_env_value(value: str) -> str:
    """Encadre la valeur de guillemets doubles, avec echappement minimal.

    Les mots de passe/cles peuvent contenir espaces, '#' ou '=' : sans
    guillemets, un parseur .env couperait la ligne au mauvais endroit.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def write_env_values(values: dict[str, str], *, env_path: Path = ENV_PATH) -> None:
    """Ecrit des paires cle/valeur dans le fichier .env, en place.

    Remplace la ligne existante quand la cle y figure deja (n'importe ou dans
    le fichier), l'ajoute sinon. Toutes les autres lignes — commentaires,
    reglages non touches — sont preservees telles quelles. `.env` est
    gitignore (verifie a l'ecriture de cette fonction) : ecrire dedans depuis
    l'UI n'expose donc rien au controle de version.
    """
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    remaining = dict(values)

    for i, line in enumerate(lines):
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", line)
        if match and match.group(1) in remaining:
            key = match.group(1)
            lines[i] = f"{key}={_quote_env_value(remaining.pop(key))}"

    if remaining:
        if lines and lines[-1].strip():
            lines.append("")
        for key, value in remaining.items():
            lines.append(f"{key}={_quote_env_value(value)}")

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def reload_settings() -> None:
    """Recharge `settings` depuis le `.env` courant, en mutant l'objet en place.

    `Settings` n'est pas figee (pas de `frozen=True`) : muter ses attributs,
    plutot que reassigner `settings`, rend le changement visible partout ou
    le module a fait `from .config import settings` — sans redemarrer jobot.
    """
    fresh = Settings()
    for name in Settings.model_fields:
        setattr(settings, name, getattr(fresh, name))


settings = Settings()
