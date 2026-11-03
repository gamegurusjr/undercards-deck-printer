# -*- coding: utf-8 -*-
"""Optional /AllCards harvest through the user's OWN signed-in browser session.

The card catalogue endpoint ``GET /AllCards`` on undercards.net is part of the
signed-in game client (the official card.js calls it on every page load).  To
keep the project on the right side of the site's rules this module does NOT
scrape, guess cookies, solve challenges or use headless evasion of any kind.
It simply:

1. opens a real, visible Chromium window (Playwright persistent profile) that
   the user logs into themselves;
2. once the user is in, it performs the exact same ``GET /AllCards`` request
   the page would perform, from inside that page, with the session's own
   cookies (``page.evaluate('fetch("/AllCards")')``);
3. merges the authoritative payload into the local DB (card_db.CardDB).

Playwright is an OPTIONAL dependency: without it the app still works fully
from the bundled wiki-based database.  The harvest only *adds* the exact
official fields (and no personal data is ever sent anywhere - everything
stays on this machine).
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Callable, List, Optional

__all__ = ["HarvestError", "HarvestState", "start_harvest", "get_state", "cancel",
           "sync_available"]

SITE_URL = "https://undercards.net"
_PROFILE_SENTINEL = object()


class HarvestError(RuntimeError):
    pass


@dataclass
class HarvestState:
    status: str = "idle"          # idle | starting | waiting-login | fetching | done | error | cancelled
    message: str = ""
    log: List[str] = field(default_factory=list)
    progress: Optional[dict] = None   # {"cards": n} once fetched
    error: Optional[str] = None
    started_at: float = 0.0
    finished_at: float = 0.0

    def snapshot(self) -> dict:
        return {
            "status": self.status,
            "message": self.message,
            "log": list(self.log[-40:]),
            "progress": self.progress,
            "error": self.error,
        }


_state_lock = threading.Lock()
_state = HarvestState()
_cancel_event = threading.Event()

# The running browser thread keeps its own handle so the web app can ask the
# page to fetch AllCards again without restarting (not required for the main
# flow, kept for future use).
_browser_ref = {"thread": None}


def sync_available() -> bool:
    """True when Playwright's sync API can actually be imported."""
    try:
        import playwright.sync_api  # noqa: F401  # PLW2903
        return True
    except ImportError:
        return False


def get_state() -> dict:
    with _state_lock:
        return _state.snapshot()


def _set(status: str, message: str = "", error: Optional[str] = None,
         progress: Optional[dict] = None, append_log: Optional[str] = None) -> None:
    with _state_lock:
        _state.status = status
        if message:
            _state.message = message
        if error is not None:
            _state.error = error
        if progress is not None:
            _state.progress = progress
        if append_log:
            stamp = time.strftime("%H:%M:%S")
            # the message book may contain nothing sensitive, still: trim
            _state.log.append("%s  %s" % (stamp, append_log))
        if status in ("done", "error", "cancelled"):
            _state.finished_at = time.time()


def cancel() -> None:
    _cancel_event.set()
    _set("cancelled", "Harvest cancelled - the browser window can be closed safely.",
         append_log="cancel requested")


def _reset() -> None:
    _cancel_event.clear()
    with _state_lock:
        global _state
        _state = HarvestState()
        _state.status = "starting"
        _state.started_at = time.time()


PLAYWRIGHT_HINT = (
    "Playwright is not installed. To enable the optional /AllCards harvest run:\n"
    "    pip install playwright\n"
    "    python -m playwright install chromium\n"
    "(first launch opens a visible browser window for you to log in yourself - "
    "the app never touches credentials.)"
)


def _page_has_login(page) -> bool:
    """Best effort login probe: the game shell only shows the AllCards fetch
    once the client is served; any HTTP-200 JSON with a cards payload means we
    are inside the signed-in experience."""
    data = page.evaluate(
        """async () => {
            let out = {status: 0, text: null};
            try {
                const resp = await fetch('AllCards', {credentials: 'include',
                                                      headers: {'Accept': 'application/json'}});
                out.status = resp.status;
                const ct = (resp.headers.get('content-type') || '');
                if (resp.ok) out.text = await resp.text();
                else out.text = null;
                out.ct = ct;
            } catch (e) {
                out.err = String(e);
            }
            return out;
        }"""
    )
    return data


def _extract_cards(raw_text: str) -> List[dict]:
    """Official client: response = {'cards': '<json string of card array>'}."""
    data = json.loads(raw_text)
    if isinstance(data, dict) and isinstance(data.get("cards"), str):
        entries = json.loads(data["cards"])
    elif isinstance(data, dict) and isinstance(data.get("cards"), list):
        entries = data["cards"]
    elif isinstance(data, list):
        entries = data
    else:
        raise HarvestError("Unexpected /AllCards response shape.")
    if not isinstance(entries, list):
        raise HarvestError("/AllCards did not contain a card list.")
    entries = [e for e in entries if isinstance(e, dict) and e.get("id") is not None]
    if not entries:
        raise HarvestError("/AllCards returned 0 usable card entries.")
    return entries


