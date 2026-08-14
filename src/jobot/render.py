from __future__ import annotations

from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .cv import Experience, MasterCV, Project, SkillCategory
from .models import Offer

TEMPLATE_DIR = Path(__file__).parent / "templates"

MONTHS_FR = (
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
)

_env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=select_autoescape(["html"]),
)


def _filter_skills(cv: MasterCV, selected: set[str]) -> list[SkillCategory]:
    """Toutes les competences restent ; les selectionnees passent en tete.

    Supprimer les tags non selectionnes appauvrissait trop le CV (et penalise
    le matching ATS) : la selection du LLM ordonne, elle ne retire rien.
    """
    result = []
    for category in cv.skills:
        picked = [item for item in category.items if item.id in selected]
        rest = [item for item in category.items if item.id not in selected]
        result.append(SkillCategory(category=category.category, items=picked + rest))
    return result


def _filter_experiences(cv: MasterCV, selected: set[str]) -> list[Experience]:
    """Les experiences sont toujours affichees en entier ; seuls les bullets sont
    filtres. Si aucun bullet d'une experience n'a ete selectionne, on les garde
    tous plutot que d'afficher une experience vide."""
    result = []
    for exp in cv.experiences:
        bullets = [b for b in exp.bullets if b.id in selected]
        result.append(exp.model_copy(update={"bullets": bullets or exp.bullets}))
    return result


def _filter_projects(cv: MasterCV, selected: set[str]) -> list[Project]:
    """Tous les projets restent, mais les projets selectionnes passent en tete.

    Comme pour les competences : la selection met en avant, elle ne supprime
    pas. Les bullets d'un projet sont filtres sur la selection, avec repli sur
    tous les bullets si aucun n'a ete retenu (jamais de projet vide).
    """
    picked, rest = [], []
    for project in cv.projects:
        bullets = [b for b in project.bullets if b.id in selected]
        rendered = project.model_copy(update={"bullets": bullets or project.bullets})
        if project.id in selected or bullets:
            picked.append(rendered)
        else:
            rest.append(rendered)
    return picked + rest


def render_html(cv: MasterCV, selected_ids: list[str]) -> str:
    selected = set(selected_ids)
    template = _env.get_template("cv.html.jinja")
    return template.render(
        cv=cv,
        skill_categories=_filter_skills(cv, selected),
        experiences=_filter_experiences(cv, selected),
        projects=_filter_projects(cv, selected),
    )


def _french_date(day: date) -> str:
    return f"{day.day} {MONTHS_FR[day.month - 1]} {day.year}"


def render_letter_html(cv: MasterCV, offer: Offer, paragraphs: list[str]) -> str:
    """Habille les paragraphes generes d'une lettre complete.

    Tout ce qui n'est pas dans `paragraphs` (en-tete, objet, formules) vient du
    template : voir letter.py pour le raisonnement.
    """
    template = _env.get_template("letter.html.jinja")
    return template.render(
        cv=cv,
        offer=offer,
        paragraphs=paragraphs,
        today=_french_date(date.today()),
    )


def render_pdf(html: str, output_path: Path) -> None:
    from playwright.sync_api import sync_playwright

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="load")
        page.emulate_media(media="print")
        page.pdf(
            path=str(output_path),
            format="A4",
            print_background=True,
            margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
        )
        browser.close()
