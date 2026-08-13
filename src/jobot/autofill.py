"""Remplissage et soumission automatiques des formulaires de candidature.

Ne se declenche qu'apres la validation explicite de la candidature par
l'utilisateur (clic par offre dans l'UI). Le navigateur reste visible du debut
a la fin : l'utilisateur voit ce qui est rempli, peut corriger, et confirme
lui-meme le statut final — la detection du succes d'une soumission n'est
jamais fiable a 100 %.

Le remplissage est heuristique (best effort) : chaque site de recrutement a
son propre formulaire. Ce module remplit ce qu'il reconnait, joint le CV,
et ne tente la soumission que si rien ne la bloque (connexion requise,
captcha, aucun champ reconnu). Le texte de motivation vient du template
email — jamais du LLM (invariant du projet).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from .cv import MasterCV

# Libelles/attributs reconnus -> valeur a inscrire. L'ordre compte : le
# premier motif qui matche gagne ('prenom' avant 'nom', sinon "Prénom"
# matcherait aussi le motif du nom).
_FIELD_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("prenom", re.compile(r"pr[eé]nom|first.?name", re.I)),
    ("nom", re.compile(r"\bnoms?\b|last.?name|family.?name|surname", re.I)),
    ("email", re.compile(r"e-?mail|courriel", re.I)),
    ("telephone", re.compile(r"t[eé]l[eé]?phone|\btel\b|phone|mobile|portable", re.I)),
    ("message", re.compile(r"motivation|message|commentaire|cover.?letter|presentation", re.I)),
]

_SUBMIT_RE = re.compile(
    r"envoyer\s+(ma\s+|la\s+)?candidature|postuler|candidater|je\s+postule"
    r"|soumettre|envoyer|submit|apply",
    re.I,
)
# Envoi sans ambiguite possible. Seul motif accepte quand aucun champ n'a ete
# rempli : le motif large ci-dessus recliquerait le « Postuler » qui vient
# justement de nous amener sur l'ecran d'envoi.
_SUBMIT_STRICT_RE = re.compile(
    r"envoyer\s+(ma|la|votre|cette)\s+candidature"
    r"|soumettre\s+(ma|la|votre|cette)\s+candidature"
    r"|send\s+(my\s+)?application|submit\s+(my\s+)?application",
    re.I,
)
# Bouton qui devoile le formulaire quand la page d'offre n'en affiche pas.
_REVEAL_RE = re.compile(r"postuler|candidater|je\s+postule|apply", re.I)

_CAPTCHA_RE = re.compile(r"captcha|datadome|turnstile|challenge", re.I)

# Depot du CV derriere un bouton, sans `input[type=file]` atteignable (France
# Travail : « Télécharger un CV » ouvre un selecteur de fichier natif).
# Volontairement ancre sur le mot CV/fichier : « Télécharger l'offre en PDF »
# ne doit pas matcher, « Créer un CV » non plus (c'est l'editeur de CV du site).
_UPLOAD_RE = re.compile(
    r"t[eé]l[eé]charger\s+(un|mon|le|votre)?\s*cv"
    r"|(joindre|importer|ajouter|d[eé]poser|choisir)\s+(un|mon|le|votre|ma)?\s*"
    r"(cv|fichier|document)"
    r"|upload\s+(a|my|your)?\s*(cv|resume|file)",
    re.I,
)

# Cases a cocher qui conditionnent l'envoi. On ne coche que la confirmation
# explicite : une case « je souhaite recevoir... » n'a rien a faire ici.
_CONFIRM_RE = re.compile(
    r"je\s+confirme|j'atteste|je\s+certifie|je\s+reconnais|j'accepte"
    r"|conditions\s+g[eé]n[eé]rales|politique\s+de\s+confidentialit[eé]"
    r"|i\s+(confirm|agree|certify|accept)",
    re.I,
)

# Confirmation d'envoi affichee par le site. Seul signal positif fiable dont
# on dispose ; « Votre candidature sera envoyée au recruteur » (present sur le
# formulaire AVANT envoi) ne doit surtout pas matcher.
_SENT_RE = re.compile(
    r"candidature\s+(a\s+)?(bien\s+)?[eé]t[eé]\s+(envoy[eé]e|transmise|enregistr[eé]e)"
    r"|candidature\s+(envoy[eé]e|transmise|enregistr[eé]e)"
    r"|merci\s+pour\s+votre\s+candidature"
    r"|application\s+(has\s+been\s+)?(sent|submitted|received)",
    re.I,
)

# Une candidature s'etale sur plusieurs ecrans (France Travail : recapitulatif
# -> « Postuler en ligne » -> confirmation). Plafond volontaire : au-dela on
# rend la main plutot que de cliquer indefiniment.
_MAX_STEPS = 5

# Signes d'un mur de connexion. La detection ne sert qu'apres un echec de
# remplissage : beaucoup de sites affichent un lien "Connexion" en en-tete
# alors que leur formulaire de candidature est parfaitement public.
_LOGIN_URL_RE = re.compile(r"/(connexion|login|authentification|sign-?in|sso|oauth)", re.I)
_LOGIN_TEXT_RE = re.compile(
    r"se\s+connecter|connexion|s'identifier|identifiez-vous|cr[eé]er\s+un\s+compte", re.I
)

# Bandeau de consentement aux cookies. Il recouvre la page et intercepte les
# clics : sans le fermer d'abord, meme un bouton parfaitement visible devient
# incliquable (constate sur candidat.francetravail.fr). L'ordre traduit une
# preference : refuser quand le site le propose, accepter seulement sinon.
_BANNER_PATTERNS = [
    re.compile(r"continuer\s+sans\s+accepter", re.I),
    re.compile(r"tout\s+refuser|refuser\s+tout|refuser\s+&|reject\s+all", re.I),
    re.compile(r"tout\s+accepter|accepter\s+tout|accept\s+all|allow\s+all", re.I),
    re.compile(r"j'accepte|i\s+accept", re.I),
]


@dataclass
class AutofillReport:
    """Ce que l'automatisation a reussi a faire, montre tel quel dans l'UI."""

    filled: list[str] = dataclass_field(default_factory=list)
    uploaded: bool = False
    submitted: bool = False
    notes: list[str] = dataclass_field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "filled": self.filled,
            "uploaded": self.uploaded,
            "submitted": self.submitted,
            "notes": self.notes,
        }


def _values(cv: MasterCV, message: str) -> dict[str, str]:
    parts = cv.personal.name.split()
    return {
        "prenom": parts[0] if parts else cv.personal.name,
        "nom": " ".join(parts[1:]) or cv.personal.name,
        "email": cv.personal.email,
        "telephone": cv.personal.phone,
        "message": message,
    }


def _descriptor(el) -> str:
    """Texte agrege qui decrit un champ : attributs + label associe."""
    attrs = [
        el.get_attribute("name"),
        el.get_attribute("id"),
        el.get_attribute("placeholder"),
        el.get_attribute("aria-label"),
        el.get_attribute("autocomplete"),
    ]
    try:
        label = el.evaluate(
            "e => e.labels && e.labels.length ? e.labels[0].innerText : ''"
        )
    except Exception:  # noqa: BLE001 - un label illisible ne bloque pas le reste
        label = ""
    return " ".join(filter(None, attrs + [label]))


def _checkbox_text(el) -> str:
    """Texte qui accompagne une case a cocher.

    Propre aux cases : on remonte jusqu'au parent faute de `<label>` associe,
    ce qu'on ne peut pas se permettre pour un champ texte (le texte alentour
    ferait matcher n'importe quel motif de `_FIELD_PATTERNS`).
    """
    try:
        return (
            el.evaluate(
                "e => (e.labels && e.labels.length ? e.labels[0].innerText : '')"
                " || (e.closest('label') ? e.closest('label').innerText : '')"
                " || (e.parentElement ? e.parentElement.innerText : '')"
            )
            or ""
        )
    except Exception:  # noqa: BLE001
        return ""


def _fill_frame(frame, values: dict[str, str], pdf_path: Path, report: AutofillReport) -> bool:
    """Remplit les champs reconnus d'un frame ; joint le CV aux input file.

    Retourne True si quelque chose a bouge — y compris une simple case cochee,
    qui ne laisse pas de trace dans le rapport mais prouve qu'on est bien sur
    un formulaire de candidature.
    """
    touched = False
    inputs = frame.locator(
        "input[type=text], input[type=email], input[type=tel], input:not([type]), textarea"
    )
    for i in range(inputs.count()):
        el = inputs.nth(i)
        try:
            if not el.is_visible() or el.input_value():
                continue  # champ cache ou deja rempli : on ne touche pas
            desc = _descriptor(el)
            input_type = (el.get_attribute("type") or "").lower()
            if input_type == "email":
                desc = f"email {desc}"
            elif input_type == "tel":
                desc = f"telephone {desc}"
            for key, pattern in _FIELD_PATTERNS:
                if pattern.search(desc):
                    el.fill(values[key])
                    report.filled.append(key)
                    touched = True
                    break
        except Exception:  # noqa: BLE001 - un champ recalcitrant n'arrete pas le reste
            continue

    if not report.uploaded:
        uploads = frame.locator("input[type=file]")
        for i in range(uploads.count()):
            el = uploads.nth(i)
            try:
                # Pas de `is_visible()` sur l'input : il est presque toujours
                # masque derriere un bouton stylise, et `set_input_files`
                # fonctionne quand meme. Son conteneur, lui, doit etre
                # affiche — sinon on joint le CV au formulaire d'un ecran
                # encore cache, et jobot croit tenir le bon formulaire alors
                # qu'il n'a meme pas clique « Postuler ».
                host_shown = el.evaluate(
                    "e => { const p = e.parentElement;"
                    " return !p || !p.checkVisibility || p.checkVisibility(); }"
                )
                if not host_shown:
                    continue
                el.set_input_files(str(pdf_path))
                report.uploaded = True
                touched = True
                break
            except Exception:  # noqa: BLE001
                continue

    # Cases qui conditionnent l'envoi : obligatoires (consentement RGPD) ou
    # confirmation explicite ("Je confirme que mes coordonnées sont valides",
    # non marquee `required` sur France Travail — sans elle, « Envoyer » est
    # refuse et le parcours s'arrete sans rien dire).
    boxes = frame.locator("input[type=checkbox]")
    for i in range(min(boxes.count(), 60)):
        box = boxes.nth(i)
        try:
            if not box.is_visible() or box.is_checked():
                continue
            if box.get_attribute("required") is not None or _CONFIRM_RE.search(
                _checkbox_text(box)
            ):
                box.check()
                touched = True
        except Exception:  # noqa: BLE001
            continue

    return touched


def _detect_blockers(page) -> list[str]:
    """Raisons de ne PAS soumettre automatiquement.

    La connexion n'en fait pas partie : elle est traitee en amont par une
    pause qui rend la main a l'utilisateur (voir `auto_apply`).
    """
    blockers = []
    try:
        for fr in page.frames:
            if _CAPTCHA_RE.search(fr.url or ""):
                blockers.append("Captcha détecté : termine à la main.")
                break
    except Exception:  # noqa: BLE001
        pass
    return blockers


def _looks_like_login(page) -> bool:
    """La page demande-t-elle de s'authentifier ?"""
    try:
        if page.locator("input[type=password]").count():
            return True
        if _LOGIN_URL_RE.search(page.url or ""):
            return True
        # Un seul aller-retour plutot qu'un par element : ces pages en ont
        # souvent des centaines.
        texts = page.evaluate(
            "() => Array.from(document.querySelectorAll('button, a'))"
            ".filter(e => e.offsetParent !== null).map(e => e.innerText || '').join(' | ')"
        )
        return bool(_LOGIN_TEXT_RE.search(texts or ""))
    except Exception:  # noqa: BLE001
        return False


