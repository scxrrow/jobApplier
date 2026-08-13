from __future__ import annotations

from pathlib import Path

from .cv import MasterCV
from .models import Offer


def apply_url(offer: Offer) -> str | None:
    return offer.apply_url or offer.origin_url


def clipboard_fields(cv: MasterCV, pdf_path: Path) -> list[tuple[str, str]]:
    """Informations a recopier dans le formulaire, dans l'ordre habituel."""
    fields = [("Nom", cv.personal.name), ("Email", cv.personal.email)]
    if cv.personal.phone:
        fields.append(("Telephone", cv.personal.phone))
    if cv.personal.github:
        fields.append(("GitHub", cv.personal.github))
    fields.append(("CV a joindre", str(pdf_path.resolve())))
    return fields


def open_application_page(url: str, profile_dir: Path) -> None:
    """Ouvre l'offre dans un Chromium visible, profil persistant.

    Le profil persiste entre les lancements pour garder les sessions ouvertes
    (LinkedIn, Welcome to the Jungle...) et eviter de se reconnecter a chaque
    candidature. Bloque jusqu'a ce que l'utilisateur ferme la fenetre : c'est
    lui qui remplit et qui clique sur 'Envoyer'.
    """
    from playwright.sync_api import sync_playwright

    profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            viewport=None,
            args=["--start-maximized"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(url, wait_until="domcontentloaded")

        # On rend la main quand l'utilisateur ferme le navigateur.
        context.wait_for_event("close", timeout=0)
