# -*- coding: utf-8 -*-
"""Undercards Deck Printer - local web app (Flask).

Pipeline (background job, UI polls for progress):
    parse deck code -> resolve every id against the card DB -> fetch art
    (cached, rate-limited) -> compose official-geometry card faces ->
    optional artifact sheet -> fixed-size PDF + page previews.

Guarantees enforced here:
  * duplicates and print order are preserved exactly as in the deck code;
  * nothing is silently substituted: missing art exists in the response and
    the PDF is refused until the user explicitly proceeds anyway (or stops);
  * the PDF is only produced by app.pdfgen, which holds the hard
    2.5 x 3.5 in invariant (extra pages instead of resizing).
"""

from __future__ import annotations

import base64
import hmac
import io
import json
import os
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from flask import Flask, abort, jsonify, render_template, request, send_file

from . import assets as amod
from . import cardface
from . import deck_parser
from . import harvest
from . import pdfgen
from .card_db import CardDB
from .i18n import CLASS_COLORS

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
CACHE = os.path.join(DATA, "cache")
JOBS_DIR = os.path.join(DATA, "jobs")
FONTS_DIR = os.path.join(CACHE, "fonts")
FONT_REGULAR = cardface.FONT_REG_NAME
FONT_SANS = cardface.FONT_SANS_NAME

FACE_SCALE = 5            # 176*5 x 246*5 = 880 x 1230 px  (~300+ DPI at print size)
PREVIEW_FACE_WIDTH = 330  # grid thumbnails
PREVIEW_DPI = 110         # page previews


app = Flask(
    __name__,
    template_folder=os.path.join(HERE, "templates"),
    static_folder=os.path.join(HERE, "static"),
)


# ---------------------------------------------------------------------------
# Personal access gate for public/cloud hosting (opt-in via environment).
#
# Set UC_ACCESS_TOKEN on the host to lock the deployment down: every request
# must present that token either as the username OR the password of a Basic
# auth header, or as ``Authorization: Bearer <token>``.  When the variable is
# unset (the default, e.g. localhost) everything behaves exactly like before
# and no gate exists.  The value is re-read on every request so editing the
# host's environment takes effect without a redeploy, and /api/health stays
# open in both modes so the host's liveness probe keeps working.
# ---------------------------------------------------------------------------

_OPEN_PATHS = {"/api/health"}


@app.before_request
def _access_gate():
    expected = os.environ.get("UC_ACCESS_TOKEN", "")
    if not expected or request.path in _OPEN_PATHS:
        return None
    auth = request.headers.get("Authorization", "")
    supplied = ""
    if auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth[6:].strip()).decode("utf-8", "replace")
        except Exception:
            decoded = ""
        if ":" in decoded:
            user, pw = decoded.split(":", 1)
            supplied = user if user else pw  # either half may carry the token
    elif auth.startswith("Bearer "):
        supplied = auth[7:].strip()
    if supplied:
        match = hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
        if match:
            return None
    resp = jsonify({
        "error": "unauthorized",
        "message": ("This deployment is private. Send your access token as the "
                    "Basic auth username (or password), or as a Bearer token."),
    })
    resp.status_code = 401
    resp.headers["WWW-Authenticate"] = (
        'Basic realm="Undercards Deck Printer", charset="UTF-8"')
    return resp

_job_store: Dict[str, "Job"] = {}
_job_lock = threading.Lock()
_db_lock = threading.Lock()
_db: Optional[CardDB] = None
_db_error: Optional[str] = None
_asset_store: Optional[amod.AssetStore] = None


def asset_store() -> amod.AssetStore:
    global _asset_store
    if _asset_store is None:
        _asset_store = amod.AssetStore(CACHE)
    return _asset_store


def i18n_image_resolver(src: str, w: int, h: int):
    """I18n {{IMAGE:}} -> local cached file path (or None)."""
    try:
        if src.startswith("/images/inserted/"):
            name = src.rsplit("/", 1)[-1]
            if name.lower().endswith(".png"):
                name = name[:-4]
            entry = asset_store().inserted(name)
            if entry.ok:
                return entry.path
    except Exception:  # noqa: BLE001 - an inline icon must never break parsing
        return None
    return None


