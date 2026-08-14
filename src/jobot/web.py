"""API locale de l'interface web (`jobot ui`).

Toutes les routes sont des `def` synchrones : FastAPI les execute dans son
threadpool, ce qui autorise SQLite et l'API sync de Playwright (le rendu PDF
plante dans un thread qui porte une boucle asyncio).

La validation humaine se joue ici. Deux chemins selon le canal de l'offre :

- `POST /api/offers/{id}/apply` envoie l'email, sur clic explicite de
  l'utilisateur (ou depuis le pipeline en mode autonome, quand il a desactive
  la verification au lancement de la recherche) ;
- `POST /api/offers/{id}/applied` enregistre une candidature deposee a la main
  sur le site de l'offre. jobot ne remplit aucun formulaire : il fournit le
  dossier, l'humain le depose.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, pipeline
from .config import reload_settings, settings, write_env_values
from .cv import extract_master_cv, html_to_text, load_master_cv, save_master_cv
from .letter import LetterDraft, suspect_terms
from .llm import PRESETS as LLM_PRESETS
from .llm import LLMError, build_client
from .mailer import build_body, build_subject
from .models import Channel, Status

WEBUI_DIR = Path(__file__).parent / "webui"

app = FastAPI(title="jobot", docs_url=None, redoc_url=None)
# CSS partagee entre les pages (index.html, candidatures.html).
app.mount("/assets", StaticFiles(directory=WEBUI_DIR), name="assets")


def _conn():
    return db.connect(settings.db_path)


def _get_row(conn, offer_id: str):
    row = conn.execute("SELECT * FROM offers WHERE id = ?", (offer_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"Aucune offre avec l'id '{offer_id}'.")
    return row


def _offer_json(row, *, description: bool = False) -> dict[str, Any]:
    data = {
        "id": row["id"],
        "source": row["source"],
        "title": row["title"],
        "company": row["company"],
        "location": row["location"],
        "department": row["department"],
        "contract": row["contract_label"] or row["contract_type"],
        "is_alternance": bool(row["is_alternance"]),
        "salary": row["salary"],
        "experience": row["experience"],
        "channel": row["channel"],
        "status": row["status"],
        "score": row["score"],
        "score_reason": row["score_reason"],
        "filter_reason": row["filter_reason"],
        "apply_email": row["apply_email"],
        "url": row["apply_url"] or row["origin_url"],
        "published_at": row["published_at"],
        "applied_at": row["applied_at"],
        "pdf_ready": (settings.out_dir / f"{pipeline.safe_id(row['id'])}.pdf").exists(),
        "letter_ready": bool(row["letter"]),
    }
    if description:
        data["description"] = row["description"]
    return data


def _cv_status() -> dict[str, Any]:
    if not settings.cv_path.exists():
        return {"present": False}
    try:
        cv = load_master_cv(settings.cv_path)
    except Exception as exc:  # noqa: BLE001 - erreurs de validation Pydantic incluses
        return {"present": True, "valid": False, "error": str(exc)}
    return {
        "present": True,
        "valid": True,
        "name": cv.personal.name,
        "headline": cv.personal.headline,
        "email": cv.personal.email,
        "selectable_ids": len(cv.selectable_ids()),
    }


def _smtp_ok() -> bool:
    try:
        settings.require_smtp()
        return True
    except RuntimeError:
        return False


def _ft_ok() -> bool:
    try:
        settings.require_ft_credentials()
        return True
    except RuntimeError:
        return False


def _lba_ok() -> bool:
    try:
        settings.require_lba_key()
        return True
    except RuntimeError:
        return False


def _llm_ok() -> bool:
    try:
        build_client(settings)
        return True
    except LLMError:
        return False


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEBUI_DIR / "index.html", media_type="text/html")


@app.get("/candidatures")
def candidatures_page() -> FileResponse:
    return FileResponse(WEBUI_DIR / "candidatures.html", media_type="text/html")


@app.get("/api/meta")
def meta() -> dict[str, Any]:
    saved = pipeline.load_ui_params()
    if saved is None:
        # Premiers reglages : on part de ce que le .env decrit deja.
        saved = pipeline.SearchParams(
            departements=settings.departements,
            contrat="alternance" if settings.jobot_alternance_only else "tous",
            mots_cles=settings.mots_cles,
            sources=[s for s in settings.sources if s in pipeline.SOURCE_NAMES],
        )
    return {
        "departements": [{"code": c, "nom": n} for c, n in pipeline.DEPARTEMENTS],
        "contrats": pipeline.CONTRACT_TYPES,
        "sources": list(pipeline.SOURCE_NAMES),
        "defaults": saved.model_dump(),
        "cv": _cv_status(),
        "smtp_ok": _smtp_ok(),
        "ft_ok": _ft_ok(),
        "lba_ok": _lba_ok(),
        "llm_ok": _llm_ok(),
        "llm": {"provider": settings.jobot_llm_provider, "model": settings.jobot_llm_model},
    }


@app.post("/api/search")
def search(params: pipeline.SearchParams) -> dict[str, Any]:
    if not params.departements:
        raise HTTPException(422, "Choisis au moins un département.")
    if not params.keywords():
        raise HTTPException(422, "Indique au moins un intitulé de poste ou un mot-clé.")
    unknown = [s for s in params.sources if s not in pipeline.SOURCE_NAMES]
    if unknown or not params.sources:
        raise HTTPException(422, "Sources invalides.")
    if not pipeline.start_run(params):
        raise HTTPException(409, "Une recherche est déjà en cours.")
    return {"started": True}


@app.post("/api/cancel")
def cancel() -> dict[str, Any]:
    pipeline.RUN.request_cancel()
    return {"cancelling": True}


@app.get("/api/state")
def state() -> dict[str, Any]:
    conn = _conn()
    try:
        by_status = db.counts_by_status(conn)
        by_channel = db.counts_by_channel(conn)
    finally:
        conn.close()
    return {
        "run": pipeline.RUN.snapshot(),
        "stats": {"by_status": by_status, "by_channel": by_channel},
    }


@app.get("/api/offers")
def offers(statuts: str = "scored,queued", limite: int = 100) -> list[dict[str, Any]]:
    wanted = [s.strip() for s in statuts.split(",") if s.strip()]
    if not wanted:
        return []
    marks = ",".join("?" for _ in wanted)
    conn = _conn()
    try:
        rows = conn.execute(
            f"SELECT * FROM offers WHERE status IN ({marks}) "
            "ORDER BY COALESCE(applied_at, '') DESC, COALESCE(score, -1) DESC, "
            "published_at DESC LIMIT ?",
            (*wanted, limite),
        ).fetchall()
    finally:
        conn.close()
    return [_offer_json(row) for row in rows]


@app.get("/api/offers/{offer_id}")
def offer_detail(offer_id: str) -> dict[str, Any]:
    conn = _conn()
    try:
        row = _get_row(conn, offer_id)
    finally:
        conn.close()
    return _offer_json(row, description=True)


@app.get("/api/offers/{offer_id}/preview")
def email_preview(offer_id: str) -> dict[str, Any]:
    """Ce que la validation va envoyer : destinataire, objet, corps."""
    conn = _conn()
    try:
        row = _get_row(conn, offer_id)
    finally:
        conn.close()
    if row["channel"] != str(Channel.EMAIL):
        raise HTTPException(400, "Cette offre ne se candidate pas par email.")

    offer = pipeline.offer_from_row(row)
    try:
        cv = load_master_cv(settings.cv_path)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"CV maître illisible : {exc}")
    return {
        "to": offer.apply_email,
        "from": f"{cv.personal.name} <{settings.sender_address}>" if _smtp_ok() else None,
        "subject": build_subject(offer),
        "body": build_body(cv, offer),
        "smtp_ok": _smtp_ok(),
    }


@app.post("/api/offers/{offer_id}/skip")
def skip_offer(offer_id: str) -> dict[str, Any]:
    conn = _conn()
    try:
        row = _get_row(conn, offer_id)
        if row["status"] not in (str(Status.SCORED), str(Status.QUEUED)):
            raise HTTPException(400, f"Offre au statut '{row['status']}', pas écartable.")
        db.set_status(conn, offer_id, Status.SKIPPED)
        conn.commit()
        row = _get_row(conn, offer_id)
    finally:
        conn.close()
    return _offer_json(row)


@app.post("/api/offers/{offer_id}/apply")
def apply_offer(offer_id: str) -> dict[str, Any]:
    """Envoie la candidature email. Appele par le clic de validation de l'UI."""
    if not _smtp_ok():
        raise HTTPException(
            400,
            "SMTP non configuré : renseigne SMTP_HOST / SMTP_USER / SMTP_PASSWORD "
            "dans le fichier .env puis relance jobot ui.",
        )
    conn = _conn()
    try:
        row = _get_row(conn, offer_id)
        if row["status"] not in (str(Status.SCORED), str(Status.QUEUED)):
            raise HTTPException(400, f"Offre au statut '{row['status']}', pas envoyable.")
        if row["channel"] != str(Channel.EMAIL):
            raise HTTPException(
                400,
                "Cette offre se dépose sur son site : récupère le CV et la lettre, "
                "puis marque la candidature comme envoyée.",
            )
        if not row["cv_selection"]:
            raise HTTPException(400, "Offre pas encore scorée.")

        try:
            cv = load_master_cv(settings.cv_path)
            db.set_status(conn, offer_id, Status.QUEUED)
            conn.commit()
            pipeline.send_application(conn, row, cv)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - l'UI affiche le message tel quel
            raise HTTPException(500, f"Envoi en échec : {exc}")
        row = _get_row(conn, offer_id)
    finally:
        conn.close()
    return _offer_json(row)


