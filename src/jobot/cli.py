from __future__ import annotations

import json
import shutil
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import db, pipeline
from .config import settings
from .cv import (
    EXAMPLE_CV_PATH,
    extract_master_cv,
    html_to_text,
    load_master_cv,
    save_master_cv,
)
from .assist import apply_url, clipboard_fields, open_application_page
from .llm import LLMError, build_client
from .mailer import build_message, build_subject, send_message
from .models import Channel, Status

app = typer.Typer(add_completion=False, help="Pipeline de candidature automatisee.")
cv_app = typer.Typer(help="Gestion du CV maitre (data/master-cv.json).")
app.add_typer(cv_app, name="cv")
console = Console()


def _llm_client():
    """Instancie le backend LLM, avec un message clair si la config est incomplete."""
    try:
        return build_client(settings)
    except LLMError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)


def _console_log(level: str, message: str) -> None:
    """Adapte le journal du pipeline aux couleurs rich du terminal."""
    colors = {"warn": "yellow", "error": "red", "success": "green"}
    color = colors.get(level)
    console.print(f"[{color}]{message}[/{color}]" if color else message)


def _env_params(**overrides) -> pipeline.SearchParams:
    """Criteres de recherche construits depuis le .env, pour les commandes CLI."""
    return pipeline.SearchParams(
        departements=settings.departements,
        contrat="alternance" if settings.jobot_alternance_only else "tous",
        mots_cles=settings.mots_cles,
        sources=settings.sources,
        **overrides,
    )


@app.command()
def ui(
    host: str = typer.Option("127.0.0.1", help="Adresse d'ecoute du serveur local."),
    port: int = typer.Option(8321, help="Port d'ecoute."),
    ouvrir: bool = typer.Option(
        True, "--ouvrir/--sans-ouvrir", help="Ouvrir le navigateur au lancement."
    ),
) -> None:
    """Lance l'interface web : recherche automatisee de bout en bout + validation."""
    import threading
    import webbrowser

    import uvicorn

    from .web import app as web_app

    url = f"http://{host}:{port}"
    console.print(f"[bold]Interface jobot :[/bold] {url}  [dim](Ctrl-C pour arreter)[/dim]")
    if ouvrir:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(web_app, host=host, port=port, log_level="warning")


@app.command()
def fetch(
    jours: int = typer.Option(7, help="Ne recuperer que les offres publiees depuis N jours."),
    max_par_requete: int = typer.Option(600, help="Plafond de resultats par combinaison."),
    dry_run: bool = typer.Option(False, help="Afficher sans ecrire en base."),
) -> None:
    """Recupere les offres des sources configurees et les stocke en base."""
    sources = settings.sources
    unknown = [name for name in sources if name not in pipeline.SOURCE_NAMES]
    if unknown:
        console.print(
            f"[red]Source inconnue : {', '.join(unknown)}.[/red] "
            f"JOBOT_SOURCES accepte : {', '.join(pipeline.SOURCE_NAMES)}."
        )
        raise typer.Exit(1)

    if "francetravail" in sources:
        try:
            settings.require_ft_credentials()
        except RuntimeError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1)

    params = _env_params(jours=jours, max_par_requete=max_par_requete)
    offers, raws = pipeline.fetch_offers(params, _console_log)
    console.print(f"\n[bold]{len(offers)}[/bold] offres uniques recuperees.")

    if dry_run:
        console.print("[dim]--dry-run : rien n'a ete ecrit.[/dim]")
        return

    conn = db.connect(settings.db_path)
    new, updated, unchanged = db.upsert_offers(conn, offers, raws)
    console.print(
        f"Base : [green]{new} nouvelles[/green], "
        f"[yellow]{updated} mises a jour[/yellow], {unchanged} inchangees."
    )
    pipeline.apply_filters(conn, params.rules(), _console_log)
    conn.close()


@app.command()
def reparse() -> None:
    """Recalcule le canal de candidature des offres en base, sans appel API.

    A lancer apres une amelioration du parsing : le JSON brut etant conserve,
    les offres deja stockees profitent des corrections (URL directe du site
    partenaire, rejet des adresses email invalides).
    """
    conn = db.connect(settings.db_path)
    changed = pipeline.reparse_routing(conn)
    if changed:
        console.print(f"[green]{changed} offre(s) re-routee(s).[/green]")
        for channel, n in sorted(db.counts_by_channel(conn).items(), key=lambda kv: -kv[1]):
            console.print(f"  {channel:<9} {n:>5}")
    else:
        console.print("[dim]Aucun changement : le routage est deja a jour.[/dim]")
    conn.close()


