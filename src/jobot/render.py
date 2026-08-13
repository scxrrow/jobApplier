from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .cv import Experience, MasterCV, Project, SkillCategory

TEMPLATE_DIR = Path(__file__).parent / "templates"

_env = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=select_autoescape(["html"]),
)


def _filter_skills(cv: MasterCV, selected: set[str]) -> list[SkillCategory]:
    """Ne garde que les tags selectionnes ; enleve les categories vides."""
    result = []
    for category in cv.skills:
        items = [item for item in category.items if item.id in selected]
        if items:
            result.append(SkillCategory(category=category.category, items=items))
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
    """Un projet est retenu si son id ou au moins un de ses bullets a ete
    selectionne. Si le projet est retenu mais aucun bullet individuellement,
    on garde tous ses bullets."""
    result = []
    for project in cv.projects:
        bullets = [b for b in project.bullets if b.id in selected]
        if project.id in selected or bullets:
            result.append(project.model_copy(update={"bullets": bullets or project.bullets}))
    return result


def render_html(cv: MasterCV, selected_ids: list[str]) -> str:
    selected = set(selected_ids)
    template = _env.get_template("cv.html.jinja")
    return template.render(
        cv=cv,
        skill_categories=_filter_skills(cv, selected),
        experiences=_filter_experiences(cv, selected),
        projects=_filter_projects(cv, selected),
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
