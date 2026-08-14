from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from .models import Offer, Status, dedup_key_for

# Proprietaire de toutes les offres tant que jobot tourne en local, pour un
# seul candidat. Voir `_migrate` pour le raisonnement.
LOCAL_USER = "local"

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
    if "applied_at" not in cols:
        conn.execute("ALTER TABLE offers ADD COLUMN applied_at TEXT")
    if "letter" not in cols:
        # Lettre de motivation generee, en JSON (cf. letter.LetterDraft).
        conn.execute("ALTER TABLE offers ADD COLUMN letter TEXT")
    if "dedup_key" not in cols:
        # Rapproche la meme annonce publiee sur plusieurs sources.
        conn.execute("ALTER TABLE offers ADD COLUMN dedup_key TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_offers_dedup ON offers(dedup_key)")
    # Hors du bloc ci-dessus, et donc rejoue a chaque ouverture : une colonne
    # ajoutee par une version anterieure du code n'aurait jamais ete remplie,
    # et un rattrapage interrompu doit pouvoir reprendre.
    _backfill_dedup_keys(conn)
    if "user_id" not in cols:
        # jobot est mono-utilisateur pour l'instant : tout appartient a
        # LOCAL_USER. La colonne existe des maintenant pour qu'un passage en
        # multi-utilisateur soit une migration de donnees et non une reecriture
        # des requetes — toutes les lectures filtrent deja dessus.
        conn.execute(
            f"ALTER TABLE offers ADD COLUMN user_id TEXT NOT NULL DEFAULT '{LOCAL_USER}'"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_offers_user ON offers(user_id)")

    # Le canal 'form' designait un formulaire que jobot remplissait lui-meme.
    # L'auto-remplissage a ete abandonne (trop de plateformes, aucune ne se
    # ressemble) : le canal s'appelle desormais 'site' et signifie « le
    # candidat depose son dossier la-bas ».
    conn.execute("UPDATE offers SET channel = 'site' WHERE channel = 'form'")
    conn.commit()


def _backfill_dedup_keys(conn: sqlite3.Connection) -> None:
    """Calcule la cle de dedup des offres deja en base.

    Sans ce rattrapage, les offres anterieures a la colonne resteraient a NULL
    et ne bloqueraient jamais un doublon venu d'une nouvelle source.
    """
    rows = conn.execute(
        "SELECT id, title, company FROM offers WHERE dedup_key IS NULL"
    ).fetchall()
    conn.executemany(
        "UPDATE offers SET dedup_key = ? WHERE id = ?",
        [(dedup_key_for(r["id"], r["title"], r["company"]), r["id"]) for r in rows],
    )


def upsert_offers(
    conn: sqlite3.Connection, offers: Iterable[Offer], raws: dict[str, dict] | None = None
) -> tuple[int, int, int, int]:
    """Insere les nouvelles offres, re-ouvre celles dont le contenu a change.

    Retourne (nouvelles, mises_a_jour, inchangees, doublons_inter_sources).
    """
    raws = raws or {}
    new = updated = unchanged = cross = 0

    for offer in offers:
        row = conn.execute(
            "SELECT content_hash FROM offers WHERE id = ?", (offer.id,)
        ).fetchone()

        if row is None:
            # La meme annonce est peut-etre deja en base via une autre source :
            # l'ignorer economise un scoring, un CV et une lettre.
            twin = conn.execute(
                "SELECT id FROM offers WHERE dedup_key = ? AND id != ? LIMIT 1",
                (offer.dedup_key, offer.id),
            ).fetchone()
            if twin is not None:
                cross += 1
                continue

            conn.execute(
                """
                INSERT INTO offers (
                    id, source, native_id, title, company, description,
                    contract_type, contract_label, location, postal_code, department,
                    rome_code, rome_label, salary, experience, is_alternance,
                    apply_email, apply_url, origin_url, channel,
                    published_at, fetched_at, content_hash, status, raw, dedup_key
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                    offer.dedup_key,
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
                    score=NULL, score_reason=NULL, letter=NULL, dedup_key=?
                WHERE id=?
                """,
                (
                    offer.title, offer.company, offer.description, offer.salary,
                    offer.apply_email, offer.apply_url, offer.origin_url,
                    str(offer.channel),
                    offer.fetched_at.isoformat(), offer.content_hash,
                    str(Status.NEW), offer.dedup_key, offer.id,
                ),
            )
            updated += 1
        else:
            unchanged += 1

    conn.commit()
    return new, updated, unchanged, cross


def update_routing(conn: sqlite3.Connection, offer: Offer) -> bool:
    """Reecrit les champs de candidature d'une offre stockee, si besoin.

    Sert aux corrections de parsing appliquees a posteriori (nouvelle source
    d'URL, adresse email invalide) : le JSON brut est conserve en base, on
    peut donc re-router sans rappeler l'API. Ne touche ni au statut, ni au
    score, ni a la selection de CV. Retourne True si quelque chose a change.
    """
    row = conn.execute(
        "SELECT apply_email, apply_url, origin_url, channel FROM offers WHERE id = ?",
        (offer.id,),
    ).fetchone()
    if row is None:
        return False

    fields = (offer.apply_email, offer.apply_url, offer.origin_url, str(offer.channel))
    if tuple(row) == fields:
        return False

    conn.execute(
        "UPDATE offers SET apply_email=?, apply_url=?, origin_url=?, channel=? WHERE id=?",
        (*fields, offer.id),
    )
    return True


def mark_filtered(conn: sqlite3.Connection, offer_id: str, reason: str) -> None:
    conn.execute(
        "UPDATE offers SET status=?, filter_reason=? WHERE id=?",
        (str(Status.FILTERED_OUT), reason, offer_id),
    )


def reset_filtered(conn: sqlite3.Connection) -> int:
    """Repasse les offres ecartees par les filtres en 'new'.

    Les criteres changent d'une recherche a l'autre (UI) : une offre rejetee
    hier peut passer aujourd'hui. Sans effet sur les statuts decides par
    l'humain ou le scoring (scored/queued/skipped/applied).
    """
    cur = conn.execute(
        "UPDATE offers SET status=?, filter_reason=NULL WHERE status=?",
        (str(Status.NEW), str(Status.FILTERED_OUT)),
    )
    conn.commit()
    return cur.rowcount


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


def save_letter(conn: sqlite3.Connection, offer_id: str, letter_json: str) -> None:
    """Stocke la lettre generee. Sans effet sur le statut : une lettre peut
    etre regeneree autant de fois que le candidat le souhaite avant d'envoyer."""
    conn.execute("UPDATE offers SET letter=? WHERE id=?", (letter_json, offer_id))


def set_status(conn: sqlite3.Connection, offer_id: str, status: Status) -> None:
    conn.execute("UPDATE offers SET status=? WHERE id=?", (str(status), offer_id))


def mark_applied(conn: sqlite3.Connection, offer_id: str) -> None:
    conn.execute(
        "UPDATE offers SET status=?, applied_at=? WHERE id=?",
        (str(Status.APPLIED), datetime.now(timezone.utc).isoformat(), offer_id),
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
