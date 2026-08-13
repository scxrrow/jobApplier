"""Orchestration du pipeline complet : fetch -> filtre -> score -> generation -> envoi.

Utilise par l'UI web (`jobot ui`) pour derouler toute la chaine en une passe,
et par le CLI pour les etapes unitaires. Le comportement par defaut garde la
validation humaine avant tout envoi ; le mode autonome (validation desactivee)
est un choix explicite de l'utilisateur au lancement de la recherche.

Meme en mode autonome, le canal `form` n'est jamais soumis automatiquement :
l'assistant navigateur reste le seul chemin, avec un clic humain final.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

from . import db
from .config import settings
from .cv import MasterCV, load_master_cv
from .filters import DEFAULT_EXCLUDE, FilterRules, normalize
from .llm import build_client
from .mailer import build_message, send_message
from .models import Channel, Offer, Status
from .render import render_html, render_pdf
from .sources import (
    ApecClient,
    ApecError,
    FranceTravailClient,
    FranceTravailError,
    parse_apec_offer,
    parse_ft_offer,
)

SOURCE_NAMES = ("francetravail", "apec")

# Types de contrat proposes dans l'UI. La valeur est celle de
# `filters.CONTRACT_KINDS` (+ 'tous' pour ne pas filtrer).
CONTRACT_TYPES = [
    {"id": "alternance", "label": "Alternance"},
    {"id": "cdi", "label": "CDI"},
    {"id": "cdd", "label": "CDD"},
    {"id": "stage", "label": "Stage"},
    {"id": "interim", "label": "Intérim"},
    {"id": "tous", "label": "Tous types"},
]

# France Travail : parametre `typeContrat` de l'API pour chaque type.
# L'alternance passe par le drapeau dedie, le stage n'a pas de code officiel
# cote FT (le filtre client `FilterRules.contrat` s'en charge).
FT_CONTRACT_PARAM = {"cdi": "CDI", "cdd": "CDD", "interim": "MIS"}

# Familles de postes proposees dans l'UI. Chaque mot-cle declenche une requete
# par departement sur chaque source : listes volontairement courtes.
DOMAINES: dict[str, dict[str, Any]] = {
    "support": {
        "label": "Support technique",
        "mots_cles": ["technicien support", "support informatique", "helpdesk"],
    },
    "sysres": {
        "label": "Systèmes & réseaux",
        "mots_cles": ["administrateur systeme", "administrateur reseau"],
    },
    "cyber": {
        "label": "Cybersécurité",
        "mots_cles": ["cybersecurite", "analyste SOC", "pentest"],
    },
    "dev": {
        "label": "Développement",
        "mots_cles": ["developpeur"],
    },
    "devops": {
        "label": "DevOps / Cloud",
        "mots_cles": ["devops", "ingenieur cloud"],
    },
    "data": {
        "label": "Data / BI",
        "mots_cles": ["data analyst", "data engineer"],
    },
    "rh": {
        "label": "Ressources humaines",
        "mots_cles": ["ressources humaines", "charge de recrutement", "gestionnaire de paie"],
    },
    "compta": {
        "label": "Comptabilité / Gestion",
        "mots_cles": ["comptable", "assistant de gestion"],
    },
    "marketing": {
        "label": "Marketing / Communication",
        "mots_cles": ["marketing", "communication"],
    },
    "commerce": {
        "label": "Commerce / Vente",
        "mots_cles": ["commercial", "conseiller de vente"],
    },
    "logistique": {
        "label": "Logistique / Supply chain",
        "mots_cles": ["logistique", "supply chain"],
    },
    "juridique": {
        "label": "Juridique",
        "mots_cles": ["juriste", "assistant juridique"],
    },
}

DEPARTEMENTS = [
    ("01", "Ain"), ("02", "Aisne"), ("03", "Allier"), ("04", "Alpes-de-Haute-Provence"),
    ("05", "Hautes-Alpes"), ("06", "Alpes-Maritimes"), ("07", "Ardèche"), ("08", "Ardennes"),
    ("09", "Ariège"), ("10", "Aube"), ("11", "Aude"), ("12", "Aveyron"),
    ("13", "Bouches-du-Rhône"), ("14", "Calvados"), ("15", "Cantal"), ("16", "Charente"),
    ("17", "Charente-Maritime"), ("18", "Cher"), ("19", "Corrèze"), ("2A", "Corse-du-Sud"),
    ("2B", "Haute-Corse"), ("21", "Côte-d'Or"), ("22", "Côtes-d'Armor"), ("23", "Creuse"),
    ("24", "Dordogne"), ("25", "Doubs"), ("26", "Drôme"), ("27", "Eure"),
    ("28", "Eure-et-Loir"), ("29", "Finistère"), ("30", "Gard"), ("31", "Haute-Garonne"),
    ("32", "Gers"), ("33", "Gironde"), ("34", "Hérault"), ("35", "Ille-et-Vilaine"),
    ("36", "Indre"), ("37", "Indre-et-Loire"), ("38", "Isère"), ("39", "Jura"),
    ("40", "Landes"), ("41", "Loir-et-Cher"), ("42", "Loire"), ("43", "Haute-Loire"),
    ("44", "Loire-Atlantique"), ("45", "Loiret"), ("46", "Lot"), ("47", "Lot-et-Garonne"),
    ("48", "Lozère"), ("49", "Maine-et-Loire"), ("50", "Manche"), ("51", "Marne"),
    ("52", "Haute-Marne"), ("53", "Mayenne"), ("54", "Meurthe-et-Moselle"), ("55", "Meuse"),
    ("56", "Morbihan"), ("57", "Moselle"), ("58", "Nièvre"), ("59", "Nord"),
    ("60", "Oise"), ("61", "Orne"), ("62", "Pas-de-Calais"), ("63", "Puy-de-Dôme"),
    ("64", "Pyrénées-Atlantiques"), ("65", "Hautes-Pyrénées"), ("66", "Pyrénées-Orientales"),
    ("67", "Bas-Rhin"), ("68", "Haut-Rhin"), ("69", "Rhône"), ("70", "Haute-Saône"),
    ("71", "Saône-et-Loire"), ("72", "Sarthe"), ("73", "Savoie"), ("74", "Haute-Savoie"),
    ("75", "Paris"), ("76", "Seine-Maritime"), ("77", "Seine-et-Marne"), ("78", "Yvelines"),
    ("79", "Deux-Sèvres"), ("80", "Somme"), ("81", "Tarn"), ("82", "Tarn-et-Garonne"),
    ("83", "Var"), ("84", "Vaucluse"), ("85", "Vendée"), ("86", "Vienne"),
    ("87", "Haute-Vienne"), ("88", "Vosges"), ("89", "Yonne"), ("90", "Territoire de Belfort"),
    ("91", "Essonne"), ("92", "Hauts-de-Seine"), ("93", "Seine-Saint-Denis"),
    ("94", "Val-de-Marne"), ("95", "Val-d'Oise"), ("971", "Guadeloupe"), ("972", "Martinique"),
    ("973", "Guyane"), ("974", "La Réunion"), ("976", "Mayotte"),
]

# Valeurs acceptees par France Travail pour `publieeDepuis`.
FT_DAYS = (1, 3, 7, 14, 31)

UI_PARAMS_PATH = settings.cv_path.parent / "ui-params.json"

Log = Callable[[str, str], None]  # (level: info|warn|error|success, message)


class SearchParams(BaseModel):
    """Criteres d'une recherche lancee depuis l'UI (ou construits par le CLI)."""

    departements: list[str] = Field(default_factory=list)
    contrat: str = "alternance"
    domaines: list[str] = Field(default_factory=list)
    mots_cles: list[str] = Field(default_factory=list)
    jours: int = 7
    sources: list[str] = Field(default_factory=lambda: list(SOURCE_NAMES))
    validation_humaine: bool = True
    score_min: int = 60
    limite_score: int = 100
    max_par_requete: int = 600

    def keywords(self) -> list[str]:
        """Union ordonnee des mots-cles des domaines choisis et des mots-cles libres."""
        seen: set[str] = set()
        result: list[str] = []
        for domaine in self.domaines:
            for kw in DOMAINES.get(domaine, {}).get("mots_cles", []):
                if normalize(kw) not in seen:
                    seen.add(normalize(kw))
                    result.append(kw)
        for kw in self.mots_cles:
            kw = kw.strip()
            if kw and normalize(kw) not in seen:
                seen.add(normalize(kw))
                result.append(kw)
        return result

    def exclusions(self) -> list[str]:
        """Exclusions de titre par defaut, moins celles qui contredisent la recherche.

        Chercher 'commercial' avec 'commercial' dans la liste d'exclusion
        rejetterait tout : un terme exclu qui recoupe un mot-cle choisi saute.
        """
        kws = [normalize(k) for k in self.keywords()]
        return [
            term
            for term in DEFAULT_EXCLUDE
            if not any(normalize(term) in kw or kw in normalize(term) for kw in kws)
        ]

    def contract_kind(self) -> str:
        kind = self.contrat.strip().lower()
        return "" if kind in ("", "tous") else kind

    def rules(self) -> FilterRules:
        return FilterRules(
            departements=self.departements,
            mots_cles=self.keywords(),
            contrat=self.contract_kind(),
            exclude=self.exclusions(),
        )


def save_ui_params(params: SearchParams) -> None:
    UI_PARAMS_PATH.parent.mkdir(parents=True, exist_ok=True)
    UI_PARAMS_PATH.write_text(params.model_dump_json(indent=2), encoding="utf-8")


def load_ui_params() -> SearchParams | None:
    if not UI_PARAMS_PATH.exists():
        return None
    try:
        return SearchParams.model_validate_json(UI_PARAMS_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - fichier corrompu = on repart des defauts
        return None


class Cancelled(RuntimeError):
    """La recherche a ete interrompue depuis l'UI."""


