"""API locale de l'interface web (`jobot ui`).

Toutes les routes sont des `def` synchrones : FastAPI les execute dans son
threadpool, ce qui autorise SQLite et l'API sync de Playwright (le rendu PDF
et l'assistant navigateur plantent dans un thread qui porte une boucle asyncio).

La validation humaine se joue ici : `POST /api/offers/{id}/apply` n'est appele
que par le clic explicite de l'utilisateur dans la modale de confirmation — ou
par le pipeline en mode autonome, quand l'utilisateur a desactive la
verification au lancement de la recherche.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import db, pipeline
from .assist import apply_url as offer_apply_url
from .assist import clipboard_fields, open_application_page
from .autofill import auto_apply
from .config import settings
from .cv import extract_master_cv, html_to_text, load_master_cv, save_master_cv
from .llm import LLMError, build_client
from .mailer import build_body, build_subject
from .models import Channel, Status

WEBUI_DIR = Path(__file__).parent / "webui"

app = FastAPI(title="jobot", docs_url=None, redoc_url=None)


def _conn():
    return db.connect(settings.db_path)


def _safe_id(offer_id: str) -> str:
    return offer_id.replace(":", "_").replace("/", "_")


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
        "pdf_ready": (settings.out_dir / f"{_safe_id(row['id'])}.pdf").exists(),
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


def _llm_ok() -> bool:
    try:
        build_client(settings)
        return True
    except LLMError:
        return False


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEBUI_DIR / "index.html", media_type="text/html")


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
        "assist": _assist_snapshot(),
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
            raise HTTPException(400, "Canal formulaire : passe par l'assistant navigateur.")
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
def offer_pdf(offer_id: str) -> FileResponse:
    return _cv_file(offer_id, "pdf")


@app.get("/api/offers/{offer_id}/cv.html")
def offer_html(offer_id: str) -> FileResponse:
    return _cv_file(offer_id, "html")


def _cv_file(offer_id: str, kind: str) -> FileResponse:
    """Sert le CV adapte, en le generant au premier acces si besoin."""
    path = settings.out_dir / f"{_safe_id(offer_id)}.{kind}"
    if not path.exists():
        conn = _conn()
        try:
            row = _get_row(conn, offer_id)
        finally:
            conn.close()
        if not row["cv_selection"]:
            raise HTTPException(400, "Offre pas encore scorée : aucun CV à générer.")
        try:
            cv = load_master_cv(settings.cv_path)
            pipeline.build_cv_files(row, cv, settings.out_dir)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"Génération du CV en échec : {exc}")
    media = "application/pdf" if kind == "pdf" else "text/html"
    return FileResponse(path, media_type=media)


# ---------------------------------------------------------------------------
# Candidature par formulaire (canal `form`). Un seul navigateur a la fois.
# Deux modes, choisis par l'utilisateur : automatique (Playwright remplit et
# soumet, apres la validation de l'offre — le navigateur reste visible) ou
# manuel (la page s'ouvre, l'humain fait tout).

_ASSIST_LOCK = threading.Lock()
_ASSIST_IDLE: dict[str, Any] = {
    "offer_id": None,
    "status": "idle",
    "error": None,
    "fields": [],
    "mode": "manual",
    "report": None,
}
_ASSIST: dict[str, Any] = dict(_ASSIST_IDLE)


def _assist_snapshot() -> dict[str, Any]:
    with _ASSIST_LOCK:
        return dict(_ASSIST)


def _assist_thread(url: str, *, auto: bool, cv, pdf_path, message: str) -> None:
    try:
        if auto:
            def on_report(report) -> None:
                with _ASSIST_LOCK:
                    _ASSIST["report"] = report.as_dict()

            auto_apply(
                url,
                cv=cv,
                pdf_path=pdf_path,
                message=message,
                profile_dir=settings.chrome_profile,
                on_report=on_report,
            )
        else:
            open_application_page(url, settings.chrome_profile)
        with _ASSIST_LOCK:
            _ASSIST["status"] = "closed"
    except Exception as exc:  # noqa: BLE001
        with _ASSIST_LOCK:
            _ASSIST["status"] = "error"
            _ASSIST["error"] = str(exc)


class AssistStart(BaseModel):
    # None : suivre la preference form_auto memorisee avec la recherche.
    auto: bool | None = None


@app.post("/api/offers/{offer_id}/assist")
def assist_offer(offer_id: str, body: AssistStart | None = None) -> dict[str, Any]:
    conn = _conn()
    try:
        row = _get_row(conn, offer_id)
        if not row["cv_selection"]:
            raise HTTPException(400, "Offre pas encore scorée.")
        offer = pipeline.offer_from_row(row)
        url = offer_apply_url(offer)
        if not url:
            raise HTTPException(400, "Aucune URL de candidature pour cette offre.")

        with _ASSIST_LOCK:
            if _ASSIST["status"] == "open":
                raise HTTPException(409, "Un navigateur de candidature est déjà ouvert : ferme-le d'abord.")

        try:
            cv = load_master_cv(settings.cv_path)
            _, pdf_path = pipeline.build_cv_files(row, cv, settings.out_dir)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"Génération du CV en échec : {exc}")

        if row["status"] == str(Status.SCORED):
            db.set_status(conn, offer_id, Status.QUEUED)
            conn.commit()
    finally:
        conn.close()

    if body is not None and body.auto is not None:
        auto = body.auto
    else:
        saved = pipeline.load_ui_params()
        auto = saved.form_auto if saved else True

    fields = [{"label": lab, "value": val} for lab, val in clipboard_fields(cv, pdf_path)]
    with _ASSIST_LOCK:
        _ASSIST.update(
            {
                "offer_id": offer_id,
                "status": "open",
                "error": None,
                "fields": fields,
                "mode": "auto" if auto else "manual",
                "report": None,
            }
        )
    threading.Thread(
        target=_assist_thread,
        args=(url,),
        kwargs={
            "auto": auto,
            "cv": cv,
            "pdf_path": pdf_path,
            "message": build_body(cv, offer),
        },
        daemon=True,
    ).start()
    return {"opened": True, "url": url, "fields": fields, "mode": "auto" if auto else "manual"}


class AssistDone(BaseModel):
    applied: bool


@app.post("/api/assist/done")
def assist_done(body: AssistDone) -> dict[str, Any]:
    """L'utilisateur repond a « candidature envoyée ? » apres le navigateur."""
    with _ASSIST_LOCK:
        offer_id = _ASSIST["offer_id"]
        _ASSIST.update(dict(_ASSIST_IDLE))
    if offer_id and body.applied:
        conn = _conn()
        try:
            db.mark_applied(conn, offer_id)
            conn.commit()
        finally:
            conn.close()
    return {"ok": True}


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