def get_db() -> CardDB:
    """Lazy, thread-safe one-shot CardDB load."""
    global _db, _db_error
    with _db_lock:
        if _db is not None:
            return _db
        try:
            _db = CardDB.load(DATA, image_resolver=i18n_image_resolver)
            _db_error = None
        except FileNotFoundError as exc:
            _db_error = str(exc)
            raise
        if _db is not None:
            cardface.set_placeholder_font_path(os.path.join(FONTS_DIR, FONT_REGULAR))
        return _db


def db_unavailable() -> Optional[str]:
    try:
        get_db()
        return None
    except FileNotFoundError:
        return _db_error or "Card database is missing - click 'Refresh card database'."
    except Exception as exc:  # noqa: BLE001
        return str(exc)


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------

DEFAULT_SETTINGS = {
    "paper": "letter",
    "orientation": "landscape",
    "gap_mm": 4.0,
    "cut_guides": True,
    "include_artifacts": False,
    "proceed_missing": False,
}


@dataclass
class Entry:
    card_id: int
    fixed_id: Optional[int] = None
    qty: int = 1
    name: Optional[str] = None
    desc_plain: str = ""
    type_card: int = 0
    cost: Optional[int] = None
    atk: Optional[int] = None
    hp: Optional[int] = None
    rarity: Optional[str] = None
    extension: Optional[str] = None
    soul: Optional[str] = None
    face_k: Optional[int] = None
    art_status: str = "pending"
    art_message: Optional[str] = None
    art_ok: bool = False


@dataclass
class Job:
    id: str
    kind: str = "deck"
    code: Optional[str] = None
    label: str = ""
    created: float = field(default_factory=time.time)
    status: str = "running"          # running | done | blocked | error
    stage: str = "queued"
    stage_detail: str = ""
    art_done: int = 0
    art_total: int = 0
    error: Optional[str] = None
    hint: Optional[str] = None
    log: List[str] = field(default_factory=list)
    settings: dict = field(default_factory=lambda: dict(DEFAULT_SETTINGS))
    deck: Optional[deck_parser.Deck] = None
    entries: List[Entry] = field(default_factory=list)
    artifacts: List[dict] = field(default_factory=list)
    missing: List[dict] = field(default_factory=list)
    unknown_cards: List[int] = field(default_factory=list)
    unknown_artifacts: List[int] = field(default_factory=list)
    face_files: List[str] = field(default_factory=list)     # unique faces, k-indexed
    print_faces: List[Optional[str]] = field(default_factory=list)  # deck order
    artifact_faces: List[str] = field(default_factory=list)
    pdf_decks: Optional[str] = None
    pdf_artifacts: Optional[str] = None
    counts: dict = field(default_factory=dict)
    dir: str = ""
    rev: int = 1                     # bumped whenever settings change; frontend cache-buster

    # ------------------------------------------------------------------
    def say(self, line: str) -> None:
        with _job_lock:
            self.log.append(line)
            if len(self.log) > 200:
                del self.log[: len(self.log) - 200]

    def dir_create(self) -> None:
        self.dir = os.path.join(JOBS_DIR, self.id)
        os.makedirs(self.dir, exist_ok=True)

    def snapshot(self) -> dict:
        with _job_lock:
            entries = []
            for e in self.entries:
                entries.append({
                    "id": e.card_id, "fixed_id": e.fixed_id, "qty": e.qty,
                    "name": e.name, "desc": e.desc_plain, "typeCard": e.type_card,
                    "cost": e.cost, "atk": e.atk, "hp": e.hp,
                    "rarity": e.rarity, "extension": e.extension, "soul": e.soul,
                    "face_k": e.face_k, "art_ok": e.art_ok,
                    "art_status": e.art_status, "art_message": e.art_message,
                })
            counts = dict(self.counts)
            return {
                "id": self.id,
                "kind": self.kind,
                "status": self.status,
                "stage": self.stage,
                "stage_detail": self.stage_detail,
                "art_done": self.art_done,
                "art_total": self.art_total,
                "error": self.error,
                "hint": self.hint,
                "log": self.log[-60:],
                "settings": dict(self.settings),
                "deck": {
                    "soul": self.deck.soul if self.deck else None,
                    "total_cards": self.deck.total_cards if self.deck else 0,
                    "unique_cards": self.deck.unique_cards if self.deck else 0,
                    "duplicates": self.deck.duplicates if self.deck else 0,
                    "artifact_ids": self.deck.artifacts if self.deck else [],
                },
                "entries": entries,
                "artifacts": self.artifacts,
                "missing": self.missing,
                "unknown_cards": self.unknown_cards,
                "unknown_artifacts": self.unknown_artifacts,
                "counts": counts,
                "label": self.label,
                "rev": self.rev,
            }