@app.command()
def score(
    limite: int = typer.Option(50, help="Nombre max d'offres a scorer par appel."),
) -> None:
    """Score les offres au statut 'new' avec le LLM et selectionne les bullets du CV."""
    try:
        settings.require_master_cv()
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    cv = load_master_cv(settings.cv_path)
    client = _llm_client()

    conn = db.connect(settings.db_path)
    scored = pipeline.score_pending(
        conn,
        _env_params(limite_score=limite),
        _console_log,
        client=client,
        cv=cv,
    )
    if scored == 0:
        console.print("[dim]Aucune offre au statut 'new' a scorer.[/dim]")
    conn.close()


def _require_scored(row, offer_id: str):
    if row is None:
        console.print(f"[red]Aucune offre avec l'id '{offer_id}'.[/red]")
        raise typer.Exit(1)
    if not row["cv_selection"]:
        console.print(
            f"[red]Offre pas encore scoree (statut : {row['status']}).[/red] "
            "Lance d'abord : jobot score"
        )
        raise typer.Exit(1)


@app.command()
def generate(
    offer_id: str,
    out_dir: Path = typer.Option(None, help="Dossier de sortie pour le HTML/PDF."),
) -> None:
    """Genere le CV adapte (HTML + PDF) pour une offre deja scoree."""
    try:
        settings.require_master_cv()
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    conn = db.connect(settings.db_path)
    row = conn.execute("SELECT * FROM offers WHERE id = ?", (offer_id,)).fetchone()
    conn.close()
    _require_scored(row, offer_id)

    cv = load_master_cv(settings.cv_path)
    try:
        html_path, pdf_path = pipeline.build_cv_files(row, cv, out_dir or settings.out_dir)
    except Exception as exc:  # noqa: BLE001 - message lisible plutot qu'une trace
        console.print(f"[red]Generation du PDF en echec : {exc}[/red]")
        console.print("[dim]Navigateur installe ? uv run playwright install chromium[/dim]")
        raise typer.Exit(1)

    console.print(f"[green]HTML :[/green] {html_path}")
    console.print(f"[green]PDF  :[/green] {pdf_path}")


@app.command()
def review(
    score_min: int = typer.Option(0, help="Ne revoir que les offres au-dessus de ce score."),
    limite: int = typer.Option(20),
) -> None:
    """Passe en revue les offres scorees : garder (queued) ou ecarter (skipped)."""
    conn = db.connect(settings.db_path)
    rows = conn.execute(
        "SELECT * FROM offers WHERE status = ? AND COALESCE(score, 0) >= ? "
        "ORDER BY score DESC LIMIT ?",
        (str(Status.SCORED), score_min, limite),
    ).fetchall()

    if not rows:
        console.print("[dim]Aucune offre a revoir.[/dim]")
        conn.close()
        return

    console.print(f"[bold]{len(rows)} offre(s) a revoir.[/bold] Ctrl-C pour arreter.\n")
    kept = skipped = 0

    for i, row in enumerate(rows, 1):
        color = "green" if (row["score"] or 0) >= 70 else "yellow" if (row["score"] or 0) >= 40 else "red"
        console.print(f"[dim]({i}/{len(rows)})[/dim] [{color}]{row['score']}/100[/{color}]  [bold]{row['title']}[/bold]")
        console.print(f"        {row['company'] or '—'} — {row['location'] or '—'}  [dim]({row['channel']})[/dim]")
        console.print(f"        [dim]{row['score_reason']}[/dim]")
        console.print(f"        [dim]{row['id']}[/dim]")

        try:
            choice = typer.prompt("        [g]arder / [e]carter / [d]etail / [q]uitter", default="g")
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Interrompu.[/dim]")
            break

        choice = choice.strip().lower()[:1]
        if choice == "q":
            break
        if choice == "d":
            console.print(f"\n{row['description']}\n")
            choice = typer.prompt("        [g]arder / [e]carter", default="g").strip().lower()[:1]

        if choice == "e":
            db.set_status(conn, row["id"], Status.SKIPPED)
            skipped += 1
            console.print("        [red]ecartee[/red]\n")
        else:
            db.set_status(conn, row["id"], Status.QUEUED)
            kept += 1
            console.print("        [green]en attente d'envoi[/green]\n")
        conn.commit()

    conn.close()
    console.print(f"[green]{kept} gardee(s)[/green], [red]{skipped} ecartee(s)[/red].")
    if kept:
        console.print("[dim]Suite : jobot send (canal email) / jobot assist <id> (canal form)[/dim]")


