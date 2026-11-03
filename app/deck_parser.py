# -*- coding: utf-8 -*-
"""Undercards deck code decoding.

Verified deck format (see README, "Deck code format"):
    base64( UTF-8 JSON )  ->  {
        "soul": "JUSTICE",
        "cardIds": [258, 258, 258, ...],
        "artifactIds": [3, 20]
    }

* Duplicated card ids (same number appearing more than once) are the official
  way quantities are encoded - they must be preserved, in order.
* The code never says whether a card is shiny; the printed face is therefore
  always the normal card art.
* Older/forked tools exported ``cards``/``artifacts``/``shinyIds`` key names as
  well; we accept those aliases but never silently invent a deck: anything that
  cannot be decoded raises :class:`DeckParseError` with a human-readable
  message instead of guessing.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import parse_qs, urlsplit

__all__ = ["Deck", "DeckParseError", "parse_deck"]

# Rough sanity range for card/artifact ids (current live DB has ids < 1010).
_CARD_ID_MAX = 4096
_ARTIFACT_ID_MAX = 512


class DeckParseError(ValueError):
    """Raised for any input that is not a decodable Undercards deck code."""


@dataclass
class Deck:
    """Decoded deck: duplicates preserved in original order."""

    cards: List[int]
    artifacts: List[int] = field(default_factory=list)
    shiny: List[int] = field(default_factory=list)
    soul: Optional[str] = None

    # helpers ------------------------------------------------------------
    @property
    def total_cards(self) -> int:
        return len(self.cards)

    @property
    def unique_cards(self) -> int:
        return len(set(self.cards))

    @property
    def duplicates(self) -> int:
        return self.total_cards - self.unique_cards

    def quantities(self) -> List[tuple]:
        """[(cardId, quantity), ...] in first-appearance order."""
        seen: List[tuple] = []
        for cid in self.cards:
            for i, (existing, _) in enumerate(seen):
                if existing == cid:
                    seen[i] = (cid, seen[i][1] + 1)
                    break
            else:  # first time
                seen.append((cid, 1))
        return seen


# ---------------------------------------------------------------------------
# base64 plumbing
# ---------------------------------------------------------------------------

_B64_RE = re.compile(r"^[A-Za-z0-9+/_\-=:]+$")


def _b64pad(s: str) -> str:
    return s + "=" * (-len(s) % 4)


def _decode_candidates(raw: str):
    """Yield (label, bytes) decode attempts in priority order."""
    cleaned = re.sub(r"\s+", "", raw)
    std = cleaned.replace("-", "+").replace("_", "/")
    for label, text in (
        ("base64", _b64pad(std)),
        ("base64(no-pad)", std.rstrip("=")),
        ("base64url", _b64pad(cleaned)),
    ):
        try:
            yield label, base64.b64decode(text, validate=False)
        except (binascii.Error, ValueError):
            continue


def _extract_code_field(text: str) -> str:
    """If the user pasted a URL containing ``code=...`` keep just the payload."""
    text = text.strip()
    if not text:
        raise DeckParseError("The deck code is empty - paste the code (or deck URL) from Undercards.")
    # Full deck URLs like https://undercards.net/Decks/?code=eyJzb3VsIjoiSlVT...
    if "code=" in text:
        try:
            split_url = urlsplit(text)
        except ValueError:
            split_url = None
        texts = []
        if split_url is not None and split_url.query and "code=" in split_url.query:
            try:
                texts.append(parse_qs(split_url.query, keep_blank_values=True).get("code", [""])[0])
            except Exception:  # noqa: BLE001 - tolerate malformed urls
                texts = []
        # also try a lenient regex in case the pasted text mangles the URL
        if not texts:
            m = re.search(r"code=([A-Za-z0-9_\-=/+%.]+)", text)
            if m:
                texts.append(m.group(1))
        if texts and texts[0]:
            # the value may still be percent-encoded
            from urllib.parse import unquote

            return unquote(texts[0]).strip()
    # Fragment form: https://.../Decks/#eyJ...  (or appends)
    if text.startswith("#"):
        return text[1:].strip()
    if "undercards" in text.lower() and "#" in text:
        frag = text.split("#", 1)[1].strip()
        if frag:
            return frag
    return text


def _as_int(value, what: str) -> int:
    if isinstance(value, bool) or value is None:
        raise DeckParseError(f"Deck {what} contains an invalid entry: {value!r}")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        value = int(value)
    if not isinstance(value, int):
        raise DeckParseError(f"Deck {what} contains an invalid entry: {value!r}")
    return value


def _id_list(payload: dict, keys: tuple, what: str, id_max: int, required: bool) -> List[int]:
    value = None
    found_key = None
    for k in keys:
        if k in payload:
            value = payload[k]
            found_key = k
            break

    if found_key is None:
        if required:
            raise DeckParseError(
                "The decoded deck has no card list (expected one of the keys %s). "
                "This does not look like an Undercards deck code."
                % ", ".join("'%s'" % k for k in keys)
            )
        return []

    if isinstance(value, dict):
        # tolerate {"cards": [{"id":..}, ...]} style exports
        inner = value.get("ids") or value.get("id") or value
        if isinstance(inner, dict) and all(isinstance(v, (int, float, str)) for v in inner.values()):
            # {id: ..., key: qty...} mapping form -> expand by quantity
            items: List[int] = []
            for k, v in inner.items():
                try:
                    cid = _as_int(k, what)
                except DeckParseError:
                    continue
                qty = 1
                try:
                    qty = max(1, int(v))
                except (TypeError, ValueError):
                    qty = 1
                items.extend([cid] * qty)
            value = items
        else:
            value = None

    if not isinstance(value, list):
        raise DeckParseError(f"Deck {what} is not a list (got {type(value).__name__}).")

    out: List[int] = []
    for v in value:
        n = _as_int(v, what)
        if n <= 0 or n > id_max:
            raise DeckParseError(
                f"Deck {what} contains an out-of-range id {n} (expected 1..{id_max}). "
                "The deck code seems corrupted."
            )
        out.append(n)
    return out


def _normalize_soul(payload: dict) -> Optional[str]:
    soul = payload.get("soul")
    if isinstance(soul, dict):
        soul = soul.get("name")
    if soul is None:
        return None
    if not isinstance(soul, str):
        return None
    soul = soul.strip().upper()
    return soul or None


def parse_deck(code_text: str) -> Deck:
    """Decode a deck code (or a URL containing it). Raises DeckParseError."""
    payload_text = _extract_code_field(code_text)

    last_error: Exception | None = None
    for label, decoded in _decode_candidates(payload_text):
        try:
            text = decoded.decode("utf-8")
            payload = json.loads(text)
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
        except json.JSONDecodeError as exc:
            last_error = exc
            continue

        if not isinstance(payload, dict):
            last_error = DeckParseError(
                f"The decoded {label} payload is {type(payload).__name__}, not a deck object."
            )
            continue

        cards = _id_list(payload, ("cardIds", "cards", "card_id", "cardID"), "card list", _CARD_ID_MAX, True)
        artifacts = _id_list(payload, ("artifactIds", "artifacts"), "artifact list", _ARTIFACT_ID_MAX, False)
        shiny = _id_list(payload, ("shinyIds", "shiny"), "shiny list", _CARD_ID_MAX, False)
        soul = _normalize_soul(payload)

        if not cards:
            raise DeckParseError(
                "The deck code decodes correctly but contains no cards "
                "(cardIds is empty) - nothing to print."
            )
        return Deck(cards=cards, artifacts=artifacts, shiny=shiny, soul=soul)

    if last_error is not None:
        raise DeckParseError(
            "Could not decode the deck code: %s. "
            "Make sure you copy the game's deck export text (it is a base64 blob, "
            "often reachable from a share link ending in ?code=...)."
            % (last_error)
        ) from last_error
    raise DeckParseError(
        "Could not decode the deck code as base64. "
        "Make sure you copy the full deck export text and that it did not get truncated."
    )