# Etapes affichees par l'UI, dans l'ordre d'execution.
STEP_NAMES = ("fetch", "filtre", "score", "generation", "envoi")


@dataclass
class RunState:
    """Etat observable d'une execution du pipeline, partage avec l'UI via l'API.

    Toutes les mutations passent par les methodes, qui prennent le verrou :
    le thread du pipeline ecrit, les requetes HTTP lisent des instantanes.
    """

    lock: threading.Lock = field(default_factory=threading.Lock)
    running: bool = False
    cancel_requested: bool = False
    step: str = "idle"
    steps: dict[str, str] = field(default_factory=dict)
    logs: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    params: SearchParams | None = None

    def start(self, params: SearchParams) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.cancel_requested = False
            self.step = "preflight"
            self.steps = {name: "pending" for name in STEP_NAMES}
            self.logs = []
            self.error = None
            self.started_at = time.time()
            self.finished_at = None
            self.params = params
            return True

    def log(self, level: str, message: str) -> None:
        with self.lock:
            self.logs.append({"ts": time.time(), "level": level, "message": message})
            # L'UI n'affiche que la fin ; on borne pour ne pas grossir sans fin.
            if len(self.logs) > 500:
                del self.logs[: len(self.logs) - 500]

    def begin(self, step: str) -> None:
        with self.lock:
            self.step = step
            self.steps[step] = "running"

    def end(self, step: str, status: str = "done") -> None:
        with self.lock:
            self.steps[step] = status

    def finish(self, error: str | None = None) -> None:
        with self.lock:
            self.running = False
            self.step = "error" if error else "done"
            self.error = error
            self.finished_at = time.time()
            if error:
                for name, value in self.steps.items():
                    if value == "running":
                        self.steps[name] = "error"

    def request_cancel(self) -> None:
        with self.lock:
            if self.running:
                self.cancel_requested = True

    def check_cancel(self) -> None:
        with self.lock:
            cancelled = self.cancel_requested
        if cancelled:
            raise Cancelled("Recherche interrompue.")

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "running": self.running,
                "step": self.step,
                "steps": dict(self.steps),
                "logs": list(self.logs[-200:]),
                "error": self.error,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "params": self.params.model_dump() if self.params else None,
                "validation_humaine": self.params.validation_humaine if self.params else True,
            }


