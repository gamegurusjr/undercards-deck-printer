# -*- coding: utf-8 -*-
"""Fetch the card DB fallback from the undercards.miraheze.org wiki (CC BY-SA).

The MediaWiki API lives at ``https://undercards.miraheze.org/w/api.php`` (the
bare ``/api.php`` root 404s).  Every card has a page with an
``{{Infobox Monster|...}}``/``{{Infobox Spell|...}}``/``{{Infobox Artifact|...}}``
template; we pull those pages through ``action=query&prop=revisions&rvprop=content``
in small batches (politeness limit) and parse the infobox parameters.

IMPORTANT - this wiki data is a *fallback*: ids were verified to match the
official i18n fixedId keys for sampled cards, but the authoritative source for
card fields is the game's own /AllCards dump (harvest.py), which users can load
from their own signed-in session without bypassing any access control.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

__all__ = ["WIKI_API", "fetch_category_titles", "fetch_page_wikitexts",
           "parse_infobox", "rows_from_wiki", "build_bundle_rows"]

WIKI_API = "https://undercards.miraheze.org/w/api.php"
_UA = "UCDeckPrinter/1.0 (local personal tool; contact: the local user)"
_BATCH_TITLES = 16          # pages per titles= request ( polite )
_MIN_DELAY = 0.45           # seconds between network requests
_CARDS_CAT = "Category:Cards"
_ARTIFACTS_CAT = "Category:Artifacts"

# wiki infobox values -> the site's own codes (rarity gem URL / soul colors)
_EXTENSION_MAP = {"UNDERTALE": "BASE", "DELTARUNE": "DELTARUNE", "DELTARUNE CH2": "DELTARUNE",
                  "UNDERTALE YELLOW": "UTY", "UTY": "UTY", "TWOBER": "TWOBER"}
_RARITY_KEYS = {"BASE", "COMMON", "UNCOMMON", "RARE", "EPIC", "LEGENDARY", "TOKEN",
                "STORY", "SHENANIGANS", "SKIN", "DETERMINATION", "SPECIAL", "BOSS"}
_SOULS = {"MONSTER", "DETERMINATION", "PATIENCE", "BRAVERY", "INTEGRITY",
          "PERSEVERANCE", "KINDNESS", "JUSTICE"}


def get_imageinfos(file_titles: List[str], batch: int = 40) -> Dict[str, str]:
    """'File:X.png' -> direct file URL via the public MediaWiki API."""
    out: Dict[str, str] = {}
    for i in range(0, len(file_titles), batch):
        chunk = file_titles[i:i + batch]
        params = {"action": "query", "titles": "|".join(chunk),
                  "prop": "imageinfo", "iiprop": "url", "format": "json"}
        try:
            data = _get_json(params)
        except RuntimeError:
            break
        pages = (data.get("query", {}) or {}).get("pages", {}) or {}
        for page in pages.values():
            title = page.get("title")
            infos = page.get("imageinfo") or []
            if title and infos and infos[0].get("url"):
                out[title] = infos[0]["url"]
        time.sleep(_MIN_DELAY)
    return out


def _get_raw(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _get_json(params: dict, tries: int = 3) -> dict:
    url = WIKI_API + "?" + urllib.parse.urlencode(params)
    last_error: Optional[Exception] = None
    for attempt in range(tries):
        try:
            return json.loads(_get_raw(url).decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - network resilience
            last_error = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError("Could not reach the wiki API (%s): %s" % (url, last_error))


def fetch_category_titles(category: str, limit: int = 5000) -> List[str]:
    """All member page titles of a category, following continuation."""
    out: List[str] = []
    cmcontinue = None
    while True:
        params = {"action": "query", "list": "categorymembers", "cmtitle": category,
                  "cmlimit": min(limit, 500), "format": "json"}
        if cmcontinue:
            params["cmcontinue"] = cmcontinue
        data = _get_json(params)
        members = (data.get("query", {}) or {}).get("categorymembers", []) or []
        out.extend(m.get("title") for m in members if m.get("title"))
        cinfo = data.get("continue") or {}
        cmcontinue = cinfo.get("cmcontinue")
        if not cmcontinue:
            break
        time.sleep(_MIN_DELAY)
    return out


def fetch_page_wikitexts(titles: List[str], progress: Optional[Callable] = None) -> Dict[str, str]:
    """title -> raw wikitext, batched."""
    out: Dict[str, str] = {}
    batches = [titles[i:i + _BATCH_TITLES] for i in range(0, len(titles), _BATCH_TITLES)]
    for idx, batch in enumerate(batches):
        params = {"action": "query", "prop": "revisions", "rvprop": "content",
                  "rvslots": "main", "titles": "|".join(batch), "format": "json"}
        data = _get_json(params)
        for page in (data.get("query", {}) or {}).get("pages", {}).values():
            title = page.get("title")
            revs = page.get("revisions") or []
            text = ""
            if revs:
                slots = revs[0].get("slots") or {}
                text = (slots.get("main") or {}).get("*") or ""
            if title:
                out[title] = text
        if progress is not None:
            progress(len(out), len(titles))
        time.sleep(_MIN_DELAY)
    return out


# ---------------------------------------------------------------------------
# infobox parsing
# ---------------------------------------------------------------------------

_TOP_RE = re.compile(r"^\s*\{\{\s*(Infobox Monster|Infobox Spell|Infobox Artifact)\s*\|", re.I | re.M)


def _find_template_block(wikitext: str, start: int) -> Optional[int]:
    """Index just past the matching '}}' for the template that opens at start-2."""
    depth = 1
    i = start
    n = len(wikitext)
    while i < n:
        if wikitext.startswith("{{", i):
            depth += 1
            i += 2
        elif wikitext.startswith("}}", i):
            depth -= 1
            if depth == 0:
                return i + 2
            i += 2
        else:
            i += 1
    return None


def _split_params(body: str) -> List[str]:
    """Split '|'-separated template params, respecting nested {{}}/[[]]."""
    parts: List[str] = []
    depth_t = 0   # {{ }}
    depth_l = 0   # [[ ]]
    cur: List[str] = []
    i = 0
    n = len(body)
    while i < n:
        if body.startswith("{{", i):
            depth_t += 1
            cur.append("{{")
            i += 2
        elif body.startswith("}}", i):
            depth_t -= 1
            cur.append("}}")
            i += 2
        elif body.startswith("[[", i):
            depth_l += 1
            cur.append("[[")
            i += 2
        elif body.startswith("]]", i):
            depth_l -= 1
            cur.append("]]")
            i += 2
        elif body[i] == "|" and depth_t == 0 and depth_l == 0:
            parts.append("".join(cur))
            cur = []
            i += 1
        else:
            cur.append(body[i])
            i += 1
    parts.append("".join(cur))
    return parts


_CLEAN_RE = re.compile(r"<[^>]+>")


def parse_infobox(wikitext: str) -> Optional[Tuple[str, Dict[str, str]]]:
    """(template kind, params) of the first Infobox_* template, or None."""
    match = _TOP_RE.search(wikitext)
    if not match:
        return None
    kind = match.group(1).split(None, 1)[1].lower()   # 'monster' | 'spell' | 'artifact'
    params_match_start = match.end()                  # points at the first '|'
    params_start = wikitext.find("|", match.start())
    close = _find_template_block(wikitext, params_match_start)
    if not close:
        return None
    body = wikitext[params_start + 1: close - 2]
    params: Dict[str, str] = {}
    for raw in _split_params(body):
        if "=" not in raw:
            key = str(len(params))
            params[key] = raw.strip()
            continue
        key, _, value = raw.partition("=")
        key = key.strip().lower()
        value = _CLEAN_RE.sub(" ", value)
        value = value.replace("\\n", " ").strip()
        params[key] = value
    return kind, params


_NUM_F = re.compile(r"^-?\d+$")


def _num(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    value = value.strip().replace(" ", "")
    if not value or not _NUM_F.match(value):
        return None
    return int(value)


def rows_from_wiki(progress: Optional[Callable] = None
                   ) -> Tuple[Dict[int, dict], Dict[int, dict], dict]:
    """Download everything and return (cards, artifacts, meta)."""
    card_titles = fetch_category_titles(_CARDS_CAT)
    artifact_titles = fetch_category_titles(_ARTIFACTS_CAT)
    if progress:
        progress("titles", {"cards": len(card_titles), "artifacts": len(artifact_titles)})

    def fp(lenout, lentot):
        if progress:
            progress("pages", {"done": lenout, "total": lentot})

    card_texts = fetch_page_wikitexts(card_titles, fp)
    art_texts = fetch_page_wikitexts(artifact_titles, fp)

    cards: Dict[int, dict] = {}
    artifacts: Dict[int, dict] = {}
    skipped: List[dict] = []

    def _norm_soul(soul_row: str, kind: str) -> Optional[str]:
        soul = (soul_row or "").strip().upper()
        if not soul:
            return "MONSTER"      # monsters without an explicit soul
        return soul if soul in _SOULS else None

    def _norm_ext(ext_row: str) -> str:
        return _EXTENSION_MAP.get((ext_row or "").strip().upper(), "BASE")

    def _norm_rarity(rarity_row: str) -> Optional[str]:
        r = (rarity_row or "").strip().upper()
        return r if r in _RARITY_KEYS else None

    for title, text in card_texts.items():
        parsed = parse_infobox(text)
        if not parsed:
            skipped.append({"title": title, "reason": "no infobox"})
            continue
        kind, params = parsed
        cid = _num(params.get("id"))
        if not cid:
            skipped.append({"title": title, "reason": "no numeric id"})
            continue
        type_card = 1 if kind == "spell" else 0
        image_file = (params.get("imagefile") or params.get("image") or "").strip()
        tribes = []
        for tkey in ("tribe1", "tribe2", "tribe3"):
            tv = (params.get(tkey) or "").strip()
            if tv:
                tribes.append(tv)
        row = {
            "id": cid,
            "fixedId": _num(params.get("fixedid")) or cid,
            "name": (params.get("name") or title).strip(),
            "image": _strip_art_prefix(image_file),
            "rarity": _norm_rarity(params.get("rarity")),
            "extension": _norm_ext(params.get("extension")),
            "cost": _num(params.get("cost")),
            "atk": _num(params.get("atk")),
            "hp": _num(params.get("hp")),
            "soul": _norm_soul(params.get("soul"), kind),
            "typeCard": type_card,
            "typeSkin": _num(params.get("typeskin")) or 0,
            "frame": (params.get("frameskinname") or params.get("frame") or "Undertale").strip()
                     .replace(" ", "_"),
            "tribes": tribes,
            "wiki_name": title,
            "wiki_image_file": image_file or None,
        }
        cards[cid] = row

    for title, text in art_texts.items():
        parsed = parse_infobox(text)
        if not parsed:
            skipped.append({"title": title, "reason": "no infobox"})
            continue
        kind, params = parsed
        aid = _num(params.get("id"))
        if not aid:
            skipped.append({"title": title, "reason": "no numeric id"})
            continue
        image_file = (params.get("imagefile") or params.get("image") or "").strip()
        artifacts[aid] = {
            "id": aid,
            "name": (params.get("name") or title).strip(),
            "image": _strip_art_prefix(image_file),
            "wiki_name": title,
            "wiki_image_file": image_file or None,
        }

    meta = {
        "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "https://undercards.miraheze.org (CC BY-SA)",
        "source_pages": {"cards_checked": len(card_titles), "artifacts_checked": len(artifact_titles)},
        "skipped": skipped,
        "wiki_only": True,
    }
    return cards, artifacts, meta


def _strip_art_prefix(image_file: str) -> Optional[str]:
    base = image_file.strip()
    if not base:
        return None
    base = base.split("/")[-1]
    if base.lower().endswith(".png"):
        base = base[:-4]
    for prefix in ("Card_", "Artifact_"):
        if base.startswith(prefix):
            return base[len(prefix):]
    return base


def build_bundle_rows(progress: Optional[Callable] = None) -> dict:
    cards, artifacts, meta = rows_from_wiki(progress)
    return {"_meta": meta, "cards": {str(k): v for k, v in cards.items()},
            "artifacts": {str(k): v for k, v in artifacts.items()}}
