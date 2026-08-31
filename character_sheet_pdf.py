#!/usr/bin/env python3
"""character_sheet_pdf.py — render a Character as a printable PDF character sheet.

Pure stdlib: builds PDF bytes by hand (no reportlab/fpdf), matching the
"stdlib only" spirit of ddb_character.py. Uses the standard, non-embedded
Helvetica / Helvetica-Bold fonts so the file stays small.

Usage
-----
    from ddb_character import load_character
    from character_sheet_pdf import render_pdf

    render_pdf(load_character("saphire.json"), "saphire.pdf")

The sheet is meant to be printed in greyscale for in-person play:
  * Current HP and Temporary HP are always left blank (pencil boxes).
  * Skills carry a short built-in reminder of what they cover, so the
    Player's Handbook shouldn't be needed at the table.
  * The equipment list is padded with blank ruled lines so at least half
    of it is empty, ready for the player to add loot as they find it.
  * Known coin totals (as of the JSON snapshot) are printed small inside
    each currency box, next to a blank line for the current amount.
  * Spells get as much of their rules text as fits on the page.
"""

from __future__ import annotations

import html
import re
from typing import List, Optional, Tuple

from ddb_character import (
    ABBREV,
    ABILITIES,
    Character,
    SKILLS,
    fmt,
    in_filter_description,
    in_filter_summary,
    titleize,
)

__all__ = ["render_pdf"]

# --------------------------------------------------------------------------
# Page geometry
# --------------------------------------------------------------------------

PAGE_W, PAGE_H = 612.0, 792.0  # US Letter, points
MARGIN = 36.0
CONTENT_W = PAGE_W - 2 * MARGIN
BOTTOM = PAGE_H - MARGIN

# --------------------------------------------------------------------------
# Standard-14 font metrics (widths per 1000 units, codes 32-126)
# --------------------------------------------------------------------------

_HELV = [
    278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
    1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
    333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
    556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
]
_HELV_BOLD = [
    278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278,
    556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611,
    975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778,
    667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556,
    333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611,
    611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584,
]
_WIDTHS = {"F1": _HELV, "F2": _HELV_BOLD}


def text_width(s: str, font: str = "F1", size: float = 9.0) -> float:
    table = _WIDTHS.get(font, _HELV)
    total = 0
    for ch in s:
        code = ord(ch)
        idx = code - 32
        total += table[idx] if 0 <= idx < len(table) else 556
    return total / 1000.0 * size


def fit_size(text: str, max_width: float, font: str = "F1", start: float = 15.0,
             min_size: float = 8.0) -> float:
    """Largest size (down to min_size) at which `text` fits in max_width."""
    size = start
    while size > min_size and text_width(text, font, size) > max_width:
        size -= 1
    return size


def wrap(s: str, font: str, size: float, max_width: float) -> List[str]:
    lines: List[str] = []
    for para in (s or "").split("\n"):
        words = para.split()
        if not words:
            lines.append("")
            continue
        cur = words[0]
        for w in words[1:]:
            trial = f"{cur} {w}"
            if text_width(trial, font, size) <= max_width:
                cur = trial
            else:
                lines.append(cur)
                cur = w
        lines.append(cur)
    return lines


# A short bolded lead-in at the start of a paragraph, e.g. "Damage
# Resistance. You have Resistance to ..." or "Duration. The Rage lasts
# until ..." -- the rules-text convention for calling out a sub-topic.
_LEAD_IN_RE = re.compile(r"^(\S+(?:\s+\S+){0,4})\.(\s+)(.*)$", re.DOTALL)


def _wrap_with_lead(text: str, font: str, size: float, full_width: float,
                     first_line_width: float) -> List[str]:
    """Like wrap(), but the first line is filled to `first_line_width`
    (room already taken by a bold lead-in on that line) and every line
    after it uses the full width."""
    words = (text or "").split()
    if not words:
        return []
    cur, i = "", 0
    while i < len(words):
        trial = f"{cur} {words[i]}".strip()
        if not cur or text_width(trial, font, size) <= first_line_width:
            cur = trial
            i += 1
        else:
            break
    lines = [cur] if cur else []
    remainder = " ".join(words[i:])
    if remainder:
        lines.extend(wrap(remainder, font, size, full_width))
    return lines


# --------------------------------------------------------------------------
# HTML -> plain text (D&D Beyond spell descriptions come as HTML)
# --------------------------------------------------------------------------

_BLOCK_BREAK = re.compile(r"</p\s*>|<br\s*/?>|</li\s*>|</div\s*>", re.I)
_TAG = re.compile(r"<[^>]+>")
_BLANK_LINES = re.compile(r"\n{2,}")
_SPACES = re.compile(r"[ \t]+")


def html_to_text(s: Optional[str]) -> str:
    if not s:
        return ""
    s = _BLOCK_BREAK.sub("\n", s)
    s = _TAG.sub("", s)
    s = html.unescape(s)
    s = _SPACES.sub(" ", s)
    s = _BLANK_LINES.sub("\n", s)
    return s.strip()


# --------------------------------------------------------------------------
# HTML tables embedded in a description (D&D Beyond compendium tables, e.g.
# a cantrip-upgrade or class-feature-by-level table) -- stripped naively,
# these collapse into an unreadable run of numbers and short phrases. Pull
# them out and rebuild them as real tables instead.
# --------------------------------------------------------------------------

_TABLE_RE = re.compile(r"<table\b[^>]*>(.*?)</table>", re.IGNORECASE | re.DOTALL)
_CAPTION_RE = re.compile(r"<caption\b[^>]*>(.*?)</caption>", re.IGNORECASE | re.DOTALL)
_THEAD_RE = re.compile(r"<thead\b[^>]*>(.*?)</thead>", re.IGNORECASE | re.DOTALL)
_TBODY_RE = re.compile(r"<tbody\b[^>]*>(.*?)</tbody>", re.IGNORECASE | re.DOTALL)
_TR_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_TH_RE = re.compile(r"<th\b[^>]*>(.*?)</th>", re.IGNORECASE | re.DOTALL)
_TD_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)