# Etat global du serveur web : une seule recherche a la fois.
RUN = RunState()


def _ft_days(jours: int) -> int:
    """France Travail n'accepte que certaines valeurs de `publieeDepuis`."""
    for allowed in FT_DAYS:
        if jours <= allowed:
            return allowed
    return FT_DAYS[-1]


def fetch_offers(
    params: SearchParams, log: Log, check_cancel: Callable[[], None] = lambda: None
) -> tuple[list[Offer], dict[str, dict]]:
    """Interroge chaque source sur toutes les combinaisons departement x mot-cle."""
    offers: list[Offer] = []
    raws: dict[str, dict] = {}
    seen: set[str] = set()
    kind = params.contract_kind()

    combos = [
        (dep, kw)
        for dep in (params.departements or [None])
        for kw in (params.keywords() or [None])
    ]

    for name in params.sources:
        log("info", f"Source {name} : {len(combos)} requête(s)")
        if name == "francetravail":
            client = FranceTravailClient(settings.ft_client_id, settings.ft_client_secret)
            error_type = FranceTravailError
        else:
            client = ApecClient()
            error_type = ApecError

        with client:
            for dep, kw in combos:
                check_cancel()
                label = f"dép {dep or '*'} · « {kw or '*'} »"
                try:
                    if name == "francetravail":
                        raw_offers = client.search(
                            mots_cles=kw,
                            departement=dep,
                            type_contrat=FT_CONTRACT_PARAM.get(kind),
                            alternance=kind == "alternance",
                            publiee_depuis=_ft_days(params.jours),
                            max_results=params.max_par_requete,
                        )
                    else:
                        raw_offers = client.search(
                            mots_cles=kw,
                            departement=dep,
                            type_contrat=kind or None,
                            publiee_depuis=params.jours,
                            max_results=params.max_par_requete,
                        )
                except error_type as exc:
                    log("warn", f"{name} {label} : {exc}")
                    continue

                parse = parse_ft_offer if name == "francetravail" else parse_apec_offer
                fresh = 0
                for raw in raw_offers:
                    offer = parse(raw)
                    if offer.id in seen:
                        continue
                    seen.add(offer.id)
                    offers.append(offer)
                    raws[offer.id] = raw
                    fresh += 1
                log("info", f"{name} {label} : {len(raw_offers)} reçues, {fresh} uniques")

    return offers, raws