@app.get("/api/offers/{offer_id}/cv.pdf")
def offer_cv_pdf(offer_id: str) -> FileResponse:
    return _document(offer_id, "cv", "pdf")


@app.get("/api/offers/{offer_id}/cv.html")
def offer_cv_html(offer_id: str) -> FileResponse:
    return _document(offer_id, "cv", "html")


@app.get("/api/offers/{offer_id}/lettre.pdf")
def offer_letter_pdf(offer_id: str) -> FileResponse:
    return _document(offer_id, "letter", "pdf")


@app.get("/api/offers/{offer_id}/lettre.html")
def offer_letter_html(offer_id: str) -> FileResponse:
    return _document(offer_id, "letter", "html")


def _document(offer_id: str, kind: str, ext: str) -> FileResponse:
    """Sert une piece du dossier, en la rendant au premier acces si besoin.

    Le rendu PDF est paresseux : la generation du pipeline couvre les offres
    au-dessus du seuil, mais le candidat peut ouvrir n'importe quelle offre
    scoree, y compris sous le seuil.
    """
    stem = pipeline.safe_id(offer_id) + ("-lettre" if kind == "letter" else "")
    path = settings.out_dir / f"{stem}.{ext}"

    if not path.exists():
        conn = _conn()
        try:
            row = _get_row(conn, offer_id)
        finally:
            conn.close()
        if not row["cv_selection"]:
            raise HTTPException(400, "Offre pas encore scorée : rien à générer.")
        if kind == "letter" and not row["letter"]:
            raise HTTPException(
                400, "Aucune lettre pour cette offre : lance la génération d'abord."
            )
        try:
            cv = load_master_cv(settings.cv_path)
            if kind == "letter":
                pipeline.build_letter_files(row, cv, settings.out_dir)
            else:
                pipeline.build_cv_files(row, cv, settings.out_dir)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"Génération en échec : {exc}")

    media = "application/pdf" if ext == "pdf" else "text/html"
    return FileResponse(path, media_type=media)