RichSegment = Tuple[str, object]  # ("text", str) or ("table", (caption, headers, rows))


def _clean_cell(cell_html: str) -> str:
    text = _TAG.sub(" ", cell_html)
    text = html.unescape(text)
    return _SPACES.sub(" ", text).strip()


def _parse_table(inner_html: str) -> Tuple[Optional[str], List[str], List[List[str]]]:
    caption = None
    m = _CAPTION_RE.search(inner_html)
    if m:
        caption = _clean_cell(m.group(1)) or None

    headers: List[str] = []
    thead_m = _THEAD_RE.search(inner_html)
    if thead_m:
        for tr in _TR_RE.findall(thead_m.group(1)):
            row = [_clean_cell(h) for h in _TH_RE.findall(tr)]
            if row:
                headers = row

    tbody_m = _TBODY_RE.search(inner_html)
    if tbody_m:
        body_html = tbody_m.group(1)
    elif thead_m:
        body_html = inner_html[thead_m.end():]
    else:
        body_html = inner_html

    rows: List[List[str]] = []
    for tr in _TR_RE.findall(body_html):
        cells = _TD_RE.findall(tr) or _TH_RE.findall(tr)
        if cells:
            rows.append([_clean_cell(c) for c in cells])

    return caption, headers, rows


def split_rich_text(s: Optional[str]) -> List[RichSegment]:
    """Break a description into ordered ("text", str) / ("table", data)
    segments so plain prose and embedded tables can be rendered
    differently."""
    s = s or ""
    segments: List[RichSegment] = []
    pos = 0
    for m in _TABLE_RE.finditer(s):
        text = html_to_text(s[pos:m.start()])
        if text:
            segments.append(("text", text))
        segments.append(("table", _parse_table(m.group(1))))
        pos = m.end()
    tail = html_to_text(s[pos:])
    if tail:
        segments.append(("text", tail))
    return segments


# --------------------------------------------------------------------------
# Low-level PDF writer
# --------------------------------------------------------------------------


def _pdf_escape(s: str) -> str:
    return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


class Canvas:
    """Accumulates drawing ops for one page. Y grows downward from the top."""

    def __init__(self) -> None:
        self.ops: List[str] = ["1 w"]

    def _fy(self, y: float) -> float:
        return PAGE_H - y

    def set_gray(self, g: float) -> None:
        self.ops.append(f"{g:.2f} g")

    def line_width(self, w: float) -> None:
        self.ops.append(f"{w:.2f} w")

    def hline(self, x1: float, y: float, x2: float, gray: float = 0.0) -> None:
        self.set_gray(gray)
        fy = self._fy(y)
        self.ops.append(f"{x1:.2f} {fy:.2f} m {x2:.2f} {fy:.2f} l S")
        self.set_gray(0.0)

    def vline(self, x: float, y1: float, y2: float, gray: float = 0.0) -> None:
        self.set_gray(gray)
        self.ops.append(f"{x:.2f} {self._fy(y1):.2f} m {x:.2f} {self._fy(y2):.2f} l S")
        self.set_gray(0.0)

    def rect(self, x: float, y: float, w: float, h: float, gray: float = 0.0) -> None:
        self.set_gray(gray)
        self.ops.append(f"{x:.2f} {self._fy(y) - h:.2f} {w:.2f} {h:.2f} re S")
        self.set_gray(0.0)

    def circle(self, cx: float, cy: float, r: float) -> None:
        # 4-bezier approximation of a circle
        k = 0.5523 * r
        fy = self._fy(cy)
        self.ops.append(
            f"{cx - r:.2f} {fy:.2f} m "
            f"{cx - r:.2f} {fy + k:.2f} {cx - k:.2f} {fy + r:.2f} {cx:.2f} {fy + r:.2f} c "
            f"{cx + k:.2f} {fy + r:.2f} {cx + r:.2f} {fy + k:.2f} {cx + r:.2f} {fy:.2f} c "
            f"{cx + r:.2f} {fy - k:.2f} {cx + k:.2f} {fy - r:.2f} {cx:.2f} {fy - r:.2f} c "
            f"{cx - k:.2f} {fy - r:.2f} {cx - r:.2f} {fy - k:.2f} {cx - r:.2f} {fy:.2f} c S"
        )

    def text(self, x: float, y: float, s: str, font: str = "F1", size: float = 9.0,
             gray: float = 0.0) -> None:
        if not s:
            return
        esc = _pdf_escape(s)
        self.set_gray(gray)
        self.ops.append(f"BT /{font} {size:.2f} Tf {x:.2f} {self._fy(y):.2f} Td ({esc}) Tj ET")
        self.set_gray(0.0)

    def text_centered(self, cx: float, y: float, s: str, font: str = "F1", size: float = 9.0,
                       gray: float = 0.0) -> None:
        w = text_width(s, font, size)
        self.text(cx - w / 2, y, s, font, size, gray)

    def text_right(self, rx: float, y: float, s: str, font: str = "F1", size: float = 9.0,
                    gray: float = 0.0) -> None:
        w = text_width(s, font, size)
        self.text(rx - w, y, s, font, size, gray)

    def stream_bytes(self) -> bytes:
        return "\n".join(self.ops).encode("cp1252", "replace")