def _iter_matches(page, pattern: re.Pattern, *, include_links: bool = False):
    """Elements cliquables visibles dont le texte matche, dans l'ordre du DOM.

    `include_links` etend la recherche aux `<a href>` ordinaires : sur
    France Travail (entre autres) le "Postuler" qui ouvre le formulaire est
    un simple lien, pas un bouton. Reserve au devoilement du formulaire et aux
    motifs sans ambiguite — pour une soumission ordinaire on s'en tient aux
    vrais boutons, moins ambigus.

    Les textes sont releves en un seul aller-retour par frame. Interroger
    Playwright element par element imposait un plafond (150 elements) pour
    rester rapide, sous lequel le contenu utile de la page passait a la
    trappe : les menus deroulants du bandeau France Travail comptent a eux
    seuls des centaines de liens, tous places avant le formulaire dans le DOM.
    Le bouton « Télécharger un CV » n'etait alors jamais vu — donc ni clique,
    ni compte comme etape manquante, d'ou une candidature annoncee envoyee
    alors qu'elle etait restee sur l'ecran du CV.
    """
    selector = "button, input[type=submit], input[type=button], a[role=button]"
    if include_links:
        selector += ", a[href]"

    for frame in page.frames:
        try:
            texts = frame.evaluate(
                """(selector) => Array.from(document.querySelectorAll(selector)).map((e) => {
                    const shown = e.checkVisibility
                        ? e.checkVisibility({checkVisibilityCSS: true})
                        : e.offsetParent !== null;
                    if (!shown || !e.getClientRects().length) return "";
                    return e.tagName === "INPUT" ? (e.value || "") : (e.innerText || "");
                })""",
                selector,
            )
        except Exception:  # noqa: BLE001 - frame inaccessible (cross-origin, detache)
            continue
        # `nth(i)` suit le meme ordre que `querySelectorAll` : les index
        # releves ci-dessus designent bien les memes elements.
        elements = frame.locator(selector)
        for i, text in enumerate(texts):
            if text and pattern.search(text):
                yield elements.nth(i)


