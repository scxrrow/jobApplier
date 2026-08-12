from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel

from .config import ROOT

if TYPE_CHECKING:
    from .llm import LLMClient

MASTER_CV_PATH = ROOT / "data" / "master-cv.json"
EXAMPLE_CV_PATH = ROOT / "data" / "master-cv.example.json"


class Personal(BaseModel):
    name: str
    headline: str
    phone: str
    email: str
    github: str
    extra: str | None = None


class SkillItem(BaseModel):
    id: str
    label: str


class SkillCategory(BaseModel):
    category: str
    items: list[SkillItem]


class Language(BaseModel):
    language: str
    level: str
    detail: str | None = None


class Bullet(BaseModel):
    id: str
    text: str


class Experience(BaseModel):
    id: str
    title: str
    company: str
    dates: str
    bullets: list[Bullet]
    tech_stack: str | None = None


class Project(BaseModel):
    id: str
    title: str
    bullets: list[Bullet]


class Education(BaseModel):
    id: str
    title: str
    school: str


class MasterCV(BaseModel):
    personal: Personal
    summary: str
    skills: list[SkillCategory]
    languages: list[Language]
    experiences: list[Experience]
    projects: list[Project]
    education: list[Education]
    interests: list[str]

    def selectable_ids(self) -> set[str]:
        """Tous les id que le LLM peut choisir : tags de competences, projets
        (au niveau du projet) et bullets d'experience/projet.

        Sert a valider programmatiquement une selection du LLM : chaque id
        qu'il renvoie doit appartenir a cet ensemble, sinon on le rejette.
        """
        ids: set[str] = set()
        for category in self.skills:
            ids.update(item.id for item in category.items)
        for exp in self.experiences:
            ids.update(bullet.id for bullet in exp.bullets)
        for project in self.projects:
            ids.add(project.id)
            ids.update(bullet.id for bullet in project.bullets)
        return ids


def load_master_cv(path: Path = MASTER_CV_PATH) -> MasterCV:
    return MasterCV.model_validate(json.loads(path.read_text(encoding="utf-8")))


def save_master_cv(cv: MasterCV, path: Path = MASTER_CV_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cv.model_dump_json(indent=2), encoding="utf-8")


_SCRIPTS = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_TAGS = re.compile(r"<[^>]+>")
_BLANKS = re.compile(r"\n\s*\n+")

IMPORT_PROMPT = """Tu convertis un CV en JSON structure.

Regles :
- N'invente aucune information : reprends uniquement ce qui figure dans le CV fourni.
- Chaque `id` doit etre unique, en minuscules, sans accent, mots separes par des tirets.
- Conventions d'id : `skill-<outil>` pour les competences, `exp-<entreprise>-<role>` pour
  une experience et `<id-experience>-b1`, `-b2`... pour ses bullets, `proj-<nom-court>`
  pour un projet et `<id-projet>-b1`, `-b2`... pour ses bullets, `edu-<ecole>` pour une
  formation.
- Decoupe chaque experience et chaque projet en bullets courts et autonomes : ce sont les
  briques que l'on selectionnera ensuite pour composer un CV adapte a chaque offre.
- `summary` : deux phrases de presentation neutres, deduites du CV."""


def html_to_text(raw: str) -> str:
    """Reduit un CV HTML a son texte, pour ne pas payer le balisage en tokens."""
    text = _SCRIPTS.sub(" ", raw)
    text = _TAGS.sub("\n", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&#39;", "'")
        .replace("&quot;", '"')
    )
    lines = [line.strip() for line in text.splitlines()]
    return _BLANKS.sub("\n\n", "\n".join(line for line in lines if line))


def extract_master_cv(client: LLMClient, source: str) -> MasterCV:
    """Construit un master CV a partir du texte brut d'un CV existant."""
    return client.complete_json(
        system=IMPORT_PROMPT,
        user=f"CV a convertir :\n\n{source}",
        schema=MasterCV,
    )