class PDFDocument:
    def __init__(self) -> None:
        self.pages: List[Canvas] = []

    def new_page(self) -> Canvas:
        cv = Canvas()
        self.pages.append(cv)
        return cv

    def write(self, path: str) -> None:
        objects: dict[int, bytes] = {}

        objects[1] = b"<< /Type /Catalog /Pages 2 0 R >>"
        objects[3] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
        objects[4] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"

        kids = []
        next_id = 5
        for cv in self.pages:
            content_id = next_id
            page_id = next_id + 1
            next_id += 2
            body = cv.stream_bytes()
            objects[content_id] = (
                f"<< /Length {len(body)} >>\nstream\n".encode("ascii") + body + b"\nendstream"
            )
            objects[page_id] = (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W:g} {PAGE_H:g}] "
                f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> "
                f"/Contents {content_id} 0 R >>"
            ).encode("ascii")
            kids.append(f"{page_id} 0 R")

        objects[2] = (
            f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
        ).encode("ascii")

        out = bytearray()
        out += b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"
        offsets: dict[int, int] = {}
        max_id = max(objects)
        for obj_id in range(1, max_id + 1):
            body = objects.get(obj_id)
            if body is None:
                continue
            offsets[obj_id] = len(out)
            out += f"{obj_id} 0 obj\n".encode("ascii")
            out += body
            out += b"\nendobj\n"

        xref_offset = len(out)
        out += f"xref\n0 {max_id + 1}\n".encode("ascii")
        out += b"0000000000 65535 f \n"
        for obj_id in range(1, max_id + 1):
            off = offsets.get(obj_id, 0)
            out += f"{off:010d} 00000 n \n".encode("ascii")
        out += b"trailer\n"
        out += f"<< /Size {max_id + 1} /Root 1 0 R >>\n".encode("ascii")
        out += b"startxref\n"
        out += f"{xref_offset}\n".encode("ascii")
        out += b"%%EOF"

        with open(path, "wb") as fh:
            fh.write(out)


# --------------------------------------------------------------------------
# Flowing layout builder
# --------------------------------------------------------------------------


class SheetBuilder:
    def __init__(self) -> None:
        self.doc = PDFDocument()
        self.cv = self.doc.new_page()
        self.y = MARGIN

    def ensure(self, h: float) -> None:
        if self.y + h > BOTTOM:
            self.cv = self.doc.new_page()
            self.y = MARGIN

    def gap(self, h: float = 8.0) -> None:
        self.y += h

    def title(self, text: str) -> None:
        self.ensure(28)
        self.cv.text(MARGIN, self.y + 18, text, font="F2", size=18)
        self.y += 30

    def section(self, text: str) -> None:
        self.ensure(24)
        self.cv.text(MARGIN, self.y + 9, text.upper(), font="F2", size=10)
        self.cv.hline(MARGIN, self.y + 13, PAGE_W - MARGIN)
        self.y += 20

    def line(self, text: str, size: float = 9.0, font: str = "F1", gray: float = 0.0,
              dy: float = 13.0) -> None:
        self.ensure(dy)
        self.cv.text(MARGIN, self.y + 9, text, font=font, size=size, gray=gray)
        self.y += dy

    def paragraph(self, text: str, size: float = 8.5, font: str = "F1",
                   gray: float = 0.0, width: float = CONTENT_W, x: Optional[float] = None,
                   leading: float = 10.5) -> None:
        x0 = MARGIN if x is None else x
        for para in (text or "").split("\n"):
            para = para.strip()
            if not para:
                continue  # stray blank line (D&D Beyond descriptions carry literal \r\n)
            m = _LEAD_IN_RE.match(para)
            if not m or not (1 <= len(m.group(1).split()) <= 5):
                for ln in wrap(para, font, size, width):
                    self.ensure(leading)
                    self.cv.text(x0, self.y + 8, ln, font=font, size=size, gray=gray)
                    self.y += leading
                continue

            lead = f"{m.group(1)}."
            lead_w = text_width(lead, "F2", size)
            first_w = max(20.0, width - lead_w - 4)
            lines = _wrap_with_lead(m.group(3), font, size, width, first_w)

            self.ensure(leading)
            self.cv.text(x0, self.y + 8, lead, font="F2", size=size, gray=gray)
            if lines:
                self.cv.text(x0 + lead_w + 4, self.y + 8, lines[0], font=font, size=size, gray=gray)
            self.y += leading
            for ln in lines[1:]:
                self.ensure(leading)
                self.cv.text(x0, self.y + 8, ln, font=font, size=size, gray=gray)
                self.y += leading

    def table(self, caption: Optional[str], headers: List[str], rows: List[List[str]],
               font_size: float = 7.5, header_size: float = 7.5, leading: float = 9.0,
               pad: float = 4.0) -> None:
        """A bordered grid table, column widths sized to content and scaled
        to fit the page width, with wrapped multi-line cells."""
        ncols = max(len(headers), max((len(r) for r in rows), default=0), 1)
        headers = (list(headers) + [""] * ncols)[:ncols]
        rows = [(list(r) + [""] * ncols)[:ncols] for r in rows]

        natural = []
        for ci in range(ncols):
            w = text_width(headers[ci], "F2", header_size)
            for r in rows:
                w = max(w, text_width(r[ci], "F1", font_size))
            natural.append(max(w, 30.0) + 2 * pad)
        scale = CONTENT_W / sum(natural)
        col_w = [w * scale for w in natural]

        def wrapped(cells: List[str], font: str, size: float) -> List[List[str]]:
            return [wrap(c, font, size, col_w[i] - 2 * pad) for i, c in enumerate(cells)]

        if caption:
            self.ensure(leading + 6)
            self.cv.text(MARGIN, self.y + 9, caption, font="F2", size=8.5)
            self.y += leading + 6

        header_lines = wrapped(headers, "F2", header_size)
        header_h = max(len(ls) for ls in header_lines) * leading + 2 * pad
        body_lines = [wrapped(r, "F1", font_size) for r in rows]
        row_heights = [max(len(ls) for ls in lines) * leading + 2 * pad for lines in body_lines]

        self.ensure(header_h + sum(row_heights) + 6)
        x0, y = MARGIN, self.y

        x = x0
        for i in range(ncols):
            self.cv.rect(x, y, col_w[i], header_h)
            for li, ln in enumerate(header_lines[i]):
                self.cv.text(x + pad, y + pad + 7 + li * leading, ln, font="F2", size=header_size)
            x += col_w[i]
        y += header_h

        for ri, lines in enumerate(body_lines):
            x = x0
            rh = row_heights[ri]
            for i in range(ncols):
                self.cv.rect(x, y, col_w[i], rh)
                for li, ln in enumerate(lines[i]):
                    self.cv.text(x + pad, y + pad + 7 + li * leading, ln, font="F1", size=font_size)
                x += col_w[i]
            y += rh

        self.y = y + 6

    def rich_text(self, html_text: Optional[str], size: float = 7.5, gray: float = 0.25,
                   leading: float = 9.5) -> None:
        """Render a description's prose and any embedded compendium tables,
        in order."""
        for kind, payload in split_rich_text(html_text):
            if kind == "text":
                self.paragraph(payload, size=size, gray=gray, leading=leading)
                self.gap(2)
            else:
                caption, headers, rows = payload
                if headers or rows:
                    self.table(caption, headers, rows)

    def blank_lines(self, count: int, dy: float = 16.0, label: Optional[str] = None) -> None:
        if label:
            self.line(label, font="F2", size=8, gray=0.35, dy=11)
        for _ in range(count):
            self.ensure(dy)
            self.cv.hline(MARGIN, self.y + 12, PAGE_W - MARGIN, gray=0.55)
            self.y += dy

    def blank_box(self, x: float, y: float, w: float, h: float, label: str,
                   small_note: Optional[str] = None, label_size: float = 8.0) -> None:
        self.cv.rect(x, y, w, h)
        self.cv.text_centered(x + w / 2, y + 11, label, font="F2", size=label_size)
        if small_note:
            self.cv.text_centered(x + w / 2, y + h - 4, small_note, font="F1", size=6, gray=0.5)

    def footer(self, text: str) -> None:
        self.cv.text(MARGIN, PAGE_H - 16, text, font="F1", size=7, gray=0.5)


