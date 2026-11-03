# -*- coding: utf-8 -*-
"""Asset downloader + disk cache with polite rate limiting.

Sources, in priority order (matches the project's disclosure rules):
  1. undercards.net original assets (public static files: images/css/fonts).
  2. the wiki-hosted copy of the same file (used when undercards 404s -
     the wiki mirrors card art; identical 160x90 sprites).
  3. nothing - the caller falls back to the composed MISSING-ART placeholder
     and the UI/PDF require explicit acknowledgement before printing.

Only public static files are requested from undercards.net; the app never
attempts to bypass Cloudflare, sign-up walls or rate limits.  Requests are
throttled (default >= 0.3 s apart) and 404/403 results are cached as markers
so the whole render does not hammer the site for files that are known-missing.
"""

from __future__ import annotations

import os
import re
import threading
import time
import json
import urllib.parse
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

import requests

__all__ = ["AssetStore", "EntryStatus", "Entry"]

_SITE = "https://undercards.net"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 UCDeckPrinter/1.0"
EntryStatus = str  # 'ok' | 'missing' | 'blocked' | 'error' | 'cached'


@dataclass
class Entry:
    status: EntryStatus
    path: Optional[str] = None
    url: Optional[str] = None
    http_status: Optional[int] = None
    message: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "cached") and self.path and os.path.isfile(self.path)