def start_harvest(data_dir: str, on_merged: Optional[Callable[[dict], dict]] = None) -> bool:
    """Launch the harvest thread.  Returns False when already running.

    ``on_merged`` receives the parsed card entry list and should persist it
    (webapp passes a callback that merges into CardDB and saves db.json).
    """
    with _state_lock:
        if _state.status in ("starting", "waiting-login", "fetching"):
            return False
    _reset()
    thread = threading.Thread(
        target=_run, args=(data_dir, on_merged), daemon=True, name="allcards-harvest")
    _browser_ref["thread"] = thread
    thread.start()
    return True


def _run(data_dir: str, on_merged: Optional[Callable[[dict], dict]]) -> None:
    profile_dir = os.path.join(data_dir, "browser_profile")
    raw_path = os.path.join(data_dir, "allcards_raw.json")
    try:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            _set("error", PLAYWRIGHT_HINT, error="playwright_missing",
                 append_log="playwright not installed")
            return

        _set("starting", "Launching a visible Chromium window (your own profile)...",
             append_log="launching chromium")
        os.makedirs(profile_dir, exist_ok=True)
        entries: List[dict] = []
        with sync_playwright() as p:
            browser = p.chromium.launch_persistent_context(
                profile_dir, headless=False,
                viewport={"width": 1180, "height": 860},
                args=["--window-size=1180,860"],
            )
            page = browser.pages[0] if browser.pages else browser.new_page()
            try:
                page.goto(SITE_URL, wait_until="domcontentloaded", timeout=45000)
            except Exception as exc:  # noqa: BLE001 - user can still proceed manually
                _set("waiting-login",
                     "Could not load %s automatically (%s). Navigate to undercards.net "
                     "in the opened window yourself and log in." % (SITE_URL, exc),
                     append_log="goto failed; awaiting manual navigation")

            while not _cancel_event.is_set():
                page.bring_to_front()
                _set("waiting-login",
                     "Log in with YOUR account in the opened browser window. "
                     "The app polls /AllCards with the page's own session only.",
                     append_log="waiting for signed-in session")
                data = _page_has_login(page)
                if data.get("status") == 200 and data.get("text"):
                    _set("fetching", "Got an authenticated /AllCards response. Parsing...",
                         append_log="/AllCards 200 OK")
                    try:
                        entries = _extract_cards(data["text"])
                    except (ValueError, HarvestError) as exc:
                        _set("error", str(exc), error="parse", append_log="parse failed")
                        browser.close()
                        return
                    break
                # remain waiting; a page that reloads due to login may detach listeners
                for _ in range(20):
                    if _cancel_event.is_set():
                        break
                    time.sleep(0.25)
                    try:  # keep the page responsive & detect closes
                        if page.url and "undercards" not in (page.url or ""):
                            try:
                                page.goto(SITE_URL, wait_until="domcontentloaded", timeout=30000)
                            except Exception:  # noqa: BLE001
                                pass
                    except Exception:  # noqa: BLE001 - window closed by user
                        _set("cancelled", "Browser window was closed.",
                             append_log="browser closed by user")
                        return
                else:
                    if _cancel_event.is_set():
                        _set("cancelled", "Harvest cancelled.", append_log="cancelled")
                        browser.close()
                        return
                    continue

            if _cancel_event.is_set():
                _set("cancelled", "Harvest cancelled.", append_log="cancelled")
                browser.close()
                return

            _set("fetching", "Saving the catalogue locally (no upload anywhere)...",
                 progress={"cards": len(entries)},
                 append_log="parsed %d card entries" % len(entries))
            with open(raw_path, "w", encoding="utf-8") as fh:
                json.dump(entries, fh, ensure_ascii=False)

            merged: dict = {}
            if on_merged is not None:
                merged = on_merged(entries) or {}
            _set("done",
                 "Merged %s card entries into the local database. "
                 "Your window stays open; you can close it whenever you like." % len(entries),
                 progress={"cards": len(entries),
                           **({"db": merged} if merged else {})},
                 append_log="merged: %r" % (merged,))
            try:
                browser.close()
            except Exception:  # noqa: BLE001 - closing is best effort
                pass
    except Exception as exc:  # noqa: BLE001 - top level: report, never crash
        detail = "%s: %s" % (type(exc).__name__, exc)
        _set("error", "Harvest failed: %s. Nothing was changed in the database." % detail,
             error=detail, append_log=detail, progress=None)
        traceback.print_exc()