# ---------------------------------------------------------------------------
# Le dossier de candidature. jobot ne remplit plus aucun formulaire : chaque
# plateforme de recrutement a le sien, et aucune heuristique ne tenait sur
# l'ensemble. Il prepare le CV adapte et la lettre, ouvre l'offre, et c'est le
# candidat qui depose — puis qui confirme l'envoi.


@app.get("/api/offers/{offer_id}/kit")
def offer_kit(offer_id: str) -> dict[str, Any]:
    """Tout ce qu'il faut pour candidater a une offre, en un appel."""
    conn = _conn()
    try:
        row = _get_row(conn, offer_id)
    finally:
        conn.close()

    if not row["cv_selection"]:
        raise HTTPException(400, "Offre pas encore scorée.")

    offer = pipeline.offer_from_row(row)
    quoted = quote(offer_id, safe="")
    kit: dict[str, Any] = {
        "offer": _offer_json(row, description=True),
        "apply_link": offer.apply_link,
        "cv_url": f"/api/offers/{quoted}/cv.pdf",
        "letter": None,
        "letter_url": None,
        "flagged_terms": [],
    }

    if row["letter"]:
        try:
            cv = load_master_cv(settings.cv_path)
            draft = LetterDraft.model_validate_json(row["letter"])
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"Lettre illisible : {exc}")
        kit["letter"] = draft.text()
        kit["letter_url"] = f"/api/offers/{quoted}/lettre.pdf"
        # Relire la lettre est obligatoire de toute facon (c'est le candidat
        # qui la depose) : autant lui montrer ou regarder en priorite.
        kit["flagged_terms"] = suspect_terms(draft.text(), offer, cv)

    return kit


