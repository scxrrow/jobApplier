from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, Field


class Channel(StrEnum):
    """Comment on postule a cette offre. Determine le niveau d'automatisation."""

    EMAIL = "email"  # 100% auto : SMTP + PDF en piece jointe
    FORM = "form"  # assiste : Playwright headful pre-remplit, l'humain valide
    UNKNOWN = "unknown"  # manuel


class Status(StrEnum):
    NEW = "new"
    FILTERED_OUT = "filtered_out"
    SCORED = "scored"
    QUEUED = "queued"
    APPLIED = "applied"
    SKIPPED = "skipped"


# Sources dont l'API de recherche ne renvoie qu'un extrait de l'annonce.
EXCERPT_SOURCES = {"apec"}


class Offer(BaseModel):
    source: str
    native_id: str

    title: str
    company: str | None = None
    description: str = ""
    contract_type: str | None = None
    contract_label: str | None = None
    location: str | None = None
    postal_code: str | None = None
    department: str | None = None
    rome_code: str | None = None
    rome_label: str | None = None
    salary: str | None = None
    experience: str | None = None
    is_alternance: bool = False

    # Routage de candidature
    apply_email: str | None = None
    apply_url: str | None = None
    origin_url: str | None = None

    published_at: str | None = None
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def id(self) -> str:
        return f"{self.source}:{self.native_id}"

    @property
    def has_full_description(self) -> bool:
        return self.source not in EXCERPT_SOURCES

    @property
    def channel(self) -> Channel:
        if self.apply_email:
            return Channel.EMAIL
        if self.apply_url or self.origin_url:
            return Channel.FORM
        return Channel.UNKNOWN

    @property
    def content_hash(self) -> str:
        """Detecte une reecriture de l'offre cote employeur, pour re-scorer."""
        payload = "|".join(
            [self.title, self.company or "", self.description, self.salary or ""]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
