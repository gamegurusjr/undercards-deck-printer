# -*- coding: utf-8 -*-
"""Card-face compositor (pure Pillow), replacing the browser's DOM pipeline.

Geometry and colors are the official client's own (cards.css + frames.css +
style.css of undercards.net, Undertale frame):

    card            176 x 246
    cardBackground  (8, 8) 160 x 230  black
    cardHeader      (8, 6) 160 x 28  rgba(0,0,0,.7)   (name/cost top = 9)
    cardFooter      (8, 128) 160x112 rgba(0,0,0,.7)   (desc top = 129)
    monster art     (8, 36) 160 x 90    (full-skin: (8, 8) 160 x 230)
    spell art       standard (8, 36) 160x90 / full-skin (8, 8) 160x230
    cardName        x 13..119 (padding 5), 24px line box at y=9, 14px font
    cardCost        (128..168, y 9..33) centered, 20px, #00d0ff
    cardATK         (8..48,   y 213..237) 20px, #f0003c          (monsters)
    cardHP          (128..168, y 213..237) 20px, #0dd000         (monsters)
    cardRarity gem  (76, 213) 24 x 24
    cardDesc        (8, 129) 160 x 80, 12px centered, vertically centered
    tribes          right-anchored icons - skipped in print (no official
                    print path for them; noted in README)

Everything is scaled by an integer factor S (default 5 = 880x1230 px),
which is >= 300 DPI at the printed 180x252 pt size.

Text fitting mirrors the official shrink loops (getResizedFontSize).
Card 59 (Sans) uses the game's own sans.ttf + lowercase, like .card-59 CSS.
"""

from __future__ import annotations

import os
import threading
from typing import Dict, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

from . import i18n as i18nmod
from .card_db import Card, Artifact

__all__ = ["compose_card", "compose_artifact", "font_path", "set_placeholder_font_path"]

FONT_REG_NAME = "DTM-Mono.otf"
FONT_SANS_NAME = "sans.ttf"

CARD_W = 176
CARD_H = 246

COL_COST = "#00d0ff"
COL_ATK = "#f0003c"
COL_HP = "#0dd000"
COL_WHITE = "#ffffff"
COL_MISSING = "#ff2200"
HEADER_ALPHA = 179  # 0.7 * 255

_FONT_CACHE: Dict[Tuple[str, int], "ImageFont.FreeTypeFont"] = {}
_FONT_LOCK = threading.Lock()
_PLACEHOLDER_FONT = {"path": ""}


def font_path(fonts_dir: str, name: str = FONT_REG_NAME) -> str:
    return os.path.join(fonts_dir, name)


def set_placeholder_font_path(path: str) -> None:
    _PLACEHOLDER_FONT["path"] = path


def set_primary_font_path(path: str) -> None:
    set_placeholder_font_path(path)


def _font(path: str, size: int) -> "ImageFont.FreeTypeFont":
    key = (os.path.normpath(path), size)
    with _FONT_LOCK:
        cached = _FONT_CACHE.get(key)
        if cached is None:
            if not os.path.isfile(path):
                raise FileNotFoundError("font missing: %s" % path)
            cached = ImageFont.truetype(path, size)
            _FONT_CACHE[key] = cached
        return cached


def _measure(text: str, font) -> float:
    return font.getlength(text)


# ---------------------------------------------------------------------------
# segments -> tokens (with leading-space flags) -> wrapped lines -> draw
# ---------------------------------------------------------------------------
Tok = Tuple[str, "i18nmod.Seg", bool]     # token stream entries
Line = Tuple[str, "i18nmod.Seg"]          # wrapped line entries
IMG_TOK = "\x00IMG\x00"