# --------------------------------------------------------------------------
# Reference material: short skill / ability reminders (no PHB needed)
# --------------------------------------------------------------------------

SKILL_BLURB = {
    "acrobatics": "Balance, tumble, stay on your feet",
    "animal handling": "Calm, control, or read an animal",
    "arcana": "Recall lore on magic, planes, spells",
    "athletics": "Climb, jump, swim, grapple, shove",
    "deception": "Convincingly hide the truth",
    "history": "Recall lore on past events and people",
    "insight": "Read intentions, spot a lie",
    "intimidation": "Influence someone through threats",
    "investigation": "Deduce facts, find hidden details",
    "medicine": "Stabilize the dying, diagnose ailments",
    "nature": "Recall lore on terrain, plants, weather",
    "perception": "Notice, spot, or hear something",
    "performance": "Entertain an audience",
    "persuasion": "Influence someone with tact and charm",
    "religion": "Recall lore on gods, rites, symbols",
    "sleight of hand": "Pick a pocket, palm an object, tricks",
    "stealth": "Hide, move silently",
    "survival": "Track, forage, navigate the wild",
}

SKILL_SHORT = {
    "animal handling": "An. Handling",
    "sleight of hand": "Slt of Hand",
}

CURRENCIES_ORDER = ["cp", "sp", "ep", "gp", "pp"]
CURRENCY_NAMES = {"cp": "Copper", "sp": "Silver", "ep": "Electrum", "gp": "Gold", "pp": "Platinum"}
CURRENCY_TO_GP = {"cp": 0.01, "sp": 0.1, "ep": 0.5, "gp": 1.0, "pp": 10.0}


# --------------------------------------------------------------------------
# Page builders
# --------------------------------------------------------------------------


def _build_header(b: SheetBuilder, c: Character) -> None:
    top_y = b.y
    b.title(c.name)

    # Inspiration sits in the top corner of the sheet, like the corner box
    # on the official form, rather than buried lower down the page. Death
    # Saves lives right underneath it, out of the way of the combat row.
    box = 9.0
    ix = PAGE_W - MARGIN - 95
    iy = top_y + 6
    b.cv.rect(ix, iy, box, box)
    if c.inspiration:
        b.cv.text(ix + 1.5, iy + box - 1.3, "X", font="F2", size=8)
    b.cv.text(ix + box + 5, iy + box - 1, "Inspiration", font="F2", size=8.5)

    dy = iy + box + 7
    b.cv.text(ix, dy + 6, "Death Saves", font="F2", size=7.5)
    dy += 11
    ds_box, ds_gap = 6.0, 8.5
    label_w = 26.0
    for row_label, row_dy in (("Succ.", 0.0), ("Fail", 11.0)):
        ry = dy + row_dy
        b.cv.text(ix, ry + 6, row_label, font="F1", size=6.5)
        for i in range(3):
            b.cv.circle(ix + label_w + i * ds_gap + ds_box / 2, ry + 3, ds_box / 2)

    classes = " / ".join(str(x) for x in c.classes)
    bits = [classes or "Class ______", f"Level {c.level}"]
    b.line("   ".join(bits), font="F2", size=10.5, dy=15)
    b.line(
        f"Race: {c.race or '_______________'}    "
        f"Size: {c.size or '_______________'}    "
        f"Background: {c.background or '_______________'}    "
        f"Alignment: {c.alignment or '_______________'}",
        dy=13,
    )
    b.gap(6)


