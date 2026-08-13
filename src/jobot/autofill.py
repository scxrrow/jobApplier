"""Remplissage et soumission automatiques des formulaires de candidature.

Ne se declenche qu'apres la validation explicite de la candidature par
l'utilisateur (clic par offre dans l'UI). Le navigateur reste visible du debut
a la fin : l'utilisateur voit ce qui est rempli, peut corriger, et confirme
lui-meme le statut final — la detection du succes d'une soumission n'est
jamais fiable a 100 %.

Le remplissage est heuristique (best effort) : chaque site de recrutement a
son propre formulaire. Ce module remplit ce qu'il reconnait, joint le CV,
et ne tente la soumission que si rien ne la bloque (connexion requise,
captcha, aucun champ reconnu). Le texte de motivation vient du template
email — jamais du LLM (invariant du projet).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Callable

from .cv import MasterCV

# Libelles/attributs reconnus -> valeur a inscrire. L'ordre compte : le
# premier motif qui matche gagne ('prenom' avant 'nom', sinon "Prénom"
# matcherait aussi le motif du nom).
_FIELD_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("prenom", re.compile(r"pr[eé]nom|first.?name", re.I)),
    ("nom", re.compile(r"\bnoms?\b|last.?name|family.?name|surname", re.I)),
    ("email", re.compile(r"e-?mail|courriel", re.I)),
    ("telephone", re.compile(r"t[eé]l[eé]?phone|\btel\b|phone|mobile|portable", re.I)),
    ("message", re.compile(r"motivation|message|commentaire|cover.?letter|presentation", re.I)),
]

_SUBMIT_RE = re.compile(
    r"envoyer\s+(ma\s+|la\s+)?candidature|postuler|candidater|je\s+postule"
    r"|soumettre|envoyer|submit|apply",
    re.I,
)
# Bouton qui devoile le formulaire quand la page d'offre n'en affiche pas.
_REVEAL_RE = re.compile(r"postuler|candidater|je\s+postule|apply", re.I)

_CAPTCHA_RE = re.compile(r"captcha|datadome|turnstile|challenge", re.I)


@dataclass
class AutofillReport:
    """Ce que l'automatisation a reussi a faire, montre tel quel dans l'UI."""

    filled: list[str] = dataclass_field(default_factory=list)
    uploaded: bool = False
    submitted: bool = False
    notes: list[str] = dataclass_field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "filled": self.filled,
            "uploaded": self.uploaded,
            "submitted": self.submitted,
            "notes": self.notes,
        }


def _values(cv: MasterCV, message: str) -> dict[str, str]:
    parts = cv.personal.name.split()
    return {
        "prenom": parts[0] if parts else cv.personal.name,
        "nom": " ".join(parts[1:]) or cv.personal.name,
        "email": cv.personal.email,
        "telephone": cv.personal.phone,
        "message": message,
    }


def _descriptor(el) -> str:
    """Texte agrege qui decrit un champ : attributs + label associe."""
    attrs = [
        el.get_attribute("name"),
        el.get_attribute("id"),
        el.get_attribute("placeholder"),
        el.get_attribute("aria-label"),
        el.get_attribute("autocomplete"),
    ]
    try:
        label = el.evaluate(
            "e => e.labels && e.labels.length ? e.labels[0].innerText : ''"
        )
    except Exception:  # noqa: BLE001 - un label illisible ne bloque pas le reste
        label = ""
    return " ".join(filter(None, attrs + [label]))


def _fill_frame(frame, values: dict[str, str], pdf_path: Path, report: AutofillReport) -> None:
    """Remplit les champs reconnus d'un frame ; joint le CV aux input file."""
    inputs = frame.locator(
        "input[type=text], input[type=email], input[type=tel], input:not([type]), textarea"
    )
    for i in range(inputs.count()):
        el = inputs.nth(i)
        try:
            if not el.is_visible() or el.input_value():
                continue  # champ cache ou deja rempli : on ne touche pas
            desc = _descriptor(el)
            input_type = (el.get_attribute("type") or "").lower()
            if input_type == "email":
                desc = f"email {desc}"
            elif input_type == "tel":
                desc = f"telephone {desc}"
            for key, pattern in _FIELD_PATTERNS:
                if pattern.search(desc):
                    el.fill(values[key])
                    report.filled.append(key)
                    break
        except Exception:  # noqa: BLE001 - un champ recalcitrant n'arrete pas le reste
            continue

    uploads = frame.locator("input[type=file]")
    for i in range(uploads.count()):
        try:
            uploads.nth(i).set_input_files(str(pdf_path))
            report.uploaded = True
            break
        except Exception:  # noqa: BLE001
            continue

    # Cases obligatoires (consentement RGPD etc.) : requises pour soumettre.
    boxes = frame.locator("form input[type=checkbox][required]")
    for i in range(boxes.count()):
        try:
            if boxes.nth(i).is_visible() and not boxes.nth(i).is_checked():
                boxes.nth(i).check()
        except Exception:  # noqa: BLE001
            continue