def _tokenize(segs: List["i18nmod.Seg"]) -> List[Tok]:
    """Word tokens with a *leading_space* flag.

    A space is only emitted when the official text actually contains one:
    either an explicit space in a segment, or the segment boundary right
    after a piece that ended in a space.  Juxtaposed segments with no space
    in the source (e.g. "{{KW:CHARGE}}.", "-1/-2") stay joined, exactly like
    the official DOM rendering.
    """
    tokens: List[Tok] = []
    pending_space = False
    for seg in segs:
        if seg.image is not None:
            tokens.append((IMG_TOK, seg, pending_space))
            pending_space = False
            continue
        pieces = seg.text.split(" ")
        for idx, piece in enumerate(pieces):
            if idx > 0:
                pending_space = True
            if piece:
                tokens.append((piece, seg, pending_space))
                pending_space = False
            # an empty piece just keeps pending_space True
            if idx == len(pieces) - 1 and idx > 0 and not piece:
                pending_space = True
    return tokens


def _wrap_tokens(tokens: List[Tok], max_width: float, font,
                 img_px: Dict[str, float]) -> List[List[Tok]]:
    lines: List[List[Tok]] = []
    cur: List[Tok] = []
    cur_w = 0.0
    space_w = _measure(" ", font)

    # lines hold 2-tuples (tok, seg); explicit " " tokens carry the spacing
    def push() -> None:
        nonlocal cur, cur_w
        if cur:
            lines.append(cur)
            cur = []
            cur_w = 0.0

    for tok, seg, leading in tokens:
        if tok == IMG_TOK:
            w = img_px.get(seg.image[0], 0.0)
        else:
            w = _measure(tok, font)
        gap = space_w if (cur and leading) else 0.0
        if cur and cur_w + gap + w > max_width:
            push()
            gap = 0.0
        if w > max_width and not cur:
            # hard-split very long words
            piece = ""
            for ch in tok:
                if piece and _measure(piece + ch, font) > max_width:
                    cur.append((piece, seg))
                    push()
                    piece = ch
                else:
                    piece += ch
            if piece:
                cur.append((piece, seg))
                cur_w = _measure(piece, font)
            continue
        if gap:
            cur.append((" ", seg))
            cur_w += space_w
        cur.append((tok, seg))
        cur_w += w
    push()
    return [ln for ln in lines if ln] or [[]]


def _draw_lines(layer: "Image.Image", draw: ImageDraw.ImageDraw,
                lines: List[List[Tok]], box_left: float, box_top: float,
                box_width: float, line_height: float, font,
                default_color: str, scale: int,
                image_loader=None) -> float:
    y = box_top
    for line in lines:
        widths: List[float] = []
        for tok, seg in line:
            if tok == IMG_TOK:
                widths.append(max(1.0, float((seg.image[2] or 16) * scale)))
            else:
                widths.append(_measure(tok, font))
        total = min(box_width, sum(widths))
        x = box_left + max(0.0, (box_width - total) / 2.0)
        underline_from = None
        underline_to = None
        for (tok, seg), w in zip(line, widths):
            if tok == IMG_TOK:
                if image_loader is not None and seg.image:
                    path = image_loader(seg.image[0])
                    if path:
                        try:
                            img = Image.open(path).convert("RGBA")
                            img = img.resize((max(1, seg.image[2] * scale),
                                              max(1, seg.image[3] * scale)))
                            y_img = y + max(0.0, (line_height - seg.image[3] * scale) / 2.0)
                            layer.alpha_composite(img, (int(x), int(y_img)))
                        except OSError:
                            pass
                if underline_from is not None:
                    draw.line([(underline_from, y + line_height - 1.2 * scale),
                               (underline_to, y + line_height - 1.2 * scale)],
                              fill=default_color, width=scale)
                    underline_from = None
                x += w
                continue
            color = seg.color or default_color
            draw.text((x, y), tok, font=font, fill=color)
            if seg.underline and tok != "":
                if underline_from is None:
                    underline_from = x
                underline_to = x + w
            elif underline_from is not None and tok not in (" ", ""):
                draw.line([(underline_from, y + line_height - 1.2 * scale),
                           (underline_to, y + line_height - 1.2 * scale)],
                          fill=default_color, width=scale)
                underline_from = None
            x += w
        if underline_from is not None:
            draw.line([(underline_from, y + line_height - 1.2 * scale),
                       (underline_to, y + line_height - 1.2 * scale)],
                      fill=default_color, width=scale)
        y += line_height
    return y - box_top