@app.command()
def send(
    envoyer: bool = typer.Option(
        False,
        "--envoyer",
        help="Envoyer reellement. Sans ce drapeau : simulation seule.",
    ),
    limite: int = typer.Option(10),
) -> None:
    """Envoie les candidatures 'queued' du canal email (SMTP + CV en piece jointe).

    Simulation par defaut : rien ne part sans --envoyer.
    """
    try:
        settings.require_master_cv()
        if envoyer:
            settings.require_smtp()
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    conn = db.connect(settings.db_path)
    rows = conn.execute(
        "SELECT * FROM offers WHERE status = ? AND channel = ? ORDER BY score DESC LIMIT ?",
        (str(Status.QUEUED), str(Channel.EMAIL), limite),
    ).fetchall()

    if not rows:
        console.print("[dim]Aucune candidature email en attente.[/dim]")
        console.print("[dim]Passe d'abord des offres en 'queued' avec : jobot review[/dim]")
        conn.close()
        return

    cv = load_master_cv(settings.cv_path)

    if not envoyer:
        console.print(
            f"[yellow]SIMULATION[/yellow] — {len(rows)} candidature(s) prete(s). "
            "Rien ne sera envoye.\n"
        )
    else:
        console.print(f"[bold red]{len(rows)} email(s) vont partir reellement.[/bold red]")
        console.print(f"[dim]Expediteur : {settings.sender_address}[/dim]\n")
        for row in rows:
            console.print(f"  → {row['apply_email']}  ({row['title'][:50]})")
        if not typer.confirm("\nConfirmer l'envoi ?", default=False):
            console.print("[dim]Annule.[/dim]")
            conn.close()
            return
        console.print()

    for row in rows:
        offer = pipeline.offer_from_row(row)
        try:
            _, pdf_path = pipeline.build_cv_files(row, cv, settings.out_dir)
            msg = build_message(settings=settings, cv=cv, offer=offer, pdf_path=pdf_path)
        except Exception as exc:  # noqa: BLE001 - on continue sur les autres offres
            console.print(f"[red]{offer.title[:45]:<45}[/red] preparation en echec : {exc}")
            continue

        if not envoyer:
            console.print(f"[yellow]→[/yellow] {offer.apply_email}")
            console.print(f"   Objet : {build_subject(offer)}")
            console.print(f"   Piece jointe : {pdf_path.name}")
            console.print(f"   [dim]{msg.get_body(('plain',)).get_content().strip()[:160]}...[/dim]\n")
            continue

        try:
            send_message(msg, settings)
        except Exception as exc:  # noqa: BLE001 - on continue sur les autres offres
            console.print(f"[red]{offer.apply_email:<35}[/red] envoi en echec : {exc}")
            continue

        db.mark_applied(conn, offer.id)
        conn.commit()
        console.print(f"[green]envoye[/green] → {offer.apply_email}  ({offer.title[:45]})")

    conn.close()
    if not envoyer:
        console.print("[dim]Pour envoyer pour de vrai : jobot send --envoyer[/dim]")


@app.command()
def assist(offer_id: str) -> None:
    """Ouvre le formulaire de candidature dans un navigateur, CV genere a cote.

    L'envoi final reste un clic humain : jobot ne soumet jamais le formulaire.
    """
    try:
        settings.require_master_cv()
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    conn = db.connect(settings.db_path)
    row = conn.execute("SELECT * FROM offers WHERE id = ?", (offer_id,)).fetchone()
    _require_scored(row, offer_id)

    offer = pipeline.offer_from_row(row)
    url = apply_url(offer)
    if not url:
        console.print(f"[red]Aucune URL de candidature pour {offer_id}.[/red]")
        conn.close()
        raise typer.Exit(1)

    cv = load_master_cv(settings.cv_path)
    try:
        _, pdf_path = pipeline.build_cv_files(row, cv, settings.out_dir)
    except Exception as exc:  # noqa: BLE001 - message lisible plutot qu'une trace
        console.print(f"[red]Generation du CV en echec : {exc}[/red]")
        conn.close()
        raise typer.Exit(1)

    console.print(f"[bold]{offer.title}[/bold] — {offer.company or '—'}\n")
    table = Table(show_header=False, box=None, padding=(0, 2))
    for label, value in clipboard_fields(cv, pdf_path):
        table.add_row(f"[dim]{label}[/dim]", value)
    console.print(table)
    console.print(f"\n[dim]Ouverture de {url}[/dim]")
    console.print("[yellow]Remplis et envoie toi-meme, puis ferme le navigateur.[/yellow]\n")

    try:
        open_application_page(url, settings.chrome_profile)
    except Exception as exc:  # noqa: BLE001 - message lisible plutot qu'une trace
        console.print(f"[red]Ouverture du navigateur en echec : {exc}[/red]")
        console.print("[dim]Navigateur installe ? uv run playwright install chromium[/dim]")
        conn.close()
        raise typer.Exit(1)

    if typer.confirm("Candidature envoyee ?", default=False):
        db.mark_applied(conn, offer.id)
        conn.commit()
        console.print("[green]Marquee comme envoyee.[/green]")
    else:
        console.print(f"[dim]Laissee en '{row['status']}'.[/dim]")
    conn.close()