def _click_first_match(
    page, pattern: re.Pattern, *, include_links: bool = False, timeout: float = 6000
) -> bool:
    """Clique le premier element visible dont le texte matche.

    Le `timeout` court est essentiel : un element recouvert par un bandeau
    fait echouer le clic, et avec le delai par defaut de Playwright chaque
    tentative couterait 30 s. On passe alors au match suivant.
    """
    for el in _iter_matches(page, pattern, include_links=include_links):
        try:
            el.click(timeout=timeout)
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _attach_cv(page, pdf_path: Path, report: AutofillReport) -> bool:
    """Joint le CV quand aucun `input[type=file]` n'est exploitable.

    France Travail n'en expose pas : « Télécharger un CV » ouvre un selecteur
    de fichier natif, qu'aucun `set_input_files` ne peut atteindre. Playwright
    intercepte ce selecteur via l'evenement `filechooser` — c'est le seul
    moyen de deposer le CV, et sans lui l'ecran « Postuler en ligne » reste
    incomplet, donc infranchissable.
    """
    if report.uploaded:
        return False
    for el in _iter_matches(page, _UPLOAD_RE, include_links=True):
        try:
            with page.expect_file_chooser(timeout=8000) as chooser:
                el.click(timeout=6000)
            chooser.value.set_files(str(pdf_path))
            # Le site verifie le fichier et le convertit : laisser le temps.
            page.wait_for_timeout(4000)
            report.uploaded = True
            return True
        except Exception:  # noqa: BLE001 - bouton qui ouvre autre chose qu'un selecteur
            continue
    return False