def apply_filters(conn, rules: FilterRules, log: Log) -> tuple[int, int]:
    """Filtre a regles sur toutes les offres 'new'. Retourne (gardees, ecartees)."""
    rows = conn.execute(
        "SELECT * FROM offers WHERE status = ?", (str(Status.NEW),)
    ).fetchall()
    rejected = 0
    for row in rows:
        offer = offer_from_row(row)
        reason = rules.check(offer)
        if reason:
            db.mark_filtered(conn, offer.id, reason)
            rejected += 1
    conn.commit()
    kept = len(rows) - rejected
    log("info", f"Filtres : {rejected} écartée(s), {kept} retenue(s)")
    return kept, rejected


def score_pending(
    conn,
    params: SearchParams,
    log: Log,
    *,
    client,
    cv: MasterCV,
    check_cancel: Callable[[], None] = lambda: None,
) -> int:
    """Score les offres 'new' avec le LLM. Retourne le nombre d'offres scorees."""
    from .scoring import score_offer

    rows = conn.execute(
        "SELECT * FROM offers WHERE status = ? ORDER BY published_at DESC LIMIT ?",
        (str(Status.NEW), params.limite_score),
    ).fetchall()

    scored = 0
    for i, row in enumerate(rows, 1):
        check_cancel()
        offer = offer_from_row(row)
        try:
            result = score_offer(client, offer, cv)
        except Exception as exc:  # noqa: BLE001 - on continue sur les autres offres
            log("error", f"Score en échec pour « {offer.title[:60]} » : {exc}")
            continue
        db.save_score(conn, offer.id, result.score, result.reason, result.selected_ids)
        conn.commit()
        scored += 1
        log("info", f"[{i}/{len(rows)}] {result.score}/100 — {offer.title[:70]}")
    return scored