def _detect_blockers(page) -> list[str]:
    """Raisons de ne PAS soumettre automatiquement."""
    blockers = []
    try:
        if page.locator("input[type=password]").count():
            blockers.append("Le site demande une connexion : termine à la main.")
        for fr in page.frames:
            if _CAPTCHA_RE.search(fr.url or ""):
                blockers.append("Captcha détecté : termine à la main.")
                break
    except Exception:  # noqa: BLE001
        pass
    return blockers


def _click_first_match(page, pattern: re.Pattern) -> bool:
    for frame in page.frames:
        buttons = frame.locator("button, input[type=submit], input[type=button], a[role=button]")
        for i in range(buttons.count()):
            el = buttons.nth(i)
            try:
                text = el.inner_text() if el.evaluate("e => e.tagName") != "INPUT" \
                    else (el.get_attribute("value") or "")
                if el.is_visible() and pattern.search(text or ""):
                    el.click()
                    return True
            except Exception:  # noqa: BLE001
                continue
    return False


def auto_apply(
    url: str,
    *,
    cv: MasterCV,
    pdf_path: Path,
    message: str,
    profile_dir: Path,
    submit: bool = True,
    headless: bool = False,
    wait_close: bool = True,
    on_report: Callable[[AutofillReport], None] | None = None,
) -> AutofillReport:
    """Ouvre l'offre, remplit le formulaire, joint le CV, et tente l'envoi.

    N'appeler qu'apres validation explicite de la candidature par
    l'utilisateur. Le navigateur (profil persistant, sessions conservees)
    reste ouvert jusqu'a ce que l'utilisateur le ferme, pour verification.
    `headless`/`wait_close` ne servent qu'aux tests.
    """
    from playwright.sync_api import sync_playwright

    report = AutofillReport()
    values = _values(cv, message)
    profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=headless,
            viewport=None if not headless else {"width": 1280, "height": 900},
            args=[] if headless else ["--start-maximized"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)

            for frame in page.frames:
                _fill_frame(frame, values, pdf_path, report)

            if not report.filled and not report.uploaded:
                # La page d'offre n'affiche peut-etre le formulaire qu'apres
                # un clic sur « Postuler ».
                if _click_first_match(page, _REVEAL_RE):
                    page.wait_for_timeout(2500)
                    for frame in page.frames:
                        _fill_frame(frame, values, pdf_path, report)

            blockers = _detect_blockers(page)
            report.notes.extend(blockers)

            if not report.filled and not report.uploaded:
                report.notes.append(
                    "Aucun champ reconnu : formulaire non standard, remplis à la main."
                )
            elif submit and not blockers:
                if _click_first_match(page, _SUBMIT_RE):
                    page.wait_for_timeout(4000)
                    report.submitted = True
                else:
                    report.notes.append(
                        "Bouton d'envoi introuvable : clique toi-même pour soumettre."
                    )
        except Exception as exc:  # noqa: BLE001 - le rapport porte l'erreur, l'humain reprend
            report.notes.append(f"Automatisation interrompue : {exc}")

        if on_report:
            on_report(report)
        if wait_close:
            # L'utilisateur verifie le resultat et ferme lui-meme la fenetre.
            context.wait_for_event("close", timeout=0)
        else:
            context.close()

    return report
