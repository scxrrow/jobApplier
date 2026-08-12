from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from .models import Offer, Status

SCHEMA = """
CREATE TABLE IF NOT EXISTS offers (
    id            TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    native_id     TEXT NOT NULL,

    title         TEXT NOT NULL,
    company       TEXT,
    description   TEXT NOT NULL DEFAULT '',
    contract_type TEXT,
    contract_label TEXT,
    location      TEXT,
    postal_code   TEXT,
    department    TEXT,
    rome_code     TEXT,
    rome_label    TEXT,
    salary        TEXT,
    experience    TEXT,
    is_alternance INTEGER NOT NULL DEFAULT 0,

    apply_email   TEXT,
    apply_url     TEXT,
    origin_url    TEXT,
    channel       TEXT NOT NULL,

    published_at  TEXT,
    fetched_at    TEXT NOT NULL,
    content_hash  TEXT NOT NULL,

    status        TEXT NOT NULL DEFAULT 'new',
    filter_reason TEXT,
    score         INTEGER,
    score_reason  TEXT,
    raw           TEXT
);

CREATE INDEX IF NOT EXISTS idx_offers_status  ON offers(status);
CREATE INDEX IF NOT EXISTS idx_offers_channel ON offers(channel);
CREATE INDEX IF NOT EXISTS idx_offers_score   ON offers(score DESC);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Ajoute les colonnes introduites apres la creation initiale de la table."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(offers)")}
    if "cv_selection" not in cols:
        conn.execute("ALTER TABLE offers ADD COLUMN cv_selection TEXT")
    conn.commit()


def upsert_offers(
    conn: sqlite3.Connection, offers: Iterable[Offer], raws: dict[str, dict] | None = None
) -> tuple[int, int, int]:
    """Insere les nouvelles offres, re-ouvre celles dont le contenu a change.

    Retourne (nouvelles, mises_a_jour, inchangees).
    """
    raws = raws or {}
    new = updated = unchanged = 0

    for offer in offers:
        row = conn.execute(
            "SELECT content_hash FROM offers WHERE id = ?", (offer.id,)
        ).fetchone()

        if row is None:
            conn.execute(
                """
                INSERT INTO offers (
                    id, source, native_id, title, company, description,
                    contract_type, contract_label, location, postal_code, department,
                    rome_code, rome_label, salary, experience, is_alternance,
                    apply_email, apply_url, origin_url, channel,
                    published_at, fetched_at, content_hash, status, raw
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    offer.id, offer.source, offer.native_id, offer.title, offer.company,
                    offer.description, offer.contract_type, offer.contract_label,
                    offer.location, offer.postal_code, offer.department,
                    offer.rome_code, offer.rome_label, offer.salary, offer.experience,
                    int(offer.is_alternance),
                    offer.apply_email, offer.apply_url, offer.origin_url,
                    str(offer.channel),
                    offer.published_at, offer.fetched_at.isoformat(), offer.content_hash,
                    str(Status.NEW),
                    json.dumps(raws.get(offer.id), ensure_ascii=False)
                    if offer.id in raws else None,
                ),
            )
            new += 1

        elif row["content_hash"] != offer.content_hash:
            # L'employeur a reecrit l'offre : on repasse en 'new' pour re-scorer.
            conn.execute(
                """
                UPDATE offers SET
                    title=?, company=?, description=?, salary=?,
                    apply_email=?, apply_url=?, origin_url=?, channel=?,
                    fetched_at=?, content_hash=?, status=?, filter_reason=NULL,
                    score=NULL, score_reason=NULL
                WHERE id=?
                """,
                (
                    offer.title, offer.company, offer.description, offer.salary,
                    offer.apply_email, offer.apply_url, offer.origin_url,
                    str(offer.channel),
                    offer.fetched_at.isoformat(), offer.content_hash,
                    str(Status.NEW), offer.id,
                ),
            )
            updated += 1
        else:
            unchanged += 1

    conn.commit()
    return new, updated, unchanged


def mark_filtered(conn: sqlite3.Connection, offer_id: str, reason: str) -> None:
    conn.execute(
        "UPDATE offers SET status=?, filter_reason=? WHERE id=?",
        (str(Status.FILTERED_OUT), reason, offer_id),
    )


def save_score(
    conn: sqlite3.Connection,
    offer_id: str,
    score: int,
    reason: str,
    selected_ids: list[str],
) -> None:
    conn.execute(
        "UPDATE offers SET status=?, score=?, score_reason=?, cv_selection=? WHERE id=?",
        (str(Status.SCORED), score, reason, json.dumps(selected_ids), offer_id),
    )


def counts_by_status(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM offers GROUP BY status"
    ).fetchall()
    return {r["status"]: r["n"] for r in rows}


def counts_by_channel(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT channel, COUNT(*) AS n FROM offers "
        "WHERE status != 'filtered_out' GROUP BY channel"
    ).fetchall()
    return {r["channel"]: r["n"] for r in rows}
