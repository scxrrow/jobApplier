from __future__ import annotations

import smtplib
import unicodedata
from email.message import EmailMessage
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from .config import Settings
from .cv import MasterCV
from .models import Offer

TEMPLATE_DIR = Path(__file__).parent / "templates"

# Pas d'autoescape : c'est du texte brut, pas du HTML.
_env = Environment(loader=FileSystemLoader(TEMPLATE_DIR), trim_blocks=True)


def _ascii_filename(text: str) -> str:
    """Nom de piece jointe sans accent ni espace, lisible par tous les clients."""
    plain = unicodedata.normalize("NFD", text)
    plain = "".join(c for c in plain if unicodedata.category(c) != "Mn")
    keep = [c if c.isalnum() else "-" for c in plain]
    return "-".join("".join(keep).split("-")).strip("-") or "cv"


def build_subject(offer: Offer) -> str:
    return f"Candidature - {offer.title}"


def build_body(cv: MasterCV, offer: Offer) -> str:
    return _env.get_template("email.txt.jinja").render(cv=cv, offer=offer)


def build_message(
    *,
    settings: Settings,
    cv: MasterCV,
    offer: Offer,
    pdf_path: Path,
    letter_path: Path | None = None,
) -> EmailMessage:
    if not offer.apply_email:
        raise ValueError(f"L'offre {offer.id} n'a pas d'adresse de candidature.")

    msg = EmailMessage()
    msg["From"] = f"{cv.personal.name} <{settings.sender_address}>"
    msg["To"] = offer.apply_email
    msg["Reply-To"] = cv.personal.email
    msg["Subject"] = build_subject(offer)
    msg.set_content(build_body(cv, offer))

    who = _ascii_filename(cv.personal.name)
    attachments = [(pdf_path, f"CV-{who}.pdf")]
    if letter_path is not None:
        attachments.append((letter_path, f"Lettre-de-motivation-{who}.pdf"))

    for path, filename in attachments:
        msg.add_attachment(
            path.read_bytes(),
            maintype="application",
            subtype="pdf",
            filename=filename,
        )
    return msg


def send_message(msg: EmailMessage, settings: Settings) -> None:
    """Envoie via SMTP.

    `smtp_tls` : 'ssl' (SSL implicite, port 465), 'starttls' (defaut, port 587),
    ou 'none' pour un relais local sans chiffrement.
    """
    mode = settings.smtp_tls.strip().lower()

    if mode == "ssl" or settings.smtp_port == 465:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30) as server:
            if settings.smtp_user:
                server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(msg)
        return

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as server:
        if mode != "none":
            server.starttls()
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(msg)