def build_cv_files(row, cv: MasterCV, out_dir: Path) -> tuple[Path, Path]:
    """Rend le CV adapte a une offre scoree. Retourne (html_path, pdf_path)."""
    html = render_html(cv, json.loads(row["cv_selection"]))
    safe_id = row["id"].replace(":", "_").replace("/", "_")
    html_path = out_dir / f"{safe_id}.html"
    pdf_path = out_dir / f"{safe_id}.pdf"

    out_dir.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html, encoding="utf-8")
    render_pdf(html, pdf_path)
    return html_path, pdf_path


def generate_for_candidates(
    conn,
    params: SearchParams,
    log: Log,
    *,
    cv: MasterCV,
    check_cancel: Callable[[], None] = lambda: None,
) -> int:
    """Genere les CV PDF des offres scorees au-dessus du seuil (si pas deja faits)."""
    rows = conn.execute(
        "SELECT * FROM offers WHERE status IN (?, ?) AND COALESCE(score, 0) >= ? "
        "ORDER BY score DESC",
        (str(Status.SCORED), str(Status.QUEUED), params.score_min),
    ).fetchall()

    generated = 0
    for row in rows:
        check_cancel()
        safe_id = row["id"].replace(":", "_").replace("/", "_")
        if (settings.out_dir / f"{safe_id}.pdf").exists():
            continue
        try:
            build_cv_files(row, cv, settings.out_dir)
            generated += 1
            log("info", f"CV généré — {row['title'][:70]}")
        except Exception as exc:  # noqa: BLE001 - on continue sur les autres offres
            log("error", f"Génération du CV en échec pour « {row['title'][:50]} » : {exc}")
    return generated


def send_application(conn, row, cv: MasterCV) -> None:
    """Genere le CV, construit l'email et l'envoie. Marque l'offre 'applied'.

    L'appelant est responsable de la validation humaine (ou de son
    desactivation explicite par l'utilisateur en mode autonome).
    """
    offer = offer_from_row(row)
    _, pdf_path = build_cv_files(row, cv, settings.out_dir)
    msg = build_message(settings=settings, cv=cv, offer=offer, pdf_path=pdf_path)
    send_message(msg, settings)
    db.mark_applied(conn, offer.id)
    conn.commit()


def auto_dispatch(
    conn,
    params: SearchParams,
    log: Log,
    *,
    cv: MasterCV,
    smtp_ok: bool,
    check_cancel: Callable[[], None] = lambda: None,
) -> dict[str, int]:
    """Mode autonome : met en file les offres au-dessus du seuil et envoie les emails.

    Le canal `form` n'est jamais soumis automatiquement : ces offres restent en
    file pour l'assistant navigateur (clic humain final).
    """
    rows = conn.execute(
        "SELECT * FROM offers WHERE status = ? AND COALESCE(score, 0) >= ? "
        "ORDER BY score DESC",
        (str(Status.SCORED), params.score_min),
    ).fetchall()
    for row in rows:
        db.set_status(conn, row["id"], Status.QUEUED)
    conn.commit()
    if rows:
        log("info", f"{len(rows)} offre(s) ≥ {params.score_min}/100 mises en file")

    sent = failed = forms = 0
    queued = conn.execute(
        "SELECT * FROM offers WHERE status = ? ORDER BY score DESC",
        (str(Status.QUEUED),),
    ).fetchall()

    for row in queued:
        check_cancel()
        if row["channel"] != str(Channel.EMAIL):
            forms += 1
            continue
        if not smtp_ok:
            failed += 1
            continue
        try:
            send_application(conn, row, cv)
            sent += 1
            log("success", f"Candidature envoyée → {row['apply_email']} ({row['title'][:50]})")
        except Exception as exc:  # noqa: BLE001 - on continue sur les autres offres
            failed += 1
            log("error", f"Envoi en échec pour « {row['title'][:50]} » : {exc}")

    if forms:
        log(
            "warn",
            f"{forms} candidature(s) par formulaire en attente : jobot ne soumet "
            "jamais un formulaire à ta place — ouvre l'assistant depuis l'interface.",
        )
    if not smtp_ok and failed:
        log("warn", "SMTP non configuré : les candidatures email restent en file.")
    return {"sent": sent, "failed": failed, "forms": forms}


