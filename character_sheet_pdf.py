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
from typing import List, Optional

from ddb_character import (
    ABBREV,
    ABILITIES,
    Character,
    SKILLS,
    fmt,
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
        x = MARGIN if x is None else x
        for ln in wrap(text, font, size, width):
            self.ensure(leading)
            self.cv.text(x, self.y + 8, ln, font=font, size=size, gray=gray)
            self.y += leading

    def blank_lines(self, count: int, dy: float = 16.0, label: Optional[str] = None) -> None:
        if label:
            self.line(label, font="F2", size=8, gray=0.35, dy=11)
        for _ in range(count):
            self.ensure(dy)
            self.cv.hline(MARGIN, self.y + 12, PAGE_W - MARGIN, gray=0.55)
            self.y += dy

    def checkbox_row(self, checked: bool, text: str, extra: str = "", size: float = 9.0) -> None:
        self.ensure(14)
        box = 8.0
        top = self.y + 1
        self.cv.rect(MARGIN, top, box, box)
        if checked:
            self.cv.text(MARGIN + 1.2, top + box - 1, "X", font="F2", size=8)
        self.cv.text(MARGIN + box + 6, self.y + 9, text, font="F1", size=size)
        if extra:
            self.cv.text_right(PAGE_W - MARGIN, self.y + 9, extra, font="F1", size=size, gray=0.4)
        self.y += 13

    def stat_boxes(self, items: List[tuple], w: float = 84.0, h: float = 52.0) -> None:
        """items: list of (label, big_value, sub_value_or_None)"""
        self.ensure(h + 6)
        n = len(items)
        total_gap = 8.0 * (n - 1)
        box_w = min(w, (CONTENT_W - total_gap) / n)
        x = MARGIN
        for label, big, sub in items:
            self.cv.rect(x, self.y, box_w, h)
            self.cv.text_centered(x + box_w / 2, self.y + 11, label, font="F2", size=8)
            self.cv.text_centered(x + box_w / 2, self.y + 30, str(big), font="F1", size=15)
            if sub is not None:
                self.cv.text_centered(x + box_w / 2, self.y + 45, str(sub), font="F1", size=8.5,
                                       gray=0.35)
            x += box_w + 8.0
        self.y += h + 8

    def blank_box(self, x: float, y: float, w: float, h: float, label: str,
                   small_note: Optional[str] = None) -> None:
        self.cv.rect(x, y, w, h)
        self.cv.text_centered(x + w / 2, y + 11, label, font="F2", size=8)
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

ABILITY_BLURB = {
    "strength": "Melee power, carrying capacity",
    "dexterity": "Agility, reflexes, ranged attacks",
    "constitution": "Stamina, health, endurance",
    "intelligence": "Reasoning, memory, deduction",
    "wisdom": "Awareness, intuition, willpower",
    "charisma": "Force of personality, presence",
}

CURRENCIES_ORDER = ["cp", "sp", "ep", "gp", "pp"]
CURRENCY_NAMES = {"cp": "Copper", "sp": "Silver", "ep": "Electrum", "gp": "Gold", "pp": "Platinum"}


# --------------------------------------------------------------------------
# Page builders
# --------------------------------------------------------------------------


def _build_header(b: SheetBuilder, c: Character) -> None:
    b.title(c.name)
    classes = " / ".join(str(x) for x in c.classes)
    bits = [classes or "Class ______", f"Level {c.level}"]
    b.line("   ".join(bits), font="F2", size=10.5, dy=15)
    b.line(
        f"Race: {c.race or '_______________'}    "
        f"Background: {c.background or '_______________'}    "
        f"Alignment: {c.alignment or '_______________'}",
        dy=13,
    )
    b.line(f"XP: {c.xp}    Player Name: " + "_" * 28, dy=13)
    b.gap(6)


def _build_abilities(b: SheetBuilder, c: Character) -> None:
    b.section("Ability Scores")
    items = []
    for a in ABILITIES:
        ab = c.abilities[a]
        items.append((ABBREV[a], ab.score, fmt(ab.modifier)))
    b.stat_boxes(items, w=84, h=48)
    for a in ABILITIES:
        b.ensure(11)
        b.cv.text(MARGIN, b.y + 8, f"{ABBREV[a]}: {ABILITY_BLURB[a]}", font="F1", size=7.5,
                   gray=0.4)
        b.y += 10.5
    b.gap(6)
    b.checkbox_row(c.inspiration, "Inspiration", extra=f"Proficiency Bonus  {fmt(c.proficiency_bonus)}")
    b.gap(4)


def _build_saves(b: SheetBuilder, c: Character) -> None:
    b.section("Saving Throws")
    for a in ABILITIES:
        ab = c.abilities[a]
        b.checkbox_row(ab.save_proficient, f"{fmt(ab.save)}  {ABBREV[a]} Saving Throw")
    b.gap(6)


def _build_skills(b: SheetBuilder, c: Character) -> None:
    b.section("Skills")
    for name in sorted(c.skills):
        s = c.skills[name]
        b.ensure(15)
        box = 8.0
        top = b.y + 1
        b.cv.rect(MARGIN, top, box, box)
        if s.expertise:
            b.cv.text(MARGIN + 0.5, top + box - 1, "XX", font="F2", size=6.5)
        elif s.proficient:
            b.cv.text(MARGIN + 1.2, top + box - 1, "X", font="F2", size=8)
        x = MARGIN + box + 6
        b.cv.text(x, b.y + 9, fmt(s.modifier), font="F2", size=9)
        x += 26
        b.cv.text(x, b.y + 9, f"{name.title()} ({ABBREV[s.ability]})", font="F1", size=9)
        x += 145
        b.cv.text(x, b.y + 9, SKILL_BLURB.get(name, ""), font="F1", size=7.5, gray=0.4)
        b.y += 14
    b.gap(4)


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
    b.stat_boxes([
        ("ARMOR CLASS", c.armor_class, None),
        ("INITIATIVE", fmt(c.initiative), None),
        ("SPEED", speed_text, None),
    ], w=150, h=48)
    b.line(f"AC source: {c.armor_class_source}", size=7.5, gray=0.4, dy=12)
    b.gap(4)

    # HP row: max is known, current/temp are always left blank for pencil.
    hp_w = (CONTENT_W - 16) / 3
    h = 50
    b.ensure(h + 20)
    y = b.y
    b.blank_box(MARGIN, y, hp_w, h, "MAX HIT POINTS")
    b.cv.text_centered(MARGIN + hp_w / 2, y + 34, str(c.max_hit_points), font="F1", size=16)
    b.blank_box(MARGIN + hp_w + 8, y, hp_w, h, "CURRENT HIT POINTS")
    b.blank_box(MARGIN + 2 * (hp_w + 8), y, hp_w, h, "TEMPORARY HIT POINTS")
    b.y = y + h + 8

    hit_dice = ", ".join(f"{n}{die}" for die, n in c.hit_dice.items())
    b.line(f"Hit Dice: {hit_dice}    Total Hit Dice: " + "_" * 10, dy=14)

    b.ensure(16)
    b.cv.text(MARGIN, b.y + 9, "Death Saves:", font="F2", size=9)
    sx = MARGIN + 78
    for label, gx in (("Successes", 0), ("Failures", 150)):
        b.cv.text(sx + gx, b.y + 9, label, font="F1", size=8)
        for i in range(3):
            b.cv.circle(sx + gx + 62 + i * 14, b.y + 5, 4.5)
    b.y += 18

    if c.conditions:
        b.line("Conditions: " + ", ".join(c.conditions), size=8.5)
    b.gap(4)


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
        notes = a.notes
        if a.range:
            notes = f"{notes}, {a.range}" if notes else a.range
        b.cv.text(MARGIN + 400, b.y + 9, notes[:28], font="F1", size=7.5, gray=0.4)
        b.y += 13

    # Room to pencil in additional attacks / cantrip attacks.
    for _ in range(3):
        b.ensure(13)
        b.cv.hline(MARGIN, b.y + 11, PAGE_W - MARGIN, gray=0.55)
        b.y += 13
    b.gap(4)


def _build_currency(b: SheetBuilder, c: Character) -> None:
    b.section("Treasure")
    w = (CONTENT_W - 4 * 8) / 5
    h = 42
    b.ensure(h + 8)
    x = MARGIN
    for code in CURRENCIES_ORDER:
        known = c.currencies.get(code, 0)
        b.cv.rect(x, b.y, w, h)
        b.cv.text_centered(x + w / 2, b.y + 11, f"{CURRENCY_NAMES[code]} ({code.upper()})",
                            font="F2", size=7)
        b.cv.hline(x + 8, b.y + 26, x + w - 8, gray=0.55)
        note = f"at snapshot: {known}" if known else "at snapshot: 0"
        b.cv.text_centered(x + w / 2, b.y + h - 4, note, font="F1", size=6, gray=0.5)
        x += w + 8
    b.y += h + 8
    b.line("(Blank line above each box is for your current total — the small number "
           "underneath is what you had when this sheet was made.)", size=7, gray=0.45, dy=11)
    b.gap(4)


def _build_inventory(b: SheetBuilder, c: Character) -> None:
    b.section("Equipment")
    known = []
    for i in c.inventory:
        flags = "".join(["E" if i.equipped else "", "A" if i.attuned else "", "*" if i.magic else ""])
        qty = f" x{i.quantity}" if i.quantity > 1 else ""
        known.append(f"[{flags:<3}] {i.name}{qty}")

    for row in known:
        b.ensure(14)
        b.cv.text(MARGIN, b.y + 9, row, font="F1", size=8.5)
        b.y += 14

    total_weight = sum(i.weight * i.quantity for i in c.inventory)
    b.line(f"Carried weight: {total_weight:g} lb", size=7.5, gray=0.4, dy=12)

    # Pad with ruled blank lines so at least half the equipment section is
    # empty and ready for the player to fill in during play.
    blank_count = max(len(known), 6)
    b.blank_lines(blank_count, dy=15, label="(space for loot found during play)")
    b.gap(4)

    b.section("Features & Traits")
    if c.feats:
        for feat in c.feats:
            b.line(f"- {feat}", size=8.5)
    else:
        b.line("(none recorded)", size=8, gray=0.4)
    b.blank_lines(4, dy=15)


def _build_personality(b: SheetBuilder, c: Character) -> None:
    b.section("Personality Traits")
    b.blank_lines(2)
    b.section("Ideals")
    b.blank_lines(2)
    b.section("Bonds")
    b.blank_lines(2)
    b.section("Flaws")
    b.blank_lines(2)

    b.section("Character Appearance")
    def field(label: str, value, suffix: str = "") -> str:
        return f"{label}: {value}{suffix}" if value else f"{label}: " + "_" * 14
    b.line(field("Age", c.age) + "    " + field("Gender", c.gender), dy=14)
    b.line(field("Height", c.height) + "    " + field("Weight", c.weight_lbs, " lb"), dy=14)
    b.line(field("Eyes", c.eyes) + "    " + field("Skin", c.skin) + "    " + field("Hair", c.hair),
           dy=14)
    b.line(field("Faith", c.faith), dy=14)
    b.gap(4)

    b.section("Allies & Organizations")
    b.blank_lines(3)

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


def _build_spells(b: SheetBuilder, c: Character) -> None:
    if not c.spells and not c.spell_slots and not c.pact_slots:
        return

    b.section("Spellcasting")
    casters = [cl for cl in c.classes if cl.spellcasting_ability]
    if casters:
        rows = []
        for cl in casters:
            ab = c.abilities[cl.spellcasting_ability]
            dc = 8 + c.proficiency_bonus + ab.modifier
            atk = c.proficiency_bonus + ab.modifier
            rows.append(f"{cl.name}: {ABBREV[cl.spellcasting_ability]}  "
                        f"Save DC {dc}  Attack {fmt(atk)}")
        for r in rows:
            b.line(r, font="F2", size=9)
    if c.spell_slots:
        b.line("Spell Slots: " + ", ".join(f"L{lvl} x{n}" for lvl, n in sorted(c.spell_slots.items())))
    if c.pact_slots:
        b.line(f"Pact Magic: {c.pact_slots.get('slots')} slots x level {c.pact_slots.get('level')}")
    b.line("Slots Expended: " + "_" * 30, size=8.5, gray=0.4)
    b.gap(6)

    by_level = {}
    for s in c.spells:
        by_level.setdefault(s.level, []).append(s)

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

            desc = html_to_text(s.description)
            if desc:
                b.paragraph(desc, size=8, leading=10)
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
    _build_abilities(b, c)
    _build_saves(b, c)
    _build_skills(b, c)
    _build_passives_and_profs(b, c)

    b.cv = b.doc.new_page()
    b.y = MARGIN
    _build_attacks(b, c)
    _build_currency(b, c)

    b.cv = b.doc.new_page()
    b.y = MARGIN
    _build_inventory(b, c)

    b.cv = b.doc.new_page()
    b.y = MARGIN
    _build_personality(b, c)

    _build_spells(b, c)

    return b.doc


def render_pdf(c: Character, path: str) -> None:
    """Render `c` as a printable, greyscale-friendly PDF character sheet."""
    doc = build_pdf(c)
    doc.write(path)