def _fill_current(page, values: dict[str, str], pdf_path: Path, report: AutofillReport) -> bool:
    """Traite l'ecran courant : champs, cases, depot du CV. Sans rien devoiler."""
    touched = False
    for frame in page.frames:
        touched |= _fill_frame(frame, values, pdf_path, report)
    touched |= _attach_cv(page, pdf_path, report)
    return touched


def _blocked_on_upload(page, report: AutofillReport) -> bool:
    """Le parcours reclame un CV que jobot n'a pas su deposer.

    Seule preuve fiable d'un envoi inabouti : « il reste un bouton d'envoi »
    n'en est pas une (un formulaire soumis en AJAX garde le sien), alors qu'un
    depot de CV attendu et non satisfait bloque a coup sur.
    """
    if report.uploaded:
        return False
    return next(_iter_matches(page, _UPLOAD_RE, include_links=True), None) is not None


def _page_state(page) -> str:
    """Empreinte de l'ecran courant, pour savoir si un clic a fait avancer.

    Sans ca, un bouton d'envoi qui ne navigue pas serait recliqué a chaque
    tour de boucle — donc la candidature envoyee plusieurs fois.
    """
    try:
        return page.url + "|" + page.evaluate(
            "() => String(document.body ? document.body.innerText.length : 0)"
        )
    except Exception:  # noqa: BLE001
        return ""


