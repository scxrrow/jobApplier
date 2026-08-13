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
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, pipeline
from .assist import apply_url as offer_apply_url
from .assist import clipboard_fields, open_application_page
from .autofill import auto_apply
from .config import reload_settings, settings, write_env_values
from .cv import extract_master_cv, html_to_text, load_master_cv, save_master_cv
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
    "login_domain": None,
}
_ASSIST: dict[str, Any] = dict(_ASSIST_IDLE)

# Debloque le thread d'autofill en pause sur un mur de connexion.
_RESUME = threading.Event()

# Au-dela, on considere que l'utilisateur a abandonne la connexion et on
# laisse la candidature en attente plutot que de bloquer le thread a vie.
LOGIN_TIMEOUT_S = 900


def _assist_snapshot() -> dict[str, Any]:
    with _ASSIST_LOCK:
        return dict(_ASSIST)


def _on_login_required(domain: str) -> None:
    _RESUME.clear()
    with _ASSIST_LOCK:
        _ASSIST["status"] = "login_required"
        _ASSIST["login_domain"] = domain


def _wait_for_resume() -> bool:
    """Bloque jusqu'au clic sur « Reprendre » dans l'UI (ou expiration).

    Ne prend surtout pas `_ASSIST_LOCK` : l'UI doit pouvoir lire l'etat
    pendant toute l'attente.
    """
    resumed = _RESUME.wait(timeout=LOGIN_TIMEOUT_S)
    with _ASSIST_LOCK:
        if _ASSIST["status"] == "login_required":
            _ASSIST["status"] = "open"
    return resumed


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
                on_login_required=_on_login_required,
                wait_for_resume=_wait_for_resume,
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
            if _ASSIST["status"] in ("open", "login_required"):
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
    _RESUME.clear()
    with _ASSIST_LOCK:
        _ASSIST.update(
            {
                "offer_id": offer_id,
                "status": "open",
                "error": None,
                "fields": fields,
                "mode": "auto" if auto else "manual",
                "report": None,
                "login_domain": None,
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


@app.post("/api/assist/resume")
def assist_resume() -> dict[str, Any]:
    """L'utilisateur s'est connecté : l'autofill reprend où il s'était arrêté."""
    with _ASSIST_LOCK:
        if _ASSIST["status"] != "login_required":
            raise HTTPException(409, "Aucune candidature en attente de connexion.")
    _RESUME.set()
    return {"resumed": True}


@app.post("/api/assist/done")
def assist_done(body: AssistDone) -> dict[str, Any]:
    """L'utilisateur repond a « candidature envoyée ? » apres le navigateur."""
    # Debloque un thread encore en attente de connexion, sinon il tiendrait
    # le navigateur ouvert jusqu'a l'expiration.
    _RESUME.set()
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
# Connexions aux plateformes. Le mur d'authentification est par site, pas par
# offre : une connexion dans le profil persistant vaut pour toutes les offres
# du domaine, et pour les runs suivants. Cet ecran permet de les faire d'avance
# plutot que de se faire interrompre en pleine serie de candidatures.


@app.get("/api/logins")
def logins() -> list[dict[str, Any]]:
    """Plateformes presentes dans les offres en attente, par volume."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT apply_url, origin_url FROM offers WHERE status IN (?, ?, ?)",
            (str(Status.SCORED), str(Status.QUEUED), str(Status.NEW)),
        ).fetchall()
    finally:
        conn.close()

    by_domain: dict[str, dict[str, Any]] = {}
    for row in rows:
        url = row["apply_url"] or row["origin_url"]
        if not url:
            continue
        domain = urlparse(url).netloc
        if not domain:
            continue
        entry = by_domain.setdefault(domain, {"domain": domain, "offers": 0, "url": url})
        entry["offers"] += 1
    return sorted(by_domain.values(), key=lambda e: -e["offers"])


class LoginOpen(BaseModel):
    domain: str


@app.post("/api/logins/open")
def login_open(body: LoginOpen) -> dict[str, Any]:
    """Ouvre le navigateur sur une plateforme pour s'y connecter une fois."""
    target = next((e for e in logins() if e["domain"] == body.domain), None)
    if target is None:
        raise HTTPException(404, f"Aucune offre connue sur {body.domain}.")

    with _ASSIST_LOCK:
        if _ASSIST["status"] in ("open", "login_required"):
            raise HTTPException(409, "Un navigateur est déjà ouvert : ferme-le d'abord.")
        _ASSIST.update(
            {
                **_ASSIST_IDLE,
                "status": "open",
                "mode": "login",
                "login_domain": body.domain,
            }
        )

    def run() -> None:
        try:
            open_application_page(target["url"], settings.chrome_profile)
            with _ASSIST_LOCK:
                _ASSIST["status"] = "closed"
        except Exception as exc:  # noqa: BLE001
            with _ASSIST_LOCK:
                _ASSIST["status"] = "error"
                _ASSIST["error"] = str(exc)

    threading.Thread(target=run, daemon=True).start()
    return {"opened": True, "domain": body.domain, "url": target["url"]}


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
        "llm_ok": _llm_ok(),
        "smtp_ok": _smtp_ok(),
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