def spawn_job(job: Job) -> None:
    with _job_lock:
        _job_store[job.id] = job
    job.dir_create()
    thread = threading.Thread(target=_run_job, args=(job,), daemon=True,
                              name="job-%s" % job.id)
    thread.start()


# ---------------------------------------------------------------------------
# the pipeline
# ---------------------------------------------------------------------------

def _set_stage(job: Job, stage: str, detail: str = "") -> None:
    with _job_lock:
        job.stage = stage
        job.stage_detail = detail


def _sanitize_settings(raw: dict) -> dict:
    s = dict(DEFAULT_SETTINGS)
    s.update({
        "paper": str(raw.get("paper", s["paper"])).lower(),
        "orientation": str(raw.get("orientation", s["orientation"])).lower(),
        "cut_guides": bool(raw.get("cut_guides", s["cut_guides"])),
        "include_artifacts": bool(raw.get("include_artifacts", s["include_artifacts"])),
        "proceed_missing": bool(raw.get("proceed_missing", s["proceed_missing"])),
    })
    if s["paper"] not in ("letter", "a4"):
        s["paper"] = "letter"
    if s["orientation"] not in ("landscape", "portrait"):
        s["orientation"] = "landscape"
    try:
        gap = float(raw.get("gap_mm", s["gap_mm"]))
    except (TypeError, ValueError):
        gap = s["gap_mm"]
    s["gap_mm"] = min(10.0, max(0.0, gap))
    return s


def _resolve_db(job: Job) -> Optional[str]:
    """Fill job.entries/artifacts/unknowns. Returns an error string or None."""
    db = get_db()
    art_ids = job.deck.artifacts

    job.entries = []
    seen: Dict[int, Entry] = {}
    for cid, qty in job.deck.quantities():
        if cid in seen:
            seen[cid].qty += qty          # keep first-appearance order & running qty
            continue
        card = db.card(cid)
        if card is None:
            job.unknown_cards.append(cid)
            continue
        try:
            desc = db.i18n.plain(card.official_desc_key())
        except Exception:  # noqa: BLE001 - description text is cosmetic
            desc = ""
        e = Entry(
            card_id=cid, fixed_id=card.fixedId or card.id, qty=qty,
            name=card.official_name(db.i18n), desc_plain=(desc or "").strip(),
            type_card=int(card.typeCard or 0), cost=card.cost,
            atk=card.atk, hp=card.hp, rarity=card.rarity,
            extension=card.extension, soul=card.soul,
        )
        seen[cid] = e
        job.entries.append(e)
    if job.unknown_cards:
        return (
            "Card id(s) not found in the card database: %s. These ids are newer "
            "than the bundled source data or the deck code is corrupted. The app "
            "never invents cards." % ", ".join(str(c) for c in job.unknown_cards))
    if not job.entries:
        return "The deck contains no printable cards."

    job.artifacts = []
    for aid in art_ids:
        a = db.artifact(aid)
        known = a is not None
        name = a.official_name(db.i18n) if a else None
        for slot in job.artifacts:
            if slot["id"] == aid:
                slot["qty"] += 1
                break
        else:
            job.artifacts.append({
                "id": aid, "name": name, "qty": 1, "known": known,
                "face_k": None, "art_ok": False,
            })
    job.unknown_artifacts = [a["id"] for a in job.artifacts if not a["known"]]
    return None


def _fetch_fonts(job: Job) -> bool:
    store = asset_store()
    reg = store.font(FONT_REGULAR)
    ok = reg.ok
    if not ok:
        job.hint = ("The official card font (fonts/DTM-Mono.otf) could not be fetched "
                    "from undercards.net and is not cached yet. Retrying later usually "
                    "fixes it; text is drawn with Pillow once any font is available.")
    db = get_db()
    if any(e.card_id == 59 or e.fixed_id == 59 for e in job.entries):
        store.font(FONT_SANS)   # only fetched when a sans card is actually printed
    try:
        cardface.set_placeholder_font_path(os.path.join(FONTS_DIR, FONT_REGULAR))
    except Exception:  # noqa: BLE001
        pass
    return ok