def _shrink_fit(text: str, max_width: float, start_size: int, min_size: int,
                fontfile: str) -> "ImageFont.FreeTypeFont":
    size = start_size
    while size > min_size:
        f = _font(fontfile, size)
        if _measure(text, f) <= max_width:
            break
        size -= 1
    return _font(fontfile, size)


def _fit_desc_lines(segs: List["i18nmod.Seg"], font_file: str, width_px: float,
                    start_size: int, min_size: int, max_height_px: float,
                    scale: int, img_px: Dict[str, float]) -> Tuple[List[List[Tok]], object, int, int]:
    """Official shrink loop: reduce font size until the wrapped block fits."""
    size = start_size
    while True:
        font = _font(font_file, size)
        line_h = int(size * 1.25)
        lines = _wrap_tokens(_tokenize(segs), width_px, font, img_px)
        used = len(lines) * line_h
        if used <= max_height_px or size <= min_size:
            return lines, font, line_h, size
        size -= 1


# ---------------------------------------------------------------------------
# placeholder art
# ---------------------------------------------------------------------------

def _placeholder_art(w: int, h: int, s: int, label: str) -> "Image.Image":
    img = Image.new("RGBA", (w, h), (24, 24, 24, 255))
    d = ImageDraw.Draw(img)
    step = 14 * s
    for yy in range(-h, h * 2, step * 2):
        d.line([(0, yy), (w, yy + h)], fill=(56, 56, 56, 255), width=max(1, step // 2))
    label = label.upper()

    def fit_font(text: str, max_w: int) -> "ImageFont.FreeTypeFont":
        size = max(10, int(max_w / max(1, len(text)) * 1.6))
        path = _PLACEHOLDER_FONT["path"] or FONT_REG_NAME
        try:
            while size > 10:
                f = _font(path, size)
                if f.getlength(text) <= max_w:
                    return f
                size -= 2
            return _font(path, max(10, size))
        except Exception:  # noqa: BLE001 - font not available: big bitmap text
            return None

    f = fit_font(label, w - 10 * s)
    if f is not None:
        tw = f.getlength(label)
        d.text(((w - tw) / 2, h * 0.42), label, font=f, fill=COL_MISSING)
    sub = "(overlay this area yourself)"
    f2 = fit_font(sub, w - 20 * s)
    if f2 is not None:
        tw2 = f2.getlength(sub)
        d.text(((w - tw2) / 2, h * 0.62), sub, font=f2, fill=(200, 200, 200, 255))
    return img


# ---------------------------------------------------------------------------

def _hex2rgba(color: str, alpha: int = 255) -> Tuple[int, int, int, int]:
    color = color.lstrip("#")
    if len(color) == 3:
        color = "".join(c * 2 for c in color)
    return (int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16), alpha)


def _rect_layer(w: int, h: int, x: int, y: int, r_w: int, r_h: int, fill) -> "Image.Image":
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rectangle([x, y, x + r_w, y + r_h], fill=fill)
    return layer


def _resolve_inserted(assets, src: str) -> Optional[str]:
    if not assets:
        return None
    try:
        if src.startswith("/images/inserted/"):
            name = src.rsplit("/", 1)[-1]
            if name.lower().endswith(".png"):
                name = name[:-4]
            entry = assets.inserted(name)
            return entry.path if entry.ok else None
    except Exception:  # noqa: BLE001 - an inline icon must never break a face
        return None
    return None


def _centered(text: str, box_left: float, box_width: float, font) -> float:
    tw = font.getlength(text)
    return box_left + (box_width - tw) / 2.0


def compose_card(card: Card, art_path: Optional[str], assets, i18n: "i18nmod.I18n",
                 frame_path: Optional[str], rarity_path: Optional[str],
                 scale: int = 5, fonts_dir: str = "") -> "Image.Image":
    s = scale
    W, H = CARD_W * s, CARD_H * s
    fonts_dir = fonts_dir or (assets.root if assets else ".")
    font_reg = font_path(fonts_dir, FONT_REG_NAME)
    set_primary_font_path(font_reg)

    face = Image.new("RGBA", (W, H), (0, 0, 0, 255))

    # --- art ------------------------------------------------------------
    if card.typeCard == 1 and card.typeSkin == 1:
        art_box = (8 * s, 8 * s, 160 * s, 230 * s)          # full-skin
    else:
        art_box = (8 * s, 36 * s, 160 * s, 90 * s)          # standard + monster
    art_ok = False
    if art_path and os.path.isfile(art_path):
        try:
            art = Image.open(art_path).convert("RGBA")
            art = art.resize((art_box[2], art_box[3]))
            face.alpha_composite(art, (art_box[0], art_box[1]))
            art_ok = True
        except OSError:
            art_ok = False
    if not art_ok:
        face.alpha_composite(_placeholder_art(art_box[2], art_box[3], s, "MISSING ART"),
                             (art_box[0], art_box[1]))

    # --- translucent header/footer ---------------------------------------
    face.alpha_composite(_rect_layer(W, H, 8 * s, 6 * s, 160 * s, 28 * s,
                                     (0, 0, 0, HEADER_ALPHA)))
    face.alpha_composite(_rect_layer(W, H, 8 * s, 128 * s, 160 * s, 112 * s,
                                     (0, 0, 0, HEADER_ALPHA)))

    # --- frame ------------------------------------------------------------
    if frame_path and os.path.isfile(frame_path):
        try:
            frame = Image.open(frame_path).convert("RGBA")
            if frame.size != (W, H):
                frame = frame.resize((W, H), Image.LANCZOS)
            face.alpha_composite(frame, (0, 0))
        except OSError:
            pass

    # --- text layer --------------------------------------------------------
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)

    is_sans = (card.fixedId == 59 or card.id == 59)
    name_color = COL_WHITE
    if card.typeCard == 1:
        from .i18n import CLASS_COLORS
        soul = (card.soul or "MONSTER").upper()
        name_color = CLASS_COLORS.get(soul, COL_WHITE)
    name_file = font_reg
    if is_sans:
        sans_path = font_path(fonts_dir, FONT_SANS_NAME)
        if os.path.isfile(sans_path):
            name_file = sans_path

    fid = card.fixedId or card.id
    name = i18n.plain("card-name-%s" % fid, "1") or card.name or ""
    if is_sans:
        name = name.lower()
    name_font = _shrink_fit(name, 106 * s, 14 * s, 8 * s, name_file)
    # vertical center of the 24px line box starting at y=9
    top = 9 * s + max(0, (24 * s - (name_font.size + 2)) // 2)
    draw.text((13 * s, top), name, font=name_font, fill=name_color)

    if card.cost is not None:
        f = _font(font_reg, 20 * s)
        x = _centered(str(card.cost), 128 * s, 40 * s, f)
        draw.text((x, 9 * s + 2 * s), str(card.cost), font=f, fill=COL_COST)

    if card.typeCard != 1:
        f = _font(font_reg, 20 * s)
        if card.atk is not None:
            x = _centered(str(card.atk), 8 * s, 40 * s, f)
            draw.text((x, 213 * s + 2 * s), str(card.atk), font=f, fill=COL_ATK)
        if card.hp is not None:
            x = _centered(str(card.hp), 128 * s, 40 * s, f)
            draw.text((x, 213 * s + 2 * s), str(card.hp), font=f, fill=COL_HP)
        if rarity_path and os.path.isfile(rarity_path):
            try:
                gem = Image.open(rarity_path).convert("RGBA").resize((24 * s, 24 * s))
                face.alpha_composite(gem, (76 * s, 213 * s))
            except OSError:
                pass

    # --- description -------------------------------------------------------
    desc_segs = i18n.render("card-%s" % fid)
    img_px: Dict[str, float] = {}
    for seg in desc_segs:
        if seg.image:
            img_px[seg.image[0]] = float((seg.image[2] or 16) * scale)

    lines, font, line_h, size = _fit_desc_lines(
        desc_segs, font_reg, 160 * s, 12 * s, 6 * s, 81 * s, s, img_px)
    used = len(lines) * line_h
    top_desc = 129 * s + max(0.0, (80 * s - used) / 2.0)
    _draw_lines(layer, draw, lines, 8 * s, top_desc, 160 * s, line_h,
                font, COL_WHITE, s, image_loader=lambda src: _resolve_inserted(assets, src))

    face.alpha_composite(layer, (0, 0))
    return face


def compose_artifact(artifact: Artifact, art_path: Optional[str], assets,
                     i18n: "i18nmod.I18n", scale: int = 5,
                     fonts_dir: str = "") -> "Image.Image":
    """Simplified artifact face for the optional artifact sheet.

    Artifacts are never rendered as cards by the official client, so this is
    a clearly-labelled approximation: official frame proportions, i18n name
    and description text, the artifact emblem centered.
    """
    s = scale
    W, H = CARD_W * s, CARD_H * s
    fonts_dir = fonts_dir or (assets.root if assets else ".")
    font_reg = font_path(fonts_dir, FONT_REG_NAME)
    set_primary_font_path(font_reg)

    face = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    face.alpha_composite(_rect_layer(W, H, 8 * s, 6 * s, 160 * s, 28 * s,
                                     (0, 0, 0, HEADER_ALPHA)))
    face.alpha_composite(_rect_layer(W, H, 8 * s, 128 * s, 160 * s, 112 * s,
                                     (0, 0, 0, HEADER_ALPHA)))

    draw = ImageDraw.Draw(face)
    name = i18n.plain("artifact-name-%s" % artifact.id, "") or artifact.name or ""
    name_font = _shrink_fit(name, 106 * s, 14 * s, 8 * s, font_reg)
    top = 9 * s + max(0, (24 * s - (name_font.size + 2)) // 2)
    draw.text((13 * s, top), name, font=name_font, fill=COL_WHITE)

    if art_path and os.path.isfile(art_path):
        try:
            art = Image.open(art_path).convert("RGBA")
            area_w, area_h = 150 * s, 90 * s
            ratio = min(area_w / float(art.width), area_h / float(art.height))
            if art.width * ratio > area_w or art.height * ratio > area_h:
                ratio = min(area_w / float(art.width), area_h / float(art.height))
            aw, ah = max(1, int(art.width * ratio)), max(1, int(art.height * ratio))
            art = art.resize((aw, ah))
            face.alpha_composite(art, ((W - aw) // 2, 34 * s + (area_h - ah) // 2))
        except OSError:
            pass

    desc_segs = i18n.render("artifact-%s" % artifact.id)
    lines, font, line_h, _ = _fit_desc_lines(
        desc_segs, font_reg, 160 * s, 12 * s, 6 * s, 81 * s, s, {})
    used = len(lines) * line_h
    top_desc = 129 * s + max(0.0, (80 * s - used) / 2.0)
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d2 = ImageDraw.Draw(layer)
    _draw_lines(layer, d2, lines, 8 * s, top_desc, 160 * s, line_h, font,
                COL_WHITE, s, image_loader=lambda src: _resolve_inserted(assets, src))
    face.alpha_composite(layer, (0, 0))
    draw.text((8 * s, 224 * s), "Artifact", font=_font(font_reg, 10 * s),
              fill="#8fa0b0")
    return face