@app.post("/api/offers/{offer_id}/letter")
def regenerate_letter(offer_id: str) -> dict[str, Any]:
    """Reecrit la lettre d'une offre. Le candidat peut relancer autant qu'il veut."""
    try:
        client = build_client(settings)
    except LLMError as exc:
        raise HTTPException(400, str(exc))

    conn = _conn()
    try:
        row = _get_row(conn, offer_id)
        if not row["cv_selection"]:
            raise HTTPException(400, "Offre pas encore scorée.")
        try:
            cv = load_master_cv(settings.cv_path)
            draft = pipeline.draft_letter(conn, row, cv, client=client)
            pipeline.build_letter_files(row, cv, settings.out_dir, draft)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"Rédaction en échec : {exc}")
        offer = pipeline.offer_from_row(row)
    finally:
        conn.close()

    return {
        "letter": draft.text(),
        "letter_url": f"/api/offers/{quote(offer_id, safe='')}/lettre.pdf",
        "flagged_terms": suspect_terms(draft.text(), offer, cv),
    }


@app.post("/api/offers/{offer_id}/applied")
def mark_applied(offer_id: str) -> dict[str, Any]:
    """Le candidat a depose son dossier sur le site de l'offre.

    Aucune detection automatique : jobot n'a pas suivi le candidat sur la
    plateforme, seul lui sait si la candidature est bien partie.
    """
    conn = _conn()
    try:
        row = _get_row(conn, offer_id)
        if row["status"] == str(Status.APPLIED):
            raise HTTPException(400, "Candidature déjà enregistrée.")
        db.mark_applied(conn, offer_id)
        conn.commit()
        row = _get_row(conn, offer_id)
    finally:
        conn.close()
    return _offer_json(row)


# ---------------------------------------------------------------------------
# Reglages LLM et SMTP. Ecrits dans .env (gitignore, jamais commite) puis
# rechargeables a chaud sans redemarrer `jobot ui` — voir config.reload_settings.
# Les secrets (cle API, mot de passe SMTP) ne sont jamais renvoyes en clair :
# seul un booleen "deja defini" sort de l'API.

LLM_PROVIDERS = ["gemini", *LLM_PRESETS.keys(), "openai_compat"]
SMTP_TLS_MODES = ("starttls", "ssl", "none")