def _fetch_card_assets(job: Job) -> None:
    db = get_db()
    store = asset_store()
    store.wiki_file_urls([c.wiki_image_file for c in
                          [db.card(e.card_id) for e in job.entries]
                          if c and c.wiki_image_file])
    total = len(job.entries)
    with _job_lock:
        job.art_total = total
    for e in job.entries:
        card = db.card(e.card_id)
        if card is None:
            continue
        try:
            art = store.card_art(card.image, card.wiki_image_file)
            e.art_status = art.status
            e.art_message = art.message
            e.art_ok = bool(art.ok)
            entry_found = art.ok
            art_path = art.path if entry_found else None
        except Exception as exc:  # noqa: BLE001 - single-card failures must not stop the deck
            e.art_status = "error"
            e.art_message = str(exc)
            e.art_ok = False
            entry_found, art_path = False, None
        if not e.art_ok:
            with _job_lock:
                job.missing.append({
                    "kind": "card", "id": e.card_id, "name": e.name,
                    "title": getattr(card, "wiki_name", None),
                    "art_status": e.art_status,
                    "message": e.art_message or "art unavailable from both sources",
                })
        face_path = os.path.join(job.dir, "face_%d.png" % e.card_id)
        try:
            face = cardface.compose_card(
                card, art_path, store, db.i18n,
                frame_path=_frame_path(job, card),
                rarity_path=_rarity_path(job, card),
                scale=FACE_SCALE, fonts_dir=FONTS_DIR)
            face.save(face_path)
            e.face_k = len(job.face_files)
            job.face_files.append(face_path)
        except Exception as exc:  # noqa: BLE001 - compose failure also must not stop
            traceback.print_exc()
            e.art_status = "compose-error"
            e.art_message = str(exc)
            e.art_ok = False
            with _job_lock:
                job.missing.append({
                    "kind": "card", "id": e.card_id, "name": e.name,
                    "title": getattr(card, "wiki_name", None),
                    "art_status": "compose-error",
                    "message": "could not compose the card face: %s" % exc,
                })
        with _job_lock:
            job.art_done += 1
    with _job_lock:
        job.art_total = total


def _frame_path(job: Job, card) -> Optional[str]:
    frame = asset_store().frame(card.frame, int(card.typeCard or 0))
    return frame.path if frame.ok else None


def _rarity_path(job: Job, card) -> Optional[str]:
    if int(card.typeCard or 0) == 1:
        return None                      # official spells have no ATK/HP/gem
    r = asset_store().rarity(card.extension, card.rarity)
    return r.path if r.ok else None


def _compose_artifacts(job: Job) -> None:
    db = get_db()
    store = asset_store()
    store.wiki_file_urls([db.artifact(a["id"]).wiki_image_file
                          for a in job.artifacts if db.artifact(a["id"])])
    for a in job.artifacts:
        art_obj = db.artifact(a["id"])
        if art_obj is None:
            continue
        art = store.card_art(art_obj.image, art_obj.wiki_image_file)
        a["art_ok"] = bool(art.ok)
        face_path = os.path.join(job.dir, "artifact_%d.png" % a["id"])
        try:
            face = cardface.compose_artifact(
                art_obj, art.path if art.ok else None, store, db.i18n,
                scale=FACE_SCALE, fonts_dir=FONTS_DIR)
            face.save(face_path)
        except Exception as exc:  # noqa: BLE001 - one broken artifact must not kill the sheet
            traceback.print_exc()
            a["art_ok"] = False
            job.say("artifact %s could not be composed: %s" % (a["id"], exc))
            continue
        a["face_k"] = len(job.face_files)
        job.face_files.append(face_path)
        job.artifact_faces.append(face_path)