def _build_ability_columns(b: SheetBuilder, c: Character) -> None:
    """One column per ability: score box, its saving throw, then the skills
    it governs — stacked together so each ability's letters (STR, DEX, ...)
    appear once instead of being repeated across three separate sections."""
    b.section("Ability Scores, Saving Throws & Skills")
    col_gap = 6.0
    col_w = (CONTENT_W - col_gap * 5) / 6
    score_box_h = 38.0
    header_h = 14.0
    save_row_h = 14.0
    save_sep_gap = 6.0
    row_gap = 3.0
    blurb_size = 6.2
    blurb_leading = 7.4

    by_ability = {a: sorted(n for n, ab in SKILLS.items() if ab == a) for a in ABILITIES}
    skill_size = 6.8
    skill_leading = 9.0

    def skill_lines(name: str, s) -> List[str]:
        label = SKILL_SHORT.get(name, name.title())
        text = f"{label} {fmt(s.modifier)}"
        return wrap(text, "F1", skill_size, col_w - 2)

    def column_height(ability: str) -> float:
        h = header_h + score_box_h + 6.0 + save_row_h + save_sep_gap
        for name in by_ability[ability]:
            s_lines = skill_lines(name, c.skills[name])
            blurb = wrap(SKILL_BLURB.get(name, ""), "F1", blurb_size, col_w - 2)[:2]
            h += len(s_lines) * skill_leading + len(blurb) * blurb_leading + row_gap
        return h

    total_h = max(column_height(a) for a in ABILITIES)
    b.ensure(total_h + 8)

    y0 = b.y
    max_bottom = y0
    for i, ability in enumerate(ABILITIES):
        x = MARGIN + i * (col_w + col_gap)
        y = y0
        ab = c.abilities[ability]

        b.cv.text_centered(x + col_w / 2, y + 10, ABBREV[ability], font="F2", size=11)
        y += header_h

        b.cv.rect(x, y, col_w, score_box_h)
        b.cv.text_centered(x + col_w / 2, y + 20, fmt(ab.modifier), font="F1", size=17)
        b.cv.text_centered(x + col_w / 2, y + 33, str(ab.score), font="F1", size=9, gray=0.35)
        y += score_box_h + 6.0

        b.cv.text(x, y + 10, f"Save {fmt(ab.save)}", font="F2", size=9.5)
        y += save_row_h
        b.cv.hline(x, y, x + col_w, gray=0.4)
        y += save_sep_gap

        for name in by_ability[ability]:
            s = c.skills[name]
            for ln in skill_lines(name, s):
                b.cv.text(x, y + 7.0, ln, font="F1", size=skill_size)
                y += skill_leading

            for ln in wrap(SKILL_BLURB.get(name, ""), "F1", blurb_size, col_w - 2)[:2]:
                b.cv.text(x, y + 6.2, ln, font="F1", size=blurb_size, gray=0.42)
                y += blurb_leading
            y += row_gap

        max_bottom = max(max_bottom, y)

    b.y = max_bottom + 6


def _build_passives_and_profs(b: SheetBuilder, c: Character) -> None:
    b.section("Passive Scores & Senses")
    b.line(
        f"Passive Perception {c.passive_perception}    "
        f"Passive Investigation {c.passive_investigation}    "
        f"Passive Insight {c.passive_insight}",
        font="F2",
        size=9.5,
    )
    if c.senses:
        b.line("Senses: " + ", ".join(f"{titleize(k)} {v} ft." for k, v in c.senses.items()))
    b.gap(4)

    b.section("Other Proficiencies & Languages")
    for label, vals in (
        ("Armor", c.armor_proficiencies),
        ("Weapons", c.weapon_proficiencies),
        ("Tools", c.tool_proficiencies),
        ("Languages", c.languages),
    ):
        text = f"{label}: " + (", ".join(vals) if vals else "—")
        b.paragraph(text, size=8.5)
    b.gap(4)