@cv_app.command("init")
def cv_init(
    force: bool = typer.Option(False, help="Ecraser un CV maitre existant."),
) -> None:
    """Cree un CV maitre vierge a partir du modele d'exemple."""
    if settings.cv_path.exists() and not force:
        console.print(
            f"[yellow]{settings.cv_path} existe deja.[/yellow] "
            "Utilise --force pour l'ecraser."
        )
        raise typer.Exit(1)

    settings.cv_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(EXAMPLE_CV_PATH, settings.cv_path)
    console.print(f"[green]Cree :[/green] {settings.cv_path}")
    console.print("Remplace les valeurs d'exemple par les tiennes, puis : jobot cv check")


@cv_app.command("import")
def cv_import(
    fichier: Path = typer.Argument(..., help="CV existant a convertir (HTML ou texte)."),
    force: bool = typer.Option(False, help="Ecraser un CV maitre existant."),
) -> None:
    """Extrait un CV maitre depuis un CV existant, via le LLM."""
    if not fichier.exists():
        console.print(f"[red]Fichier introuvable : {fichier}[/red]")
        raise typer.Exit(1)
    if settings.cv_path.exists() and not force:
        console.print(
            f"[yellow]{settings.cv_path} existe deja.[/yellow] "
            "Utilise --force pour l'ecraser."
        )
        raise typer.Exit(1)

    raw = fichier.read_text(encoding="utf-8", errors="replace")
    source = html_to_text(raw) if fichier.suffix.lower() in {".html", ".htm"} else raw
    console.print(f"[dim]{len(source)} caracteres a analyser...[/dim]")

    client = _llm_client()
    try:
        cv = extract_master_cv(client, source)
    except Exception as exc:  # noqa: BLE001 - message lisible plutot qu'une trace
        console.print(f"[red]Extraction en echec : {exc}[/red]")
        raise typer.Exit(1)

    save_master_cv(cv, settings.cv_path)
    console.print(f"[green]Cree :[/green] {settings.cv_path}")
    _print_cv_summary(cv)
    console.print(
        "\n[yellow]Relis le fichier avant de t'en servir[/yellow] : "
        "le LLM peut avoir mal decoupe ou omis des elements."
    )


@cv_app.command("check")
def cv_check() -> None:
    """Valide le CV maitre et liste les id selectionnables."""
    try:
        settings.require_master_cv()
        cv = load_master_cv(settings.cv_path)
    except Exception as exc:  # noqa: BLE001 - erreurs de validation Pydantic incluses
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    console.print(f"[green]CV maitre valide :[/green] {settings.cv_path}")
    _print_cv_summary(cv)


def _print_cv_summary(cv) -> None:
    console.print(f"\n[bold]{cv.personal.name}[/bold] — {cv.personal.headline}")

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_row("Competences", str(sum(len(c.items) for c in cv.skills)))
    table.add_row("Experiences", f"{len(cv.experiences)} ({sum(len(e.bullets) for e in cv.experiences)} bullets)")
    table.add_row("Projets", f"{len(cv.projects)} ({sum(len(p.bullets) for p in cv.projects)} bullets)")
    table.add_row("Formations", str(len(cv.education)))
    table.add_row("[bold]Id selectionnables[/bold]", f"[bold]{len(cv.selectable_ids())}[/bold]")
    console.print(table)