def run_pipeline(params: SearchParams, state: RunState) -> None:
    """Deroule le pipeline complet. Concu pour tourner dans un thread dedie."""
    try:
        # Pre-vol : echouer tot, avec des messages actionnables dans l'UI.
        try:
            settings.require_master_cv()
            cv = load_master_cv(settings.cv_path)
        except Exception as exc:  # noqa: BLE001 - erreurs Pydantic incluses
            raise RuntimeError(f"CV maître : {exc}") from exc
        client = build_client(settings)

        if "francetravail" in params.sources:
            settings.require_ft_credentials()

        smtp_ok = True
        try:
            settings.require_smtp()
        except RuntimeError:
            smtp_ok = False
        if not params.validation_humaine and not smtp_ok:
            state.log(
                "warn",
                "SMTP non configuré : le mode autonome mettra les candidatures "
                "email en file sans pouvoir les envoyer.",
            )

        conn = db.connect(settings.db_path)
        try:
            state.begin("fetch")
            offers, raws = fetch_offers(params, state.log, state.check_cancel)
            new, updated, unchanged = db.upsert_offers(conn, offers, raws)
            state.log(
                "info",
                f"{len(offers)} offres uniques — {new} nouvelles, "
                f"{updated} mises à jour, {unchanged} inchangées",
            )
            state.end("fetch")

            state.begin("filtre")
            # Les criteres viennent de changer avec cette recherche : tout ce
            # que les filtres avaient ecarte repasse a l'examen.
            reset = db.reset_filtered(conn)
            if reset:
                state.log("info", f"{reset} offre(s) précédemment écartée(s) réexaminée(s)")
            apply_filters(conn, params.rules(), state.log)
            state.end("filtre")

            state.begin("score")
            scored = score_pending(
                conn, params, state.log, client=client, cv=cv,
                check_cancel=state.check_cancel,
            )
            state.log("info", f"{scored} offre(s) scorée(s)")
            state.end("score")

            state.begin("generation")
            generate_for_candidates(
                conn, params, state.log, cv=cv, check_cancel=state.check_cancel
            )
            state.end("generation")

            if params.validation_humaine:
                state.end("envoi", "waiting")
                state.log(
                    "success",
                    "Recherche terminée — les candidatures attendent ta validation ci-dessous.",
                )
            else:
                state.begin("envoi")
                auto_dispatch(
                    conn, params, state.log, cv=cv, smtp_ok=smtp_ok,
                    check_cancel=state.check_cancel,
                )
                state.end("envoi")
                state.log("success", "Recherche terminée en mode autonome.")
        finally:
            conn.close()

        state.finish()
    except Cancelled:
        state.log("warn", "Recherche interrompue.")
        state.finish("Recherche interrompue.")
    except Exception as exc:  # noqa: BLE001 - l'erreur part telle quelle dans l'UI
        state.log("error", str(exc))
        state.finish(str(exc))


def start_run(params: SearchParams) -> bool:
    """Lance le pipeline en tache de fond. False si une recherche tourne deja."""
    if not RUN.start(params):
        return False
    save_ui_params(params)
    thread = threading.Thread(target=run_pipeline, args=(params, RUN), daemon=True)
    thread.start()
    return True


def offer_from_row(row) -> Offer:
    """Reconstruit un `Offer` depuis une ligne SQLite."""
    return Offer(
        source=row["source"],
        native_id=row["native_id"],
        title=row["title"],
        company=row["company"],
        description=row["description"],
        contract_type=row["contract_type"],
        contract_label=row["contract_label"],
        location=row["location"],
        postal_code=row["postal_code"],
        department=row["department"],
        rome_code=row["rome_code"],
        rome_label=row["rome_label"],
        salary=row["salary"],
        experience=row["experience"],
        is_alternance=bool(row["is_alternance"]),
        apply_email=row["apply_email"],
        apply_url=row["apply_url"],
        origin_url=row["origin_url"],
        published_at=row["published_at"],
    )