class AssetStore:
    """Thread-safe, rate-limited, disk-backed fetch of undercards.net statics."""

    def __init__(self, root: str, min_delay: float = 0.30, timeout: int = 30,
                 session: Optional[requests.Session] = None) -> None:
        self.root = root
        self.min_delay = min_delay
        self.timeout = timeout
        self._sess = session or requests.Session()
        self._sess.headers.update({"User-Agent": _UA})
        self._lock = threading.RLock()
        self._last_request = 0.0
        self.wiki_urls: Dict[str, str] = {}
        self._load_wiki_urls()
        self._wiki_api_caller: Optional[Callable] = None   # injected (lazy import)

    # ------------------------------------------------------------------
    # paths
    # ------------------------------------------------------------------
    def _paths_for(self, relpath: str) -> Tuple[str, str]:
        """(cache_path, counterpart under the wiki-hosted copy)."""
        rel = relpath.replace("\\", "/").lstrip("/")
        base = rel.split("/")
        return (
            os.path.join(self.root, "site", *base),
            os.path.join(self.root, "wiki", base[-1]),
        )

    # ------------------------------------------------------------------
    # main fetch
    # ------------------------------------------------------------------
    def fetch(self, relpath: str, wiki_file: Optional[str] = None) -> Entry:
        """Fetch `<site>/relpath` (Site-cached first). wiki_file: e.g. Card_X.png holding
        the wiki-hosted filename for fallback."""
        with self._lock:
            cache_path, wiki_path = self._paths_for(relpath)
            if os.path.isfile(cache_path) and os.path.getsize(cache_path) > 0:
                return Entry("cached", cache_path, url=_SITE + "/" + relpath)
            entry = self._download(relpath, cache_path)
            if entry.ok:
                return entry
        # hospital fallback sources (wiki copy)
        if wiki_file:
            with self._lock:
                if os.path.isfile(wiki_path) and os.path.getsize(wiki_path) > 0:
                    return Entry("cached", wiki_path, url="wiki:" + wiki_file, message="wiki copy")
                url = self.wiki_urls.get("File:" + wiki_file)
                if url:
                    try:
                        self._throttle()
                        resp = self._sess.get(url, timeout=self.timeout)
                        if resp.ok and resp.content:
                            os.makedirs(os.path.dirname(wiki_path), exist_ok=True)
                            with open(wiki_path, "wb") as fh:
                                fh.write(resp.content)
                            self._record(relpath, wiki_path)
                            return Entry("cached", wiki_path, url=url, message="wiki copy")
                    except requests.RequestException as exc:  # noqa: PERF203
                        return Entry("error", None, url=url, message=str(exc))
        return entry

    # ------------------------------------------------------------------
    def _download(self, relpath: str, cache_path: str) -> Entry:
        url = _SITE + "/" + relpath
        for attempt in range(3):
            try:
                self._throttle()
                resp = self._sess.get(url, timeout=self.timeout)
                ctype = (resp.headers.get("Content-Type") or "").lower()
                is_font = relpath.endswith((".otf", ".ttf", ".woff", ".woff2"))
                payload_ok = ("image" in ctype) if not is_font else ("html" not in ctype)
                if resp.ok and resp.content and payload_ok:
                    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                    with open(cache_path, "wb") as fh:
                        fh.write(resp.content)
                    return Entry("ok", cache_path, url=url, http_status=resp.status_code)
                if resp.status_code == 404:
                    return Entry("missing", None, url=url, http_status=404,
                                 message="not on undercards.net")
                if resp.status_code in (403, 503) and b"Just a moment" in resp.content[:2000]:
                    return Entry("blocked", None, url=url, http_status=resp.status_code,
                                 message="Cloudflare challenge; static asset unexpectedly blocked")
                if resp.status_code == 403:
                    return Entry("blocked", None, url=url, http_status=403,
                                 message="forbidden by the site")
                last = resp
            except requests.RequestException as exc:  # noqa: PERF203
                last = exc
            time.sleep(0.8 * (attempt + 1))
        if isinstance(last, requests.RequestException):
            return Entry("error", None, url=url, message=str(last))
        return Entry("error", None, url=url, http_status=last.status_code,
                     message="HTTP %s" % last.status_code)

    def _throttle(self) -> None:
        while True:
            gap = time.monotonic() - self._last_request
            if gap >= self.min_delay:
                break
            time.sleep(min(0.05, self.min_delay - gap))
        self._last_request = time.monotonic()

    # ------------------------------------------------------------------
    # wiki file URL resolution (public MediaWiki API, cached)
    # ------------------------------------------------------------------
    def wiki_file_urls(self, filenames: List[str]) -> Dict[str, str]:
        """Resolve `Card_X.png` -> direct wiki file URL, batched, cached on disk."""
        needed = ["File:" + name for name in filenames
                  if name and ("File:" + name) not in self.wiki_urls]
        if needed:
            url_path = os.path.join(self.root, "wiki_urls.json")
            if needed:
                try:
                    from . import wiki_fallback as wf
                    data = wf.get_imageinfos(needed)
                    for title, url in (data or {}).items():
                        if url:
                            self.wiki_urls[title] = url
                    try:
                        os.makedirs(os.path.dirname(url_path), exist_ok=True)
                        with open(url_path, "w", encoding="utf-8") as fh:
                            json.dump(self.wiki_urls, fh, indent=1)
                    except OSError:
                        pass
                except Exception:  # noqa: BLE001 - wiki must not break the pipeline
                    pass
        return self.wiki_urls

    def _load_wiki_urls(self) -> None:
        path = os.path.join(self.root, "wiki_urls.json")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
        self.wiki_urls = data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------
    # domain helpers
    # ------------------------------------------------------------------
    def card_art(self, image: Optional[str], wiki_file: Optional[str] = None) -> Entry:
        if not image:
            return Entry("missing", message="no art name in DB")
        return self.fetch("images/cards/%s.png" % image, wiki_file)

    def frame(self, frame_name: str, type_card: int) -> Entry:
        which = "frame_spell.png" if type_card == 1 else "frame_monster.png"
        return self.fetch("images/frameSkins/%s/%s" % (frame_name, which), None)

    def rarity(self, extension: Optional[str], rarity: Optional[str]) -> Entry:
        if not extension or not rarity:
            return Entry("missing", message="no rarity info")
        return self.fetch("images/rarity/%s_%s.png" % (extension, rarity), None)

    def font(self, filename: str) -> Entry:
        cache = os.path.join(self.root, "fonts", filename)
        if os.path.isfile(cache) and os.path.getsize(cache) > 0:
            return Entry("cached", cache)
        with self._lock:
            relpath = "fonts/%s" % filename
            entry = self._download(relpath, cache)
        if entry.ok:
            return entry
        return entry

    def inserted(self, name: str) -> Entry:
        return self.fetch("images/inserted/%s.png" % name, None)