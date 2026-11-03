# -*- coding: utf-8 -*-
"""Card database + translation store used by the whole pipeline.

The bundled offline database (``data/db.json``) is generated from the
undercards.miraheze.org wiki (CC BY-SA) plus the official public English
translation file (``/translation/en.json``). Card *names* and *descriptions*
always come from the official translation keys (``card-name-<fixedId>``,
``card-<fixedId>``), rendered by :mod:`app.i18n`, exactly like the game does.

Optional upgrade path: the user signs into *their own* account through the
bundled ``harvest.py`` (Playwright) and the app merges the authoritative
``/AllCards`` payload (see README).
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from . import i18n as i18nmod

__all__ = ["Card", "Artifact", "CardDB"]


@dataclass
class Card:
    id: int
    fixedId: Optional[int] = None
    name: Optional[str] = None            # fallback; official name comes from i18n
    image: Optional[str] = None           # art asset base name (images/cards/X.png)
    rarity: Optional[str] = None
    extension: Optional[str] = None       # BASE / DELTARUNE / UTY / ...
    cost: Optional[int] = None
    atk: Optional[int] = None
    hp: Optional[int] = None
    soul: Optional[str] = None            # MONSTER for monsters, soul name for spells
    typeCard: int = 0                     # 0 monster, 1 spell
    typeSkin: int = 0                     # 0 standard, 1 full, 2 breaking
    frame: str = "Undertale"
    tribes: List[str] = field(default_factory=list)
    wiki_name: Optional[str] = None       # page title on the wiki (for attribution/logs)
    wiki_image_file: Optional[str] = None  # e.g. Card_Froggit.png (wiki-hosted copy)

    @classmethod
    def from_row(cls, row: dict) -> "Card":
        def _i(name):
            v = row.get(name)
            if v in (None, "", "-"):
                return None
            try:
                return int(v)
            except (TypeError, ValueError):
                return str(v)

        return cls(
            id=_i("id") or 0,
            fixedId=_i("fixedId"),
            name=row.get("name"),
            image=row.get("image"),
            rarity=row.get("rarity"),
            extension=row.get("extension"),
            cost=_i("cost"),
            atk=_i("atk"),
            hp=_i("hp"),
            soul=row.get("soul"),
            typeCard=_i("typeCard") or 0,
            typeSkin=_i("typeSkin") or 0,
            frame=row.get("frame") or "Undertale",
            tribes=list(row.get("tribes") or []),
            wiki_name=row.get("wiki_name"),
            wiki_image_file=row.get("wiki_image_file"),
        )

    def official_name(self, i18n: "i18nmod.I18n") -> str:
        """Name exactly as appendCard renders it: i18n(card-name-fixedId, 1)."""
        fid = self.fixedId if self.fixedId not in (None, self.id) else self.id
        name = i18n.plain("card-name-%s" % fid, "1")
        name = name.strip()
        if name:
            return name
        return self.name or ("card #%d" % self.id)

    def official_desc_key(self) -> str:
        fid = self.fixedId if self.fixedId not in (None, self.id) else self.id
        return "card-%s" % fid


@dataclass
class Artifact:
    id: int
    name: Optional[str] = None
    image: Optional[str] = None           # images/artifacts/X.png base name
    wiki_image_file: Optional[str] = None
    wiki_name: Optional[str] = None

    @classmethod
    def from_row(cls, row: dict) -> "Artifact":
        def _i(name):
            v = row.get(name)
            if v in (None, "", "-"):
                return None
            try:
                return int(v)
            except (TypeError, ValueError):
                return str(v)
        return cls(
            id=_i("id") or 0,
            name=row.get("name"),
            image=row.get("image"),
            wiki_image_file=row.get("wiki_image_file"),
            wiki_name=row.get("wiki_name"),
        )

    def official_name(self, i18n: "i18nmod.I18n") -> str:
        name = i18n.plain("artifact-name-%s" % self.id, "")
        name = name.strip()
        if name:
            return name
        return self.name or ("artifact #%d" % self.id)


class CardDB:
    """In-memory database loaded from the bundled JSON + translation file."""

    def __init__(self, cards: Dict[int, Card], artifacts: Dict[int, Artifact],
                 i18n: "i18nmod.I18n", meta: dict) -> None:
        self.cards = cards
        self.artifacts = artifacts
        self.i18n = i18n
        self.meta = meta
        self.lock = threading.RLock()
        self.has_authoritative = bool(meta.get("authoritative", False))

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, data_dir: str, image_resolver=None) -> "CardDB":
        db_path = os.path.join(data_dir, "db.json")
        en_path = os.path.join(data_dir, "en.json")
        if not os.path.isfile(db_path):
            raise FileNotFoundError(
                "Card database %r is missing. Run tools/build_db.py once (or open the "
                "app and click Refresh card database) to create it." % db_path)
        if not os.path.isfile(en_path):
            raise FileNotFoundError(
                "Translation file %r is missing. Run tools/build_db.py to (re)create it." % en_path)
        with open(db_path, "r", encoding="utf-8") as fh:
            bundle = json.load(fh)
        with open(en_path, "r", encoding="utf-8") as fh:
            messages = json.load(fh)
        cards = {int(k): Card.from_row(v) for k, v in (bundle.get("cards") or {}).items()}
        artifacts = {int(k): Artifact.from_row(v)
                     for k, v in (bundle.get("artifacts") or {}).items()}
        meta = dict(bundle.get("_meta") or {})
        i18n = i18nmod.I18n(messages, image_resolver=image_resolver)
        return cls(cards, artifacts, i18n, meta)

    # ------------------------------------------------------------------
    def card(self, card_id: int) -> Optional[Card]:
        return self.cards.get(int(card_id))

    def artifact(self, artifact_id: int) -> Optional[Artifact]:
        return self.artifacts.get(int(artifact_id))

    def has_desc(self, card: Card) -> bool:
        return self.i18n.has_key(card.official_desc_key())

    def desc(self, card: Card):
        return self.i18n.render(card.official_desc_key())

    def missing_cards(self, card_ids: List[int]) -> List[int]:
        return [cid for cid in card_ids if cid not in self.cards]

    def missing_artifacts(self, artifact_ids: List[int]) -> List[int]:
        return [aid for aid in artifact_ids if aid not in self.artifacts]

    # ------------------------------------------------------------------
    # /AllCards merge (harvest path; authoritative)
    # ------------------------------------------------------------------
    def merge_allcards_payload(self, payload) -> dict:
        """Merge the signed-in user's own /AllCards dump into the in-memory DB.

        The payload shape (from card.js fetchAllCards) is {"cards": "<json str>"}
        or an already-parsed list of card dicts.  AllCards is authoritative:
        for every id present it replaces any wiki-derived row, and the build
        meta records that the data is no longer wiki-only.
        """
        entries: List[dict]
        if isinstance(payload, dict) and "cards" in payload and isinstance(payload["cards"], str):
            entries = json.loads(payload["cards"])
        elif isinstance(payload, dict) and "cards" in payload and isinstance(payload["cards"], list):
            entries = payload["cards"]
        elif isinstance(payload, list):
            entries = payload
        else:
            raise ValueError(
                "/AllCards payload does not look like the official dump "
                "(expected {\"cards\": \"<json string>\"} or a card list).")

        replaced = 0
        added = 0
        with self.lock:
            for raw in entries:
                if not isinstance(raw, dict) or "id" not in raw:
                    continue
                cid = int(raw["id"])
                fid = raw.get("fixedId", cid)
                soul = raw.get("soul")
                if isinstance(soul, dict):
                    soul = soul.get("name")
                card = Card(
                    id=cid,
                    fixedId=fid,
                    name=None,
                    image=raw.get("image"),
                    rarity=raw.get("rarity"),
                    extension=raw.get("extension"),
                    cost=raw.get("cost"),
                    atk=raw.get("attack"),
                    hp=raw.get("hp"),
                    soul=soul or ("MONSTER" if raw.get("typeCard", 0) != 1 else None),
                    typeCard=raw.get("typeCard", 0),
                    typeSkin=raw.get("typeSkin", 0),
                    frame=(raw.get("frameSkinName") or "Undertale").replace(" ", "_"),
                    tribes=[t.get("name") for t in (raw.get("tribes") or [])
                            if isinstance(t, dict) and t.get("name")],
                )
                if cid in self.cards:
                    replaced += 1
                else:
                    added += 1
                self.cards[cid] = card
            self.meta["authoritative_ids"] = added + replaced
            self.meta["authoritative_at"] = __import__("datetime").datetime.utcnow().isoformat()
            self.has_authoritative = True
        return {"added": added, "replaced": replaced, "total": len(self.cards)}

    def save(self, data_dir: str) -> None:
        """Persist the (possibly merged) DB back to db.json."""
        bundle = {
            "_meta": self.meta,
            "cards": {str(c.id): {
                "id": c.id, "fixedId": c.fixedId, "name": c.name, "image": c.image,
                "rarity": c.rarity, "extension": c.extension, "cost": c.cost,
                "atk": c.atk, "hp": c.hp, "soul": c.soul, "typeCard": c.typeCard,
                "typeSkin": c.typeSkin, "frame": c.frame, "tribes": c.tribes,
                "wiki_name": c.wiki_name, "wiki_image_file": c.wiki_image_file,
            } for c in sorted(self.cards.values(), key=lambda c: c.id)},
            "artifacts": {str(a.id): {
                "id": a.id, "name": a.name, "image": a.image,
                "wiki_image_file": a.wiki_image_file, "wiki_name": a.wiki_name,
            } for a in sorted(self.artifacts.values(), key=lambda a: a.id)},
        }
        path = os.path.join(data_dir, "db.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(bundle, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
