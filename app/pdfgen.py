# -*- coding: utf-8 -*-
"""The PDF generator - fixed card size is a HARD invariant.

* Every card is exactly 180 x 252 pt = 2.5 x 3.5 in = 63.5 x 88.9 mm on paper,
  no matter how big the deck is.  Adding cards only adds pages.
* Landscape pages hold a 4 x 2 grid (8 cards) for both Letter and A4; portrait
  pages hold 3 x 2 (6 cards) - the 9-card-per-page option is impossible at
  true poker size, so the app refuses to fake it (no scaling, ever).
* The grid is centred with equal margins on every side (never stretched).
* Cutting guides: optional dashed outline exactly on the card edge plus corner
  crop marks (offset 2 pt outside each corner).
* Sheets are 300+ DPI: faces are rendered at ~880x1230 px (see cardface) and
  embedded at the fixed print size.
"""

from __future__ import annotations

import io
from typing import Callable, List, Optional, Sequence, Tuple

from reportlab.lib.colors import HexColor
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as rl_canvas

__all__ = ["CARD_W", "CARD_H", "PagingError", "page_size_pt", "grid_counts",
           "layout_boxes", "pages_needed", "build_pdf", "MM_TO_PT"]

MM_TO_PT = 72.0 / 25.4
CARD_W = 180.0   # pt = 2.5 in
CARD_H = 252.0   # pt = 3.5 in

_PAPER = {"letter": (612.0, 792.0), "a4": (595.28, 841.89)}  # portrait (w, h)


class PagingError(ValueError):
    pass


def page_size_pt(paper: str, orientation: str) -> Tuple[float, float]:
    pa = _PAPER.get(paper or "letter")
    if pa is None:
        raise PagingError("Unknown paper %r (expected 'letter' or 'a4')." % paper)
    w, h = pa
    if (orientation or "landscape") == "landscape":
        return h, w
    return w, h


def grid_counts(orientation: str) -> Tuple[int, int]:
    """(cols, rows) for a page at given orientation."""
    if (orientation or "landscape") == "landscape":
        return 4, 2
    return 3, 2