def _build_pdfs(job: Job) -> dict:
    """Build both PDFs (deck + optional artifact sheet)."""
    s = job.settings
    header = "Undercards Deck Printer - %s cards" % (job.deck.total_cards if job.deck else 0)
    out = {}

    if job.print_faces:
        data, pages = pdfgen.build_pdf(
            job.print_faces, paper=s["paper"], orientation=s["orientation"],
            gap_mm=s["gap_mm"], cut_guides=s["cut_guides"], header_text=header)
        path = os.path.join(job.dir, "deck.pdf")
        with open(path, "wb") as fh:
            fh.write(data)
        job.pdf_decks = path
        out["deck_pages"] = pages
    else:
        out["deck_pages"] = 0

    if s["include_artifacts"] and job.artifact_faces:
        data, pages = pdfgen.build_pdf(
            list(job.artifact_faces), paper=s["paper"], orientation=s["orientation"],
            gap_mm=s["gap_mm"], cut_guides=s["cut_guides"],
            header_text="Undercards Deck Printer - artifact sheet (approximation)")
        path = os.path.join(job.dir, "artifacts.pdf")
        with open(path, "wb") as fh:
            fh.write(data)
        job.pdf_artifacts = path
        out["artifact_pages"] = pages
    else:
        out["artifact_pages"] = 0
    return out


def _update_counts(job: Job) -> None:
    s = job.settings
    pw, ph = pdfgen.page_size_pt(s["paper"], s["orientation"])
    cols, rows = pdfgen.grid_counts(s["orientation"])
    with _job_lock:
        job.counts = {
            "cards": len(job.print_faces),
            "deck_pages": pdfgen.pages_needed(len(job.print_faces), s["orientation"]),
            "per_page": cols * rows,
            "cols": cols,
            "rows": rows,
            "artifact_pages": (pdfgen.pages_needed(len(job.artifact_faces), s["orientation"])
                               if s["include_artifacts"] and job.artifact_faces else 0),
            "page_w_pt": round(pw, 1),
            "page_h_pt": round(ph, 1),
            "card_w_pt": pdfgen.CARD_W,
            "card_h_pt": pdfgen.CARD_H,
            "card_mm": [round(pdfgen.CARD_W / pdfgen.MM_TO_PT, 1),
                        round(pdfgen.CARD_H / pdfgen.MM_TO_PT, 1)],
        }


def _refresh_print_faces(job: Job) -> None:
    """Print-order face list: f(a) duplicates repeat the same face file."""
    faces: List[Optional[str]] = []
    by_card = {}
    for e in job.entries:
        if e.face_k is not None and e.face_k < len(job.face_files):
            by_card[e.card_id] = job.face_files[e.face_k]
    for e in job.entries:
        path = by_card.get(e.card_id)
        faces.extend([path] * e.qty)
    job.print_faces = faces


def _run_job(job: Job) -> None:
    try:
        try:
            get_db()
        except FileNotFoundError:
            _fail(job,
                  "The card database is not built yet. Click 'Refresh card database' "
                  "in the header (it fetches the wiki metadata once, politely) or run "
                  "'python tools\\build_db.py'.",
                  hint="db_missing")
            return

        if job.kind == "dbrefresh":
            _run_dbrefresh(job)
            return

        _set_stage(job, "parsing", "Decoding the deck code...")
        try:
            job.deck = deck_parser.parse_deck(job.code or "")
        except deck_parser.DeckParseError as exc:
            _fail(job, str(exc),
                  hint="deck_format",
                  mk_hint="The code is base64(JSON) with keys soul/cardIds/artifactIds "
                          "- copy the whole blob from undercards.net "
                          "(Decks -> export -> share link).")
            return
        job.say("deck: %d cards, %d unique, %d artifacts, soul %s"
                % (job.deck.total_cards, job.deck.unique_cards,
                   len(job.deck.artifacts), job.deck.soul))

        _set_stage(job, "resolving", "Looking up every card id...")
        err = _resolve_db(job)
        if err:
            _fail(job, err, hint="unknown_ids")
            return

        _set_stage(job, "fonts", "Fetching the official fonts (cached after first run)...")
        _fetch_fonts(job)

        _set_stage(job, "assets", "Fetching card art (rate-limited, cached)...")
        _fetch_card_assets(job)
        missing_ok = not job.missing

        if job.settings["include_artifacts"]:
            _set_stage(job, "artifacts", "Composing the optional artifact sheet...")
            _compose_artifacts(job)

        _refresh_print_faces(job)
        _update_counts(job)

        _set_stage(job, "pdf", "Laying out the print sheets at fixed 2.5x3.5 in...")
        pages = _build_pdfs(job)
        with _job_lock:
            job.counts.update({"deck_pages": pages["deck_pages"],
                               "artifact_pages": pages["artifact_pages"]})

        if not missing_ok and not job.settings["proceed_missing"]:
            with _job_lock:
                job.status = "blocked"
                job.stage = "blocked"
                job.stage_detail = (
                    "Art is missing for %d card(s); percentages/wording of the "
                    "placeholders are intentional. The PDF stays blocked until you "
                    "click 'Print anyway'." % len(job.missing))
            job.say("blocked: missing art for %d card(s)" % len(job.missing))
            return

        with _job_lock:
            job.status = "done"
            job.stage = "done"
            job.stage_detail = ("Print sheets ready: %d page(s) of %d cards, "
                                "fixed %.1f x %.1f mm."
                                % (pages["deck_pages"], job.counts["per_page"],
                                   job.counts["card_mm"][0], job.counts["card_mm"][1]))
        job.say("done: %s" % job.stage_detail)
    except Exception as exc:  # noqa: BLE001 - top level safety net
        traceback.print_exc()
        _fail(job, "%s: %s" % (type(exc).__name__, exc),
              hint="internal", mk_hint="See the server console for the full backtrace.")