def _looks_sent(page) -> bool:
    """Le site affiche-t-il une confirmation d'envoi ?"""
    try:
        text = page.evaluate("() => document.body ? document.body.innerText : ''")
    except Exception:  # noqa: BLE001
        return False
    return bool(_SENT_RE.search(text or ""))


def _dismiss_banner(page) -> bool:
    """Ferme le bandeau de consentement, s'il y en a un."""
    for pattern in _BANNER_PATTERNS:
        if _click_first_match(page, pattern, include_links=True, timeout=2500):
            page.wait_for_timeout(1200)
            return True
    return False


def _fill_pass(
    page, values: dict[str, str], pdf_path: Path, report: AutofillReport
) -> tuple[bool, bool]:
    """Une passe de remplissage : la page telle quelle, puis derriere « Postuler ».

    Retourne `(rempli, devoile)`. Les deux comptent : sur un site ou la
    candidature part du compte utilisateur (France Travail connecte), il n'y a
    aucun champ a remplir, et seul `devoile` distingue « ecran d'envoi
    atteint » de « page qu'on n'a pas su lire ».
    """
    _dismiss_banner(page)
    _fill_current(page, values, pdf_path, report)
    if report.filled or report.uploaded:
        return True, False

    # La page d'offre n'affiche parfois le formulaire qu'apres un clic.
    revealed = _click_first_match(page, _REVEAL_RE, include_links=True)
    if revealed:
        page.wait_for_timeout(3000)
        _fill_current(page, values, pdf_path, report)
    return bool(report.filled or report.uploaded), revealed


def _run_steps(
    page,
    values: dict[str, str],
    pdf_path: Path,
    report: AutofillReport,
    *,
    strict: bool,
) -> None:
    """Enchaine les ecrans jusqu'a l'envoi final.

    Un « Envoyer ma candidature » ne conclut pas toujours : France Travail
    ouvre ensuite « Postuler en ligne » (depot du CV, lettre pre-remplie, case
    de confirmation, bouton sobrement intitule « Envoyer »).

    `submitted` n'est mis que si le site confirme l'envoi, ou si plus rien
    n'attend d'action. Un faux `submitted` classerait l'offre comme envoyee
    alors qu'elle ne l'est pas — l'erreur la plus couteuse ici, puisque
    l'utilisateur ne repasserait jamais dessus.
    """
    clicks = 0
    for _ in range(_MAX_STEPS):
        before = _page_state(page)
        if not _click_first_match(page, _SUBMIT_STRICT_RE if strict else _SUBMIT_RE,
                                  include_links=strict):
            break
        clicks += 1
        page.wait_for_timeout(4000)
        if _page_state(page) == before:
            break  # le clic n'a rien ouvert : formulaire d'un seul ecran
        if _fill_current(page, values, pdf_path, report):
            # On vient de remplir cet ecran : on est donc bien dans le
            # formulaire, et le motif large redevient sur — c'est la que le
            # bouton s'appelle sobrement « Envoyer ».
            strict = False

    if clicks == 0:
        report.notes.append("Bouton d'envoi introuvable : clique toi-même pour soumettre.")
    elif _looks_sent(page) or not _blocked_on_upload(page, report):
        report.submitted = True
    else:
        report.notes.append(
            "Le CV n'a pas pu être déposé : le site attend encore une action, "
            "termine l'envoi dans le navigateur."
        )