def pages_needed(count: int, orientation: str) -> int:
    cols, rows = grid_counts(orientation)
    per = cols * rows
    return max(1, (count + per - 1) // per)


def layout_boxes(paper: str, orientation: str, gap_mm: float = 4.0
                 ) -> List[Tuple[float, float]]:
    """Card top-left positions in TOP-LEFT origin page coords, centred."""
    page_w, page_h = page_size_pt(paper, orientation)
    cols, rows = grid_counts(orientation)
    gap = max(0.0, float(gap_mm)) * MM_TO_PT
    grid_w = cols * CARD_W + (cols - 1) * gap
    grid_h = rows * CARD_H + (rows - 1) * gap
    margin_x = (page_w - grid_w) / 2.0
    margin_y = (page_h - grid_h) / 2.0
    if margin_x < 0 or margin_y < 0:
        raise PagingError(
            "The fixed 2.5x3.5 in cards plus the chosen gap do not fit a %s %s page. "
            "Try a smaller gap." % (paper, orientation))
    boxes: List[Tuple[float, float]] = []
    for row in range(rows):
        for col in range(cols):
            x = margin_x + col * (CARD_W + gap)
            y_top = margin_y + row * (CARD_H + gap)
            boxes.append((x, y_top))
    return boxes


def _to_pdf_y(y_top: float, page_h: float, card_h: float = CARD_H) -> float:
    return page_h - y_top - card_h


def _crop_marks(c: rl_canvas.Canvas, x: float, y_pdf: float, page_h: float) -> None:
    """8 crop ticks outside each corner (classic offset 2pt, length 8pt)."""
    off, length = 2.0, 8.0
    light = HexColor("#666666")
    c.setStrokeColor(light)
    c.setLineWidth(0.4)
    corners = [(x, y_pdf), (x + CARD_W, y_pdf), (x, y_pdf + CARD_H), (x + CARD_W, y_pdf + CARD_H)]
    for cx, cy in corners:
        # horizontal arm
        ax = cx - length if cx in (x,) else (cx if cx == x + CARD_W else cx)
        if cx <= x:
            c.line(cx - off - length, cy, cx - off, cy)
        else:
            c.line(cx + off, cy, cx + off + length, cy)
        # vertical arm
        if cy <= y_pdf:
            c.line(cx, cy - off, cx, cy - off - length)
        else:
            c.line(cx, cy + off, cx, cy + off + length)


def build_pdf(faces: Sequence[Optional[str]], paper: str = "letter",
              orientation: str = "landscape", gap_mm: float = 4.0,
              cut_guides: bool = True, header_text: Optional[str] = None,
              pages_override: Optional[int] = None) -> Tuple[bytes, int]:
    """Compose the PDF. `faces` is the ordered list of face PNG paths
    (already missing-art-completed by cardface). Returns (pdf_bytes, pages)."""
    page_w, page_h = page_size_pt(paper, orientation)
    boxes = layout_boxes(paper, orientation, gap_mm)
    per_page = len(boxes)
    n_cards = sum(1 for f in faces if True)  # one slot per face entry
    total = pages_override if pages_override is not None else max(1, (n_cards + per_page - 1) // per_page)
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(page_w, page_h))
    c.setTitle("Undercards deck print")
    c.setSubject("2.5x3.5 in proxy print sheets")
    c.setAuthor("undercards-deck-printer")

    for page in range(total):
        if header_text:
            c.setFont("Helvetica", 6)
            c.setFillColor(HexColor("#333333"))
            c.drawString(10, page_h - 9, "%s - page %d/%d" % (header_text, page + 1, total))
        for slot, (bx, by_top) in enumerate(boxes):
            index = page * per_page + slot
            if index >= n_cards:
                break
            y_pdf = _to_pdf_y(by_top, page_h)
            face = faces[index] or None
            try:
                c.drawImage(ImageReader(face), bx, y_pdf, width=CARD_W, height=CARD_H,
                            mask="auto")
            except (IOError, OSError):
                # Should never happen (faces pre-composed); draw a plain frame
                c.setStrokeColor(HexColor("#aa0000"))
                c.rect(bx, y_pdf, CARD_W, CARD_H, stroke=1, fill=0)
                c.setFont("Helvetica", 10)
                c.setFillColor(HexColor("#aa0000"))
                c.drawCentredString(bx + CARD_W / 2, y_pdf + CARD_H / 2, "face error")
            if cut_guides:
                _crop_marks(c, bx, y_pdf, page_h)
                c.setStrokeColor(HexColor("#999999"))
                c.setLineWidth(0.4)
                c.setDash(2, 2)
                c.rect(bx, y_pdf, CARD_W, CARD_H, stroke=1, fill=0)
                c.setDash()
        c.showPage()
    c.save()
    return buf.getvalue(), total


def page_preview_png(faces: Sequence[Optional[str]], paper: str, orientation: str,
                     gap_mm: float, cut_guides: bool, page_index: int,
                     dpi: int = 96) -> Optional["io.BytesIO"]:
    """Rasterized preview of a single sheet (fast, for the UI)."""
    from PIL import Image, ImageDraw

    page_w, page_h = page_size_pt(paper, orientation)
    boxes = layout_boxes(paper, orientation, gap_mm)
    per_page = len(boxes)
    if page_index * per_page >= len(faces):
        return None                                  # page out of range
    px_w = int(round(page_w * dpi / 72.0))
    px_h = int(round(page_h * dpi / 72.0))
    img = Image.new("RGB", (px_w, px_h), (245, 245, 245))
    d = ImageDraw.Draw(img)
    pt = dpi / 72.0
    for slot, (bx, by_top) in enumerate(boxes):
        index = page_index * per_page + slot
        if index >= len(faces):
            break
        x = bx * pt
        y = by_top * pt
        w = CARD_W * pt
        h = CARD_H * pt
        face = faces[index]
        if face:
            try:
                face_img = Image.open(face).convert("RGB")
                img.paste(face_img.resize((int(w), int(h))), (int(x), int(y)))
            except (OSError, IOError):
                d.rectangle([x, y, x + w, y + h], outline=(180, 40, 40), width=2)
        if cut_guides:
            d.rectangle([x, y, x + w, y + h], outline=(120, 120, 120), width=1)
    out = io.BytesIO()
    img.save(out, format="PNG")
    out.seek(0)
    return out