def _fail(job: Job, message: str, hint: str = "error", mk_hint: Optional[str] = None) -> None:
    with _job_lock:
        job.status = "error"
        job.stage = "error"
        job.error = message
        job.hint = hint
        job.stage_detail = ""
        job.counts = {}
        if mk_hint:
            job.log.append(mk_hint)
    job.say("ERROR: %s" % message)


def _run_dbrefresh(job: Job) -> None:
    """Re-read the wiki fallback and swap the in-memory DB when it succeeds."""
    from . import wiki_fallback as wf

    def progress(step, info) -> None:
        if step == "titles":
            job.say("wiki titles: cards=%(cards)s artifacts=%(artifacts)s" % info)
            _set_stage(job, "dbtitles", "Wiki pages found: %(cards)s cards, %(artifacts)s artifacts" % info)
        elif step == "pages":
            with _job_lock:
                job.art_total = info["total"]
                job.art_done = info["done"]
            _set_stage(job, "dbpages", "Downloading wiki pages %d/%d..." % (info["done"], info["total"]))

    try:
        rows = wf.build_bundle_rows(progress)
    except Exception as exc:  # noqa: BLE001
        _fail(job, "Could not refresh the card database from the wiki: %s" % exc,
              hint="network")
        return
    meta = dict(rows["_meta"])
    meta["en_source_keys"] = _count_en_keys()
    rows["_meta"] = meta
    path = os.path.join(DATA, "db.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)

    global _db
    with _db_lock:
        _db = None
    try:
        get_db()
    except FileNotFoundError:
        _fail(job, "Database written but the reload failed (translation file missing?).",
              hint="db_missing")
        return
    with _job_lock:
        job.status = "done"
        job.stage = "done"
        job.stage_detail = "Card database refreshed: %d cards, %d artifacts (wiki, CC BY-SA)." % (
            len(rows["cards"]), len(rows["artifacts"]))
        job.counts = {"cards": len(rows["cards"]), "artifacts": len(rows["artifacts"]),
                      "built": meta.get("built", "")}
    job.say(job.stage_detail)


def _count_en_keys() -> int:
    try:
        with open(os.path.join(DATA, "en.json"), "r", encoding="utf-8") as fh:
            return len(json.load(fh))
    except (OSError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    dbm = None
    try:
        get_db()
    except FileNotFoundError:
        dbm = _db_error
    except Exception as exc:  # noqa: BLE001
        dbm = str(exc)
    return jsonify({"ok": True, "db": "ready" if dbm is None else "unavailable",
                    "db_message": dbm})


@app.route("/api/dbinfo")
def dbinfo():
    try:
        db = get_db()
    except FileNotFoundError as exc:
        return jsonify({"available": False, "message": str(exc),
                        "hint": "Click 'Refresh card database'."})
    meta = db.meta or {}
    return jsonify({
        "available": True,
        "built": meta.get("built"),
        "source": meta.get("source"),
        "wiki_only": bool(meta.get("wiki_only", True)),
        "authoritative": bool(db.has_authoritative),
        "authoritative_at": meta.get("authoritative_at"),
        "authoritative_ids": meta.get("authoritative_ids"),
        "en_source_keys": meta.get("en_source_keys") or _count_en_keys(),
        "cards": len(db.cards),
        "artifacts": len(db.artifacts),
        "cards_named_in_en": meta.get("cards_named_in_en"),
        "artifacts_named_in_en": meta.get("artifacts_named_in_en"),
        "skipped_pages": len(meta.get("skipped", []) or []),
    })


@app.route("/api/db/refresh", methods=["POST"])
def db_refresh():
    job = Job(id=uuid.uuid4().hex[:12], kind="dbrefresh")
    spawn_job(job)
    return jsonify({"id": job.id})


@app.route("/api/parse", methods=["POST"])
def parse_start():
    data = request.get_json(silent=True) or {}
    code = str(data.get("code") or "")
    label = str(data.get("label") or "")[:60]
    if not code.strip():
        return jsonify({"error": "empty_code",
                        "message": "Paste a deck code first (it is the base64 blob "
                                   "from the deck share link)."}), 400
    job = Job(id=uuid.uuid4().hex[:12], code=code, label=label)
    job.settings = _sanitize_settings(data.get("settings") or {})
    spawn_job(job)
    return jsonify({"id": job.id})


@app.route("/api/job/<job_id>")
def job_state(job_id):
    job = _job_store.get(job_id)
    if job is None:
        return jsonify({"error": "not_found", "message": "Unknown job."}), 404
    return jsonify(job.snapshot())


@app.route("/api/settings/<job_id>", methods=["POST"])
def settings_update(job_id):
    job = _job_store.get(job_id)
    if job is None:
        return jsonify({"error": "not_found", "message": "Unknown job."}), 404
    data = request.get_json(silent=True) or {}
    with _job_lock:
        job.settings.update(_sanitize_settings(data))
        job.rev += 1                      # frontend re-render busts Chrome's img memory cache
        busy = job.status == "running"
        was = job.status
    if busy or job.deck is None or not job.face_files or job.kind != "deck":
        # the running pipeline (or a later stage) will apply them anyway
        return jsonify({"id": job.id, "queued": True})
    with _job_lock:
        if was != "running":
            job.stage = "rebuilding"
            job.stage_detail = "Applying print settings (no re-fetch, no re-compose)..."
    if job.settings["include_artifacts"] and not job.artifact_faces and job.artifacts:
        _compose_artifacts(job)                      # artifact sheet turned on later
    _refresh_print_faces(job)
    _update_counts(job)
    _build_pdfs(job)
    blocked = bool(job.missing) and not job.settings["proceed_missing"]
    with _job_lock:
        if was != "running":
            job.status = "blocked" if blocked else "done"
            job.stage = job.status
            if not blocked:
                job.stage_detail = "Updated: %d page(s) at fixed 2.5x3.5 in." % job.counts["deck_pages"]
    return jsonify({"id": job.id})


@app.route("/api/preview/<job_id>")
def preview_page(job_id):
    job = _job_store.get(job_id)
    if job is None:
        return jsonify({"error": "not_found", "message": "Unknown job."}), 404
    which = request.args.get("which", "deck")
    try:
        page = max(0, int(request.args.get("page", 0)))
    except ValueError:
        page = 0
    s = job.settings
    faces = job.artifact_faces if (which == "artifacts" and s["include_artifacts"]) else job.print_faces
    if not faces:
        return jsonify({"error": "not_ready", "message": "No faces to preview yet."}), 409
    try:
        out = pdfgen.page_preview_png(
            faces, s["paper"], s["orientation"], s["gap_mm"], s["cut_guides"], page,
            dpi=PREVIEW_DPI)
    except pdfgen.PagingError as exc:
        return jsonify({"error": "layout", "message": str(exc)}), 400
    if out is None:
        return jsonify({"error": "range", "message": "Page out of range."}), 404
    resp = send_file(out, mimetype="image/png", download_name="preview_p%d.png" % (page + 1))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/face/<job_id>")
def face_png(job_id):
    job = _job_store.get(job_id)
    if job is None:
        return jsonify({"error": "not_found", "message": "Unknown job."}), 404
    try:
        k = int(request.args.get("k", -1))
    except ValueError:
        k = -1
    if not (0 <= k < len(job.face_files)):
        return jsonify({"error": "range", "message": "No such face."}), 404
    path = job.face_files[k]
    if not os.path.isfile(path):
        return jsonify({"error": "missing", "message": "Face file disappeared."}), 404
    from PIL import Image
    try:
        width = int(request.args.get("width", PREVIEW_FACE_WIDTH))
        width = min(1230, max(60, width))
        img = Image.open(path)
        if img.width > width:
            img = img.resize((width, round(img.height * width / img.width)))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        resp = send_file(buf, mimetype="image/png")
        resp.headers["Cache-Control"] = "no-store"
        return resp
    except (OSError, ValueError) as exc:
        return jsonify({"error": "image", "message": str(exc)}), 500


@app.route("/api/download/<job_id>")
def download_pdf(job_id):
    job = _job_store.get(job_id)
    if job is None:
        return jsonify({"error": "not_found", "message": "Unknown job."}), 404
    which = request.args.get("which", "deck")
    if job.status == "blocked" and which == "deck":
        return jsonify({
            "error": "missing_art",
            "message": "Art is missing for some cards, so the PDF is intentionally "
                       "blocked. Review the list, then click 'Print anyway' if you "
                       "want the output with composed MISSING-ART placeholders.",
            "missing": job.missing,
        }), 409
    if job.status == "error":
        return jsonify({"error": "job_error", "message": job.error}), 500
    path = job.pdf_artifacts if which == "artifacts" else job.pdf_decks
    label = "artifacts" if which == "artifacts" else "deck"
    if not path or not os.path.isfile(path):
        return jsonify({"error": "not_ready", "message": "PDF not generated yet."}), 409
    return send_file(path, mimetype="application/pdf", as_attachment=True,
                     download_name="undercards-%s-%s.pdf" % (label, job.id))


@app.route("/api/harvest/start", methods=["POST"])
def harvest_start():
    if not harvest.sync_available():
        return jsonify({"error": "playwright_missing",
                        "message": harvest.PLAYWRIGHT_HINT}), 400

    def _on_merged(entries):
        db = get_db()
        summary = db.merge_allcards_payload(entries)
        db.save(DATA)
        return summary

    started = harvest.start_harvest(DATA, on_merged=_on_merged)
    if not started:
        return jsonify({"error": "already_running",
                        "message": "A harvest is already in progress."}), 409
    return jsonify({"started": True})


@app.route("/api/harvest/status")
def harvest_status():
    return jsonify(harvest.get_state())


@app.route("/api/harvest/cancel", methods=["POST"])
def harvest_cancel():
    harvest.cancel()
    return jsonify({"cancel": True})


# Only the site's own UI-font files are served here, from the local cache; they
# are allow-listed by exact name and come straight from the repo's cache dir.
FONT_MIME = {
    "DTM-Mono.otf": "font/otf",
    "sans.ttf": "font/ttf",
}


@app.route("/api/font/<path:name>")
def font_file(name):
    mime = FONT_MIME.get(name)
    if mime is None:
        return jsonify({"error": "not_found", "message": "No such font."}), 404
    path = os.path.join(CACHE, "fonts", name)
    if not os.path.isfile(path):
        entry = asset_store().font(name)          # polite single fetch, then cached
        if not entry.ok:
            return jsonify({"error": "not_found", "message": "Font not cached yet."}), 404
        path = entry.path
    resp = send_file(path, mimetype=mime)
    resp.headers["Cache-Control"] = "public, max-age=604800"
    return resp


@app.errorhandler(404)
def not_found(_):
    if request.path.startswith("/api/"):
        return jsonify({"error": "not_found", "message": "No such endpoint."}), 404
    return abort(404)


def create_handler():  # pragma: no cover - WSGI entry point for local use
    return app


if __name__ == "__main__":
    # Cloud hosts (Render etc.) pass their own PORT; localhost wins otherwise.
    port = int(os.environ.get("PORT", os.environ.get("UC_PORT", "8952")))
    host = os.environ.get("UC_BIND", "127.0.0.1")
    print("Undercards Deck Printer -> http://127.0.0.1:%d  (actual bind: %s)"
          % (port, host))
    print("(Ctrl+C here stops the local server; nothing is ever uploaded.)")
    os.makedirs(JOBS_DIR, exist_ok=True)
    app.run(host=host, port=port, debug=False, threaded=True)