def auto_apply(
    url: str,
    *,
    cv: MasterCV,
    pdf_path: Path,
    message: str,
    profile_dir: Path,
    submit: bool = True,
    headless: bool = False,
    wait_close: bool = True,
    on_report: Callable[[AutofillReport], None] | None = None,
    on_login_required: Callable[[str], None] | None = None,
    wait_for_resume: Callable[[], bool] | None = None,
) -> AutofillReport:
    """Ouvre l'offre, remplit le formulaire, joint le CV, et tente l'envoi.

    N'appeler qu'apres validation explicite de la candidature par
    l'utilisateur. Le navigateur (profil persistant, sessions conservees)
    reste ouvert jusqu'a ce que l'utilisateur le ferme, pour verification.

    Si le site exige une connexion, l'automatisation ne s'arrete pas : elle
    signale l'attente via `on_login_required`, laisse l'utilisateur
    s'authentifier dans la fenetre deja ouverte, puis reprend la ou elle en
    etait quand `wait_for_resume()` retourne True. Le profil etant persistant,
    les offres suivantes du meme site ne repassent plus par cette etape.
    `headless`/`wait_close` ne servent qu'aux tests.
    """
    from playwright.sync_api import sync_playwright

    report = AutofillReport()
    values = _values(cv, message)
    profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=headless,
            viewport=None if not headless else {"width": 1280, "height": 900},
            args=[] if headless else ["--start-maximized"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)

            filled, revealed = _fill_pass(page, values, pdf_path, report)

            # Rien de reconnu + page d'authentification : on rend la main
            # plutot que d'abandonner la candidature.
            login_wall = False
            if not filled and _looks_like_login(page):
                # Le « Postuler » n'a mene qu'au mur de connexion : rien n'est
                # devoile tant que l'utilisateur ne s'est pas authentifie.
                revealed = False
                login_wall = True
                if wait_for_resume is None:
                    report.notes.append("Le site demande une connexion : termine à la main.")
                else:
                    domain = urlparse(page.url).netloc or urlparse(url).netloc
                    report.notes.append(
                        f"Connexion requise sur {domain}." if domain
                        else "Connexion requise sur ce site."
                    )
                    if on_login_required:
                        on_login_required(domain)
                    if wait_for_resume():
                        report.notes.append("Connexion effectuée : reprise de la candidature.")
                        page.goto(url, wait_until="domcontentloaded")
                        page.wait_for_timeout(2500)
                        filled, revealed = _fill_pass(page, values, pdf_path, report)
                        login_wall = False
                    else:
                        report.notes.append(
                            "Connexion non effectuée : candidature laissée en attente."
                        )

            blockers = _detect_blockers(page)
            report.notes.extend(blockers)

            if not filled and not revealed:
                if not login_wall:  # sinon la note de connexion dit deja tout
                    report.notes.append(
                        "Aucun champ reconnu : formulaire non standard, remplis à la main."
                    )
            elif submit and not blockers:
                # Ecran atteint sans avoir rien rempli : on n'accepte alors que
                # le bouton explicite, sinon le motif large recliquerait le
                # « Postuler » qui vient justement de nous amener ici.
                _run_steps(page, values, pdf_path, report, strict=not filled)
        except Exception as exc:  # noqa: BLE001 - le rapport porte l'erreur, l'humain reprend
            report.notes.append(f"Automatisation interrompue : {exc}")

        if on_report:
            on_report(report)
        if wait_close:
            # L'utilisateur verifie le resultat et ferme lui-meme la fenetre.
            context.wait_for_event("close", timeout=0)
        else:
            context.close()

    return report