def _build_combat(b: SheetBuilder, c: Character) -> None:
    b.section("Combat")
    speed_text = ", ".join(f"{k} {v} ft." for k, v in c.speeds.items()) or "30 ft."

    # AC, Proficiency Bonus, Speed, and Initiative each get a box; Temp/
    # Current/Max HP share one wider box (in that order) split by two
    # dividers. Current/Temp HP are always left blank for pencil.
    gap = 8.0
    weights = [1.0, 1.0, 1.0, 1.0, 2.2]  # AC, Prof, Speed, Init, HP block
    unit = (CONTENT_W - gap * (len(weights) - 1)) / sum(weights)
    box_w = unit
    hp_w = unit * weights[-1]
    h = 50
    b.ensure(h + 20)
    y = b.y
    x = MARGIN

    def stat_box(label: str, value, label_size: float = 6.5) -> None:
        nonlocal x
        b.blank_box(x, y, box_w, h, label, label_size=label_size)
        size = fit_size(str(value), box_w - 8, start=15.0, min_size=8.0)
        b.cv.text_centered(x + box_w / 2, y + 34, str(value), font="F1", size=size)
        x += box_w + gap

    stat_box("AC", c.armor_class)
    stat_box("PROF. BONUS", fmt(c.proficiency_bonus))
    stat_box("SPEED", speed_text)
    stat_box("INITIATIVE", fmt(c.initiative))

    # HP block: Temp HP, Current HP, Max HP, left to right, in one box.
    b.cv.rect(x, y, hp_w, h)
    third = hp_w / 3
    b.cv.vline(x + third, y, y + h)
    b.cv.vline(x + 2 * third, y, y + h)
    b.cv.text_centered(x + third / 2, y + 11, "TEMP HP", font="F2", size=5.6)
    b.cv.text_centered(x + third + third / 2, y + 11, "CURRENT HP", font="F2", size=5.6)
    b.cv.text_centered(x + 2 * third + third / 2, y + 11, "MAX HP", font="F2", size=5.6)
    max_size = fit_size(str(c.max_hit_points), third - 6, start=14.0, min_size=7.0)
    b.cv.text_centered(x + 2 * third + third / 2, y + 34, str(c.max_hit_points), font="F1",
                        size=max_size)

    b.y = y + h + 8
    b.line(f"AC source: {c.armor_class_source}", size=7.5, gray=0.4, dy=12)

    hit_dice_desc = ", ".join(f"{n}{die}" for die, n in c.hit_dice.items())
    b.line(f"Hit Dice: {hit_dice_desc}", font="F2", size=9, dy=14)

    # One checkbox per total Hit Die, so the player can track dice spent
    # rather than a blank space that has to be kept up to date by hand.
    total_dice = sum(c.hit_dice.values())
    if total_dice:
        dbox, dgap = 8.0, 5.0
        per_row = max(1, int(CONTENT_W // (dbox + dgap)))
        rows_needed = -(-total_dice // per_row)
        b.ensure(rows_needed * (dbox + dgap) + 4)
        y = b.y
        x = MARGIN
        for i in range(total_dice):
            if i and i % per_row == 0:
                x = MARGIN
                y += dbox + dgap
            b.cv.rect(x, y, dbox, dbox)
            x += dbox + dgap
        b.y = y + dbox + dgap

    b.paragraph(
        "Short Rest: spend any of the Hit Dice above; for each one spent, roll it and "
        "add your Constitution modifier to regain that many hit points. "
        "Long Rest: regain all lost hit points, and regain a number of spent Hit Dice "
        "equal to half your total (minimum one).",
        size=7,
        gray=0.4,
        leading=9,
    )
    b.gap(2)

    if c.conditions:
        b.line("Conditions: " + ", ".join(c.conditions), size=8.5)
    b.gap(4)


def _cantrip_damage(s, char_level: int) -> str:
    """Damage dice + type for a cantrip at the character's current level,
    applying whichever cantrip-upgrade tier the level has reached. Cantrips
    with no damage (Guidance, Message, ...) show a dash instead."""
    if not s.damage_base_dice:
        return "—"
    dice = s.damage_base_dice
    for lvl, tier_dice in s.damage_scaling:
        if char_level >= lvl:
            dice = tier_dice
    return f"{dice} {s.damage_type}".strip() if s.damage_type else dice


def _build_attacks(b: SheetBuilder, c: Character) -> None:
    b.section("Attacks & Spellcasting")
    b.ensure(13)
    cols = [(MARGIN, "NAME"), (MARGIN + 200, "ATK BONUS"), (MARGIN + 270, "DAMAGE / TYPE"),
            (MARGIN + 400, "NOTES")]
    for x, label in cols:
        b.cv.text(x, b.y + 8, label, font="F2", size=7.5, gray=0.4)
    b.y += 11
    b.cv.hline(MARGIN, b.y, PAGE_W - MARGIN, gray=0.6)
    b.y += 4

    for a in c.attacks:
        b.ensure(13)
        b.cv.text(MARGIN, b.y + 9, a.name, font="F1", size=8.5)
        b.cv.text(MARGIN + 200, b.y + 9, fmt(a.attack_bonus), font="F1", size=8.5)
        dmg = f"{a.damage} {a.damage_type or ''}".strip()
        b.cv.text(MARGIN + 270, b.y + 9, dmg, font="F1", size=8.5)
        # The weapon's own formal properties (Light, Heavy, Thrown, ...)
        # already include any known mastery (e.g. Cleave, Vex) -- no need
        # to separately spell out which ability it uses or its range/reach.
        props_text = ", ".join(a.properties)
        b.cv.text(MARGIN + 400, b.y + 9, props_text[:40], font="F1", size=7.5, gray=0.4)
        b.y += 13

    # Every known cantrip counts as an at-will "attack" at the table, so it
    # gets a row too — with the caster's spell attack bonus and save DC,
    # since a cantrip might use either.
    cantrips = sorted((s for s in c.spells if s.level == 0), key=lambda s: s.name)
    if cantrips:
        caster = next((cl for cl in c.classes if cl.spellcasting_ability), None)
        atk_bonus_text, dc = "—", None
        if caster:
            ab = c.abilities[caster.spellcasting_ability]
            atk_bonus_text = fmt(c.proficiency_bonus + ab.modifier)
            dc = 8 + c.proficiency_bonus + ab.modifier
        for s in cantrips:
            b.ensure(13)
            b.cv.text(MARGIN, b.y + 9, s.name, font="F1", size=8.5)

            # This slot holds an attack bonus for attack-roll cantrips, or
            # the save DC and the ability the target rolls for save-based
            # ones (e.g. Frostbite -> "CON 14") -- whichever the cantrip's
            # own rules text calls for.
            if s.check_type == "save" and dc is not None:
                check_text = f"{ABBREV[s.save_ability]} {dc}"
            elif s.check_type == "attack":
                check_text = atk_bonus_text
            else:
                check_text = "—"
            b.cv.text(MARGIN + 200, b.y + 9, check_text, font="F1", size=8.5)

            b.cv.text(MARGIN + 270, b.y + 9, _cantrip_damage(s, c.level), font="F1", size=8.5)
            b.cv.text(MARGIN + 400, b.y + 9, s.range_text or "", font="F1", size=7.5, gray=0.4)
            b.y += 13

    # Room to pencil in additional attacks.
    for _ in range(3):
        b.ensure(13)
        b.cv.hline(MARGIN, b.y + 11, PAGE_W - MARGIN, gray=0.55)
        b.y += 13
    b.gap(4)


_REST_SUFFIX = {"short": "/short rest", "long": "/long rest", "long_plus_short": "/long rest*"}


def _build_feature_summary(b: SheetBuilder, c: Character) -> None:
    """Name-only roster of every trait/feature/feat, right under Attacks &
    Spellcasting -- full descriptions live later, near the Spell List.
    Anything with a tracked usage limit gets a checkbox per use, and those
    entries are sorted to the top so they're easy to find mid-combat.
    FILTER_SUMMARY entries (see ddb_character.py) are left off entirely --
    they're passive descriptors either shown elsewhere on the sheet or not
    actionable at the table -- but still appear in the full list below."""
    items = list(c.species_traits) + list(c.class_features) + list(c.feats)
    items = [f for f in items if not in_filter_summary(f.name)]
    if not items:
        return

    items = sorted(items, key=lambda f: f.max_uses is None)  # restricted first

    b.section("Traits & Feats")
    box, gap = 7.0, 4.0
    col_gap = 16.0
    col_w = (CONTENT_W - col_gap) / 2
    col_x = [MARGIN, MARGIN + col_w + col_gap]
    row_h = 13.0
    needs_footnote = False

    half = -(-len(items) // 2)
    columns = [items[:half], items[half:]]
    b.ensure(half * row_h + 4)
    y0 = b.y

    for ci, col_items in enumerate(columns):
        x0 = col_x[ci]
        y = y0
        for feat in col_items:
            b.cv.text(x0, y + 9, feat.name, font="F1", size=8.5)
            if feat.max_uses:
                x = x0 + text_width(feat.name, "F1", 8.5) + 10
                for _ in range(feat.max_uses):
                    b.cv.rect(x, y + 1, box, box)
                    x += box + gap
                suffix = _REST_SUFFIX[feat.rest_type]
                if feat.rest_type == "long_plus_short":
                    needs_footnote = True
                b.cv.text(x + 3, y + 9, suffix, font="F1", size=7, gray=0.4)
            y += row_h

    b.y = y0 + half * row_h

    if needs_footnote:
        b.line("* One use is also regained after a short rest.", size=7, gray=0.45, dy=10)
    b.gap(4)


def _build_currency(b: SheetBuilder, c: Character) -> None:
    b.section("Currency")
    w = (CONTENT_W - 4 * 8) / 5
    h = 48
    b.ensure(h + 8)
    x = MARGIN
    for code in CURRENCIES_ORDER:
        known = c.currencies.get(code, 0)
        rate = CURRENCY_TO_GP[code]
        b.cv.rect(x, b.y, w, h)
        b.cv.text_centered(x + w / 2, b.y + 10, f"{CURRENCY_NAMES[code]} ({code.upper()})",
                            font="F2", size=7)
        b.cv.text_centered(x + w / 2, b.y + 19, f"1 {code} = {rate:g} gp", font="F1", size=5.5,
                            gray=0.5)
        b.cv.hline(x + 8, b.y + 36, x + w - 8, gray=0.55)
        note = f"at snapshot: {known}"
        b.cv.text_centered(x + w / 2, b.y + h - 4, note, font="F1", size=6, gray=0.5)
        x += w + 8
    b.y += h + 8
    b.line("(Blank line above each box is for your current total — the small number "
           "underneath is what you had when this sheet was made.)", size=7, gray=0.45, dy=11)
    b.gap(4)


_MAX_ITEM_CHECKS = 24  # cap for stacks like arrows so a row can't run away


def _build_inventory(b: SheetBuilder, c: Character) -> None:
    b.section("Equipment")

    col_gap = 16.0
    col_w = (CONTENT_W - col_gap) / 2
    cbox, cgap = 6.0, 3.0
    per_row = max(1, int(col_w // (cbox + cgap)))
    name_size = 8.0
    note_size, note_gray = 6.5, 0.45

    def note_of(item) -> str:
        # Base (un-renamed) name first, then equip flags, then unit weight
        # last -- all in the same smaller/lighter run that follows the name.
        bits = []
        if item.base_name:
            bits.append(item.base_name)
        if item.equipped:
            bits.append("equipped")
        if item.attuned:
            bits.append("attuned")
        if item.magic:
            bits.append("magic")
        bits.append(f"{item.weight:g} lb")
        return ", ".join(bits)

    def row_height(item) -> float:
        # A single copy of an item gets no checkboxes at all — nothing to
        # track. Only stacks (quantity > 1) get one checkbox per unit.
        if item.quantity <= 1:
            return 11 + 4
        n = min(item.quantity, _MAX_ITEM_CHECKS)
        rows = max(1, -(-n // per_row))
        return 11 + rows * (cbox + cgap) + 4

    items = c.inventory
    half = -(-len(items) // 2)
    columns = [items[:half], items[half:]]
    col_x = [MARGIN, MARGIN + col_w + col_gap]

    total_h = max((sum(row_height(i) for i in col) for col in columns), default=0)
    b.ensure(total_h + 4)
    y0 = b.y
    col_bottoms = []

    for ci, col_items in enumerate(columns):
        x = col_x[ci]
        y = y0
        for item in col_items:
            qty = f" x{item.quantity}" if item.quantity > 1 else ""
            name = f"{item.name}{qty}"[:40]
            b.cv.text(x, y + 9, name, font="F1", size=name_size)

            note = note_of(item)
            if note:
                nx = x + text_width(name, "F1", name_size) + 4
                note_lines = wrap(note, "F1", note_size, col_w - (nx - x))
                if note_lines:
                    b.cv.text(nx, y + 9, note_lines[0], font="F1", size=note_size, gray=note_gray)
            y += 11

            if item.quantity > 1:
                n = min(item.quantity, _MAX_ITEM_CHECKS)
                cx = x
                for k in range(n):
                    if k and k % per_row == 0:
                        cx = x
                        y += cbox + cgap
                    b.cv.rect(cx, y, cbox, cbox)
                    cx += cbox + cgap
                if item.quantity > _MAX_ITEM_CHECKS:
                    b.cv.text(cx + 3, y + cbox - 1, f"+{item.quantity - _MAX_ITEM_CHECKS} more",
                               font="F1", size=6, gray=0.4)
                y += cbox + cgap
            y += 4
        col_bottoms.append(y)

    b.y = max(col_bottoms) if items else y0

    total_weight = sum(i.weight * i.quantity for i in c.inventory)
    b.line(f"Carried weight: {total_weight:g} lb", size=7.5, gray=0.4, dy=12)

    # Pad with ruled blank lines so at least half the equipment section is
    # empty and ready for the player to fill in during play.
    blank_count = max(len(items), 6)
    b.blank_lines(blank_count, dy=15, label="(space for loot found during play)")
    b.gap(4)


def _build_feat_list(b: SheetBuilder, feats: List) -> None:
    for feat in feats:
        b.ensure(11)
        b.cv.text(MARGIN, b.y + 9, feat.name, font="F2", size=8.5)
        b.y += 12
        if in_filter_description(feat.name):
            pass  # FILTER_DESCRIPTION: name only, description withheld
        elif feat.description:
            b.rich_text(feat.description, size=7.5, gray=0.25, leading=9.5)
        else:
            b.line("(no description in source data)", size=7, gray=0.45, dy=10)
        b.gap(3)


def _build_features(b: SheetBuilder, c: Character) -> None:
    b.section("Features & Traits")
    groups = [
        ("Feats", c.feats),
        ("Class Features", c.class_features),
        ("Species Traits", c.species_traits),
    ]
    if not any(items for _, items in groups):
        b.line("(none recorded)", size=8, gray=0.4)
        b.gap(2)
        return
    for label, items in groups:
        if not items:
            continue
        b.line(label, font="F2", size=9, dy=13)
        _build_feat_list(b, items)


def _build_appearance_and_backstory(b: SheetBuilder, c: Character) -> None:
    b.section("Character Appearance")

    def field(label: str, value, suffix: str = "") -> str:
        return f"{label}: {value}{suffix}" if value else f"{label}: " + "_" * 14

    # Portrait box: exactly a quarter of one page's usable area (half its
    # width, half its height), sitting beside the appearance fields.
    col_gap = 16.0
    box_w = CONTENT_W / 2
    box_h = (BOTTOM - MARGIN) / 2
    left_w = CONTENT_W - box_w - col_gap

    b.ensure(box_h + 8)
    y0 = b.y
    y = y0
    for text in (field("Age", c.age), field("Gender", c.gender),
                 field("Height", c.height), field("Weight", c.weight_lbs, " lb"),
                 field("Eyes", c.eyes), field("Skin", c.skin), field("Hair", c.hair),
                 field("Faith", c.faith)):
        b.cv.text(MARGIN, y + 9, text, font="F1", size=9)
        y += 14

    # Allies & Organizations lives in the narrow space left over below the
    # appearance fields, still confined to this column (beside the
    # portrait box, not spanning under it).
    y += 6
    b.cv.text(MARGIN, y + 9, "ALLIES & ORGANIZATIONS", font="F2", size=9)
    b.cv.hline(MARGIN, y + 13, MARGIN + left_w, gray=0.6)
    y += 20
    while y + 16 <= y0 + box_h:
        b.cv.hline(MARGIN, y + 12, MARGIN + left_w, gray=0.55)
        y += 16
    left_bottom = y

    box_x = MARGIN + left_w + col_gap
    b.blank_box(box_x, y0, box_w, box_h, "PORTRAIT",
                small_note="(sketch, or tape/staple a photo here)")

    b.y = max(left_bottom, y0 + box_h) + 8

    b.section("Additional Notes / Backstory")
    b.blank_lines(9)


def _spell_meta(s) -> str:
    bits = []
    if s.casting_time:
        bits.append(f"Casting Time: {s.casting_time}")
    if s.range_text:
        bits.append(f"Range: {s.range_text}")
    if s.components_text:
        bits.append(f"Components: {s.components_text}")
    if s.duration_text:
        bits.append(f"Duration: {s.duration_text}")
    return "   ".join(bits)


def _build_spell_stats(b: SheetBuilder, c: Character) -> None:
    """Spellcasting modifier, save DC, attack bonus, and slots — kept right
    under Attacks & Spellcasting rather than off with the spell list."""
    casters = [cl for cl in c.classes if cl.spellcasting_ability]
    if not casters and not c.spell_slots and not c.pact_slots:
        return

    b.section("Spellcasting")
    for cl in casters:
        ab = c.abilities[cl.spellcasting_ability]
        dc = 8 + c.proficiency_bonus + ab.modifier
        atk = c.proficiency_bonus + ab.modifier
        b.line(f"{cl.name}: {ABBREV[cl.spellcasting_ability]} modifier {fmt(ab.modifier)}   "
               f"Save DC {dc}   Attack {fmt(atk)}", font="F2", size=9)

    sbox, sgap = 8.0, 5.0

    def slot_checkboxes(label: str, count: int) -> None:
        if not count:
            return
        b.ensure(14)
        b.cv.text(MARGIN, b.y + 9, label, font="F1", size=8.5)
        x = MARGIN + text_width(label, "F1", 8.5) + 12
        for _ in range(count):
            b.cv.rect(x, b.y + 1, sbox, sbox)
            x += sbox + sgap
        b.y += 14

    for lvl in sorted(c.spell_slots):
        slot_checkboxes(f"Level {lvl} Slots:", c.spell_slots[lvl])
    if c.pact_slots:
        slot_checkboxes(f"Pact Magic (Level {c.pact_slots.get('level')}):",
                         c.pact_slots.get("slots", 0))
    b.gap(4)


def _build_spells(b: SheetBuilder, c: Character) -> None:
    if not c.spells:
        return

    by_level = {}
    for s in c.spells:
        by_level.setdefault(s.level, []).append(s)

    b.section("Spell List")
    for lvl in sorted(by_level):
        head = "Cantrips" if lvl == 0 else f"Level {lvl} Spells"
        b.section(head)
        for s in sorted(by_level[lvl], key=lambda s: s.name):
            b.ensure(16)
            mark = "[P]" if s.prepared else "[ ]"
            tag_bits = []
            if s.ritual:
                tag_bits.append("Ritual")
            if s.concentration:
                tag_bits.append("Conc.")
            tags = f"  ({', '.join(tag_bits)})" if tag_bits else ""
            header = f"{mark} {s.name}"
            if s.school:
                header += f" — {s.school}"
            header += tags
            b.cv.text(MARGIN, b.y + 9, header, font="F2", size=9)
            b.y += 13

            meta = _spell_meta(s)
            if meta:
                b.paragraph(meta, size=7, gray=0.4, leading=9.5)

            if s.description:
                b.rich_text(s.description, size=8, gray=0.0, leading=10)
            else:
                b.line("(see rulebook — no description in source data)", size=7.5, gray=0.45,
                        dy=10)
            b.gap(4)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def build_pdf(c: Character) -> PDFDocument:
    b = SheetBuilder()
    _build_header(b, c)
    _build_combat(b, c)
    _build_ability_columns(b, c)
    _build_passives_and_profs(b, c)

    b.cv = b.doc.new_page()
    b.y = MARGIN
    _build_attacks(b, c)
    _build_feature_summary(b, c)
    _build_spell_stats(b, c)

    b.cv = b.doc.new_page()
    b.y = MARGIN
    _build_inventory(b, c)
    _build_currency(b, c)

    b.cv = b.doc.new_page()
    b.y = MARGIN
    _build_appearance_and_backstory(b, c)

    b.cv = b.doc.new_page()
    b.y = MARGIN
    _build_features(b, c)
    _build_spells(b, c)

    return b.doc


def render_pdf(c: Character, path: str) -> None:
    """Render `c` as a printable, greyscale-friendly PDF character sheet."""
    doc = build_pdf(c)
    doc.write(path)