@app.command("list")
def list_offers(
    statut: str = typer.Option("new", help="new / filtered_out / scored / applied..."),
    limite: int = typer.Option(30),
) -> None:
    """Liste les offres en base."""
    conn = db.connect(settings.db_path)
    order = "score DESC" if statut == str(Status.SCORED) else "published_at DESC"
    rows = conn.execute(
        f"SELECT * FROM offers WHERE status = ? ORDER BY {order} LIMIT ?",
        (statut, limite),
    ).fetchall()

    if not rows:
        console.print(f"[dim]Aucune offre au statut '{statut}'.[/dim]")
        return

    table = Table(show_lines=False)
    table.add_column("Id", width=22, no_wrap=True, style="dim")
    if statut == str(Status.SCORED):
        table.add_column("Score", width=5, justify="right")
    table.add_column("Intitule", max_width=32, overflow="ellipsis")
    table.add_column("Entreprise", max_width=16, overflow="ellipsis")
    table.add_column("Lieu", max_width=14, overflow="ellipsis")
    table.add_column("Canal")
    if statut == str(Status.FILTERED_OUT):
        table.add_column("Motif", max_width=28, overflow="ellipsis")

    colors = {"email": "green", "form": "yellow", "unknown": "red"}
    for row in rows:
        cells = [row["id"]]
        if statut == str(Status.SCORED):
            score = row["score"] or 0
            score_color = "green" if score >= 70 else "yellow" if score >= 40 else "red"
            cells.append(f"[{score_color}]{score}[/{score_color}]")
        cells += [
            row["title"],
            row["company"] or "—",
            row["location"] or "—",
            f"[{colors.get(row['channel'], 'white')}]{row['channel']}[/]",
        ]
        if statut == str(Status.FILTERED_OUT):
            cells.append(row["filter_reason"] or "—")
        table.add_row(*cells)

    console.print(table)
    console.print("[dim]Detail complet : jobot show <id>[/dim]")
    conn.close()


@app.command()
def show(offer_id: str) -> None:
    """Affiche le detail complet d'une offre (description, liens de candidature)."""
    conn = db.connect(settings.db_path)
    row = conn.execute("SELECT * FROM offers WHERE id = ?", (offer_id,)).fetchone()
    conn.close()

    if row is None:
        console.print(f"[red]Aucune offre avec l'id '{offer_id}'.[/red]")
        raise typer.Exit(1)

    console.print(f"[bold]{row['title']}[/bold]  ({row['id']})")
    console.print(f"{row['company'] or '—'} — {row['location'] or '—'}")
    console.print(
        f"Contrat : {row['contract_label'] or row['contract_type'] or '—'}"
        f"   Alternance : {'oui' if row['is_alternance'] else 'non'}"
        f"   Publiee : {row['published_at'] or '—'}"
    )
    if row["salary"]:
        console.print(f"Salaire : {row['salary']}")
    if row["experience"]:
        console.print(f"Experience : {row['experience']}")

    console.print(f"\nStatut : {row['status']}", end="")
    if row["filter_reason"]:
        console.print(f"  (ecartee : {row['filter_reason']})")
    else:
        console.print()

    if row["score"] is not None:
        console.print(f"\n[bold]Score LLM[/bold] : {row['score']}/100 — {row['score_reason']}")
        if row["cv_selection"]:
            ids = json.loads(row["cv_selection"])
            console.print(f"[dim]Ids CV selectionnes ({len(ids)}) : {', '.join(ids)}[/dim]")

    console.print(f"\n[bold]Canal[/bold] : {row['channel']}")
    if row["apply_email"]:
        console.print(f"  Email : {row['apply_email']}")
    if row["apply_url"]:
        console.print(f"  Postuler : {row['apply_url']}")
    if row["origin_url"]:
        console.print(f"  Offre originale : {row['origin_url']}")

    console.print("\n[bold]Description[/bold]")
    console.print(row["description"] or "[dim](vide)[/dim]")


@app.command()
def stats() -> None:
    """Repartition des offres par statut et par canal de candidature."""
    conn = db.connect(settings.db_path)
    by_status = db.counts_by_status(conn)
    by_channel = db.counts_by_channel(conn)

    if not by_status:
        console.print("[dim]Base vide — lance d'abord `jobot fetch`.[/dim]")
        return

    console.print("[bold]Par statut[/bold]")
    for status, n in sorted(by_status.items(), key=lambda kv: -kv[1]):
        console.print(f"  {status:<14} {n:>5}")

    console.print("\n[bold]Par canal[/bold] [dim](hors ecartees)[/dim]")
    labels = {
        "email": "100% automatisable (SMTP)",
        "form": "automatise apres validation (navigateur visible)",
        "unknown": "manuel",
    }
    for channel, n in sorted(by_channel.items(), key=lambda kv: -kv[1]):
        console.print(f"  {channel:<9} {n:>5}   [dim]{labels.get(channel, '')}[/dim]")

    conn.close()



if __name__ == "__main__":
    app()