_ENV_KEYS = {
    "llm_provider": "JOBOT_LLM_PROVIDER",
    "llm_model": "JOBOT_LLM_MODEL",
    "llm_base_url": "JOBOT_LLM_BASE_URL",
    "llm_api_key": "JOBOT_LLM_API_KEY",
    "ft_client_id": "FT_CLIENT_ID",
    "ft_client_secret": "FT_CLIENT_SECRET",
    "lba_api_key": "LBA_API_KEY",
    "smtp_host": "SMTP_HOST",
    "smtp_port": "SMTP_PORT",
    "smtp_user": "SMTP_USER",
    "smtp_password": "SMTP_PASSWORD",
    "smtp_from": "SMTP_FROM",
    "smtp_tls": "SMTP_TLS",
}


def _settings_snapshot() -> dict[str, Any]:
    return {
        "llm": {
            "provider": settings.jobot_llm_provider,
            "model": settings.jobot_llm_model,
            "base_url": settings.jobot_llm_base_url,
            "api_key_set": bool(settings.jobot_llm_api_key),
            "providers": LLM_PROVIDERS,
        },
        "smtp": {
            "host": settings.smtp_host,
            "port": settings.smtp_port,
            "user": settings.smtp_user,
            "from_": settings.smtp_from,
            "tls": settings.smtp_tls,
            "tls_modes": list(SMTP_TLS_MODES),
            "password_set": bool(settings.smtp_password),
        },
        "sources": {
            # Les identifiants des sources qui en demandent. Comme pour le LLM
            # et le SMTP, seuls des booleens sortent : jamais le secret.
            "ft_client_id": settings.ft_client_id,
            "ft_secret_set": bool(settings.ft_client_secret),
            "lba_key_set": bool(settings.lba_api_key),
        },
        "llm_ok": _llm_ok(),
        "smtp_ok": _smtp_ok(),
        "ft_ok": _ft_ok(),
        "lba_ok": _lba_ok(),
    }


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    return _settings_snapshot()


class SettingsUpdate(BaseModel):
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_base_url: str | None = None
    # Chaine vide = effacement volontaire (distingue de "champ absent" via
    # model_fields_set plus bas, pas de la valeur elle-meme).
    llm_api_key: str | None = None
    ft_client_id: str | None = None
    ft_client_secret: str | None = None
    lba_api_key: str | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_from: str | None = None
    smtp_tls: str | None = None


@app.put("/api/settings")
def update_settings(body: SettingsUpdate) -> dict[str, Any]:
    sent = body.model_fields_set  # seuls les champs presents dans le JSON recu
    if not sent:
        return _settings_snapshot()

    if "llm_provider" in sent and body.llm_provider not in LLM_PROVIDERS:
        raise HTTPException(422, f"Fournisseur LLM invalide. Valeurs acceptées : {', '.join(LLM_PROVIDERS)}")
    if "smtp_tls" in sent and body.smtp_tls not in SMTP_TLS_MODES:
        raise HTTPException(422, f"Mode TLS invalide. Valeurs acceptées : {', '.join(SMTP_TLS_MODES)}")

    values = {
        _ENV_KEYS[field]: str(getattr(body, field))
        for field in sent
        if getattr(body, field) is not None
    }
    if values:
        write_env_values(values)
        reload_settings()
    return _settings_snapshot()


# ---------------------------------------------------------------------------
# CV maitre : consultation et import depuis l'UI (extraction LLM).


class CvImport(BaseModel):
    content: str
    filename: str = ""
    force: bool = False


@app.get("/api/cv")
def cv_get() -> dict[str, Any]:
    return _cv_status()


@app.post("/api/cv/import")
def cv_import(body: CvImport) -> dict[str, Any]:
    if settings.cv_path.exists() and not body.force:
        raise HTTPException(409, "Un CV maître existe déjà. Confirme pour l'écraser.")
    if not body.content.strip():
        raise HTTPException(422, "Contenu vide.")

    try:
        client = build_client(settings)
    except LLMError as exc:
        raise HTTPException(400, str(exc))

    is_html = body.filename.lower().endswith((".html", ".htm")) or "<html" in body.content[:500].lower()
    source = html_to_text(body.content) if is_html else body.content
    try:
        cv = extract_master_cv(client, source)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"Extraction en échec : {exc}")

    save_master_cv(cv, settings.cv_path)
    return _cv_status()
