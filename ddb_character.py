#!/usr/bin/env python3
"""
ddb_character.py — download and parse a D&D Beyond character sheet.

Usage
-----
    python ddb_character.py https://www.dndbeyond.com/characters/167142616
    python ddb_character.py 167142616 --json
    python ddb_character.py <url> --raw saphire.json
    python ddb_character.py saphire.json          # parse a previously saved file

Auth
----
Public characters (privacy = "Public") need no credentials.

Private characters need your D&D Beyond login cookie. Grab it once:
  1. Log in to dndbeyond.com in your browser.
  2. DevTools -> Application -> Cookies -> https://www.dndbeyond.com
  3. Copy the value of the `CobaltSession` cookie.

Then either:
    export DDB_COBALT_SESSION="<value>"
or:
    python ddb_character.py <url> --cobalt "<value>"

The cookie is exchanged for a short-lived bearer token via
https://auth-service.dndbeyond.com/v1/cobalt-token, exactly like the website does.

Stdlib only — no third-party dependencies.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

__all__ = [
    "Character",
    "DDBError",
    "fetch_character_json",
    "parse_character",
    "parse_character_id",
    "load_character",
]

# --------------------------------------------------------------------------
# Endpoints & constants
# --------------------------------------------------------------------------

CHARACTER_SERVICE = "https://character-service.dndbeyond.com/character/v5/character/{id}"
COBALT_TOKEN_URL = "https://auth-service.dndbeyond.com/v1/cobalt-token"
USER_AGENT = "ddb-character-parser/1.0 (+python-urllib)"

# D&D Beyond stat ids
ABILITIES = ["strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma"]
ABILITY_BY_ID = {i + 1: name for i, name in enumerate(ABILITIES)}
ABBREV = {
    "strength": "STR",
    "dexterity": "DEX",
    "constitution": "CON",
    "intelligence": "INT",
    "wisdom": "WIS",
    "charisma": "CHA",
}

# skill -> governing ability
SKILLS: Dict[str, str] = {
    "acrobatics": "dexterity",
    "animal handling": "wisdom",
    "arcana": "intelligence",
    "athletics": "strength",
    "deception": "charisma",
    "history": "intelligence",
    "insight": "wisdom",
    "intimidation": "charisma",
    "investigation": "intelligence",
    "medicine": "wisdom",
    "nature": "intelligence",
    "perception": "wisdom",
    "performance": "charisma",
    "persuasion": "charisma",
    "religion": "intelligence",
    "sleight of hand": "dexterity",
    "stealth": "dexterity",
    "survival": "wisdom",
}

# modifier subTypes are hyphenated: "animal-handling", "sleight-of-hand", ...
SKILL_SUBTYPE = {name: name.replace(" ", "-") for name in SKILLS}

ARMOR_TYPE = {1: "Light", 2: "Medium", 3: "Heavy", 4: "Shield"}

# Sources whose "bonus: hit-points" modifiers scale with character level
# (Tough, Dwarven Toughness, Hill Dwarf, ...). Item/class bonuses are flat.
PER_LEVEL_HP_SOURCES = {"feat", "race"}


class DDBError(RuntimeError):
    """Raised when D&D Beyond refuses or fails a request."""


# --------------------------------------------------------------------------
# Networking
# --------------------------------------------------------------------------


def parse_character_id(url_or_id: str) -> int:
    """Accept a full sheet URL, a bare id, or anything containing one."""
    s = str(url_or_id).strip()
    if s.isdigit():
        return int(s)
    m = re.search(r"/characters?/(\d+)", s)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d{4,})", s)
    if m:
        return int(m.group(1))
    raise ValueError(f"Could not find a character id in {url_or_id!r}")


def _request(url: str, *, method: str = "GET", headers: Optional[dict] = None,
             timeout: int = 30) -> Tuple[int, bytes]:
    req = urllib.request.Request(url, method=method)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:  # still carries a body worth reading
        return exc.code, exc.read()


def get_bearer_token(cobalt_session: str, *, timeout: int = 30) -> str:
    """Exchange a CobaltSession cookie for a short-lived bearer token."""
    status, body = _request(
        COBALT_TOKEN_URL,
        method="POST",
        headers={"Cookie": f"CobaltSession={cobalt_session}"},
        timeout=timeout,
    )
    if status != 200:
        raise DDBError(
            f"cobalt-token exchange failed (HTTP {status}). "
            "Your CobaltSession cookie is probably expired — grab a fresh one."
        )
    try:
        token = json.loads(body).get("token")
    except json.JSONDecodeError:
        token = None
    if not token:
        raise DDBError("cobalt-token endpoint returned no token.")
    return token


def fetch_character_json(url_or_id: str, *, cobalt_session: Optional[str] = None,
                         timeout: int = 30) -> dict:
    """Download the raw `data` payload for a character.

    Tries anonymously first (works for public characters); falls back to the
    cobalt-token flow when a CobaltSession cookie is available.
    """
    char_id = parse_character_id(url_or_id)
    url = CHARACTER_SERVICE.format(id=char_id)

    status, body = _request(url, timeout=timeout)
    if status == 200:
        return _unwrap(body)

    if not cobalt_session:
        cobalt_session = os.environ.get("DDB_COBALT_SESSION") or os.environ.get("COBALT_SESSION")

    if not cobalt_session:
        raise DDBError(
            f"HTTP {status} for character {char_id}. The character is not public.\n"
            "Set DDB_COBALT_SESSION (or pass --cobalt) with your D&D Beyond "
            "`CobaltSession` cookie value and try again."
        )

    token = get_bearer_token(cobalt_session, timeout=timeout)
    status, body = _request(url, headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
    if status != 200:
        raise DDBError(
            f"HTTP {status} for character {char_id} even with credentials — "
            "the cookie may belong to a different account, or the sheet was deleted."
        )
    return _unwrap(body)


def _unwrap(body: bytes) -> dict:
    payload = json.loads(body)
    if not payload.get("success", False):
        msg = (payload.get("data") or {}).get("serverMessage") or payload.get("message")
        raise DDBError(f"D&D Beyond returned an error: {msg}")
    return payload["data"]


# --------------------------------------------------------------------------
# Modifier helpers
# --------------------------------------------------------------------------


def _iter_modifiers(data: dict) -> Iterable[Tuple[str, dict]]:
    """Yield (source, modifier) for every modifier attached to the character."""
    for source, mods in (data.get("modifiers") or {}).items():
        for mod in mods or []:
            yield source, mod


def _active_item_component_ids(data: dict) -> set:
    """Definition ids of items whose modifiers actually apply.

    D&D Beyond keeps modifiers for every item in the bag; the sheet only
    applies the ones from equipped items (and attuned, when required).
    """
    active = set()
    for item in data.get("inventory") or []:
        definition = item.get("definition") or {}
        if not item.get("equipped"):
            continue
        if definition.get("canAttune") and not item.get("isAttuned"):
            # Attunement-gated benefits are off until attuned.
            continue
        active.add(definition.get("id"))
    return active


def active_modifiers(data: dict) -> List[Tuple[str, dict]]:
    """All modifiers that are currently in effect."""
    live_items = _active_item_component_ids(data)
    out = []
    for source, mod in _iter_modifiers(data):
        if source == "item" and mod.get("componentId") not in live_items:
            continue
        out.append((source, mod))
    return out


def _value_of(mod: dict) -> int:
    for key in ("fixedValue", "value"):
        v = mod.get(key)
        if isinstance(v, (int, float)):
            return int(v)
    return 0


def _sum_bonus(mods: List[Tuple[str, dict]], *subtypes: str) -> int:
    wanted = set(subtypes)
    return sum(_value_of(m) for _, m in mods
               if m.get("type") == "bonus" and m.get("subType") in wanted)


def _has(mods: List[Tuple[str, dict]], mtype: str, subtype: str) -> bool:
    return any(m.get("type") == mtype and m.get("subType") == subtype for _, m in mods)


def _subtypes(mods: List[Tuple[str, dict]], mtype: str) -> List[str]:
    seen, out = set(), []
    for _, m in mods:
        if m.get("type") == mtype:
            st = m.get("subType")
            if st and st not in seen:
                seen.add(st)
                out.append(st)
    return out


def ability_modifier(score: int) -> int:
    return math.floor((score - 10) / 2)


def fmt(n: int) -> str:
    return f"+{n}" if n >= 0 else str(n)


def titleize(subtype: str) -> str:
    return subtype.replace("-", " ").title()


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass
class Ability:
    name: str
    score: int
    modifier: int
    save: int
    save_proficient: bool


@dataclass
class Skill:
    name: str
    ability: str
    modifier: int
    proficient: bool
    expertise: bool


@dataclass
class CharClass:
    name: str
    level: int
    subclass: Optional[str]
    hit_die: int
    is_starting: bool
    spellcasting_ability: Optional[str] = None

    def __str__(self) -> str:
        sub = f" ({self.subclass})" if self.subclass else ""
        return f"{self.name}{sub} {self.level}"


@dataclass
class Item:
    name: str
    quantity: int
    equipped: bool
    attuned: bool
    weight: float
    kind: Optional[str]
    rarity: Optional[str]
    magic: bool
    cost: Optional[float] = None


@dataclass
class Attack:
    name: str
    attack_bonus: int
    damage: str
    damage_type: Optional[str]
    range: Optional[str]
    properties: List[str] = field(default_factory=list)
    proficient: bool = True
    notes: str = ""


@dataclass
class Spell:
    name: str
    level: int
    school: Optional[str]
    prepared: bool
    source: str
    ritual: bool = False
    concentration: bool = False
    description: Optional[str] = None
    casting_time: Optional[str] = None
    range_text: Optional[str] = None
    duration_text: Optional[str] = None
    components_text: Optional[str] = None
    damage_type: Optional[str] = None
    damage_base_dice: Optional[str] = None
    damage_scaling: List[Tuple[int, str]] = field(default_factory=list)


@dataclass
class Feat:
    name: str
    description: Optional[str] = None


@dataclass
class Character:
    # identity
    id: int
    name: str
    url: str
    username: Optional[str]
    race: Optional[str]
    background: Optional[str]
    classes: List[CharClass]
    level: int
    xp: int
    inspiration: bool

    # core numbers
    proficiency_bonus: int
    abilities: Dict[str, Ability]
    armor_class: int
    armor_class_source: str
    initiative: int
    max_hit_points: int
    current_hit_points: int
    temp_hit_points: int
    hit_dice: Dict[str, int]
    speeds: Dict[str, int]

    # trained things
    skills: Dict[str, Skill]
    passive_perception: int
    passive_investigation: int
    passive_insight: int
    senses: Dict[str, int]
    armor_proficiencies: List[str]
    weapon_proficiencies: List[str]
    tool_proficiencies: List[str]
    languages: List[str]

    # content
    attacks: List[Attack]
    inventory: List[Item]
    spells: List[Spell]
    spell_slots: Dict[int, int]
    pact_slots: Dict[str, int]
    feats: List[Feat]
    conditions: List[str]
    currencies: Dict[str, int]

    # bio
    alignment: Optional[str] = None
    age: Optional[int] = None
    gender: Optional[str] = None
    height: Optional[str] = None
    weight_lbs: Optional[str] = None
    eyes: Optional[str] = None
    hair: Optional[str] = None
    skin: Optional[str] = None
    faith: Optional[str] = None

    raw: dict = field(default_factory=dict, repr=False)

    # ---- convenience -----------------------------------------------------

    def ability(self, name: str) -> Ability:
        return self.abilities[name.lower()]

    def mod(self, name: str) -> int:
        return self.abilities[name.lower()].modifier

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d.pop("raw", None)
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def summary(self) -> str:
        return render_sheet(self)

    def __str__(self) -> str:  # pragma: no cover
        classes = " / ".join(str(c) for c in self.classes)
        return f"{self.name} — {self.race or '?'} {classes}"


ALIGNMENTS = {
    1: "Lawful Good", 2: "Neutral Good", 3: "Chaotic Good",
    4: "Lawful Neutral", 5: "Neutral", 6: "Chaotic Neutral",
    7: "Lawful Evil", 8: "Neutral Evil", 9: "Chaotic Evil",
}


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def parse_character(data: dict) -> Character:
    """Turn the raw character-service payload into a Character."""
    mods = active_modifiers(data)

    classes = _parse_classes(data)
    total_level = sum(c.level for c in classes) or 1
    prof = 1 + math.ceil(total_level / 4)

    abilities = _parse_abilities(data, mods, prof)
    skills = _parse_skills(abilities, mods, prof)

    ac, ac_source = _compute_ac(data, abilities, mods)
    max_hp = _compute_max_hp(data, abilities, mods, total_level)
    removed = data.get("removedHitPoints") or 0

    speeds = _parse_speeds(data, mods)
    senses = _parse_senses(data, mods)

    perception = skills["perception"].modifier + _sum_bonus(mods, "passive-perception")
    investigation = skills["investigation"].modifier + _sum_bonus(mods, "passive-investigation")
    insight = skills["insight"].modifier + _sum_bonus(mods, "passive-insight")

    return Character(
        id=data.get("id"),
        name=data.get("name") or "Unnamed",
        url=f"https://www.dndbeyond.com/characters/{data.get('id')}",
        username=data.get("username"),
        race=(data.get("race") or {}).get("fullName") or (data.get("race") or {}).get("baseName"),
        background=((data.get("background") or {}).get("definition") or {}).get("name"),
        classes=classes,
        level=total_level,
        xp=(data.get("currentXp") or 0) + (data.get("adjustmentXp") or 0),
        inspiration=bool(data.get("inspiration")),
        proficiency_bonus=prof,
        abilities=abilities,
        armor_class=ac,
        armor_class_source=ac_source,
        initiative=abilities["dexterity"].modifier + _sum_bonus(mods, "initiative"),
        max_hit_points=max_hp,
        current_hit_points=max_hp - removed,
        temp_hit_points=data.get("temporaryHitPoints") or 0,
        hit_dice={f"d{c.hit_die}": c.level for c in classes},
        speeds=speeds,
        skills=skills,
        passive_perception=10 + perception,
        passive_investigation=10 + investigation,
        passive_insight=10 + insight,
        senses=senses,
        armor_proficiencies=_proficiency_group(data, mods, "armor"),
        weapon_proficiencies=_proficiency_group(data, mods, "weapon"),
        tool_proficiencies=_proficiency_group(data, mods, "tool"),
        languages=sorted({titleize(s) for s in _subtypes(mods, "language")}),
        attacks=_parse_attacks(data, abilities, mods, prof),
        inventory=_parse_inventory(data),
        spells=_parse_spells(data),
        spell_slots=_parse_slots(data.get("spellSlots")) or _compute_spell_slots(classes),
        pact_slots=_parse_pact(data.get("pactMagic")) or _compute_pact_slots(classes),
        feats=_parse_feats(data),
        conditions=[(c.get("definition") or {}).get("name") or f"condition {c.get('id')}"
                    for c in (data.get("conditions") or [])],
        currencies={k: v for k, v in (data.get("currencies") or {}).items() if v},
        alignment=ALIGNMENTS.get(data.get("alignmentId")),
        age=data.get("age"),
        gender=data.get("gender"),
        height=data.get("height"),
        weight_lbs=data.get("weight"),
        eyes=data.get("eyes"),
        hair=data.get("hair"),
        skin=data.get("skin"),
        faith=data.get("faith"),
        raw=data,
    )


def _parse_classes(data: dict) -> List[CharClass]:
    out = []
    for c in data.get("classes") or []:
        definition = c.get("definition") or {}
        sub = (c.get("subclassDefinition") or {}).get("name")
        ability_id = definition.get("spellCastingAbilityId")
        out.append(CharClass(
            name=definition.get("name") or "Unknown",
            level=c.get("level") or 0,
            subclass=sub,
            hit_die=definition.get("hitDice") or 8,
            is_starting=bool(c.get("isStartingClass")),
            spellcasting_ability=ABILITY_BY_ID.get(ability_id) if ability_id else None,
        ))
    return out


def _stat_map(entries) -> Dict[str, Optional[int]]:
    out = {}
    for entry in entries or []:
        name = ABILITY_BY_ID.get(entry.get("id"))
        if name:
            out[name] = entry.get("value")
    return out


def _parse_abilities(data: dict, mods, prof: int) -> Dict[str, Ability]:
    base = _stat_map(data.get("stats"))
    bonus = _stat_map(data.get("bonusStats"))
    override = _stat_map(data.get("overrideStats"))

    abilities: Dict[str, Ability] = {}
    for name in ABILITIES:
        score = base.get(name) or 0
        score += bonus.get(name) or 0
        score += _sum_bonus(mods, f"{name}-score")

        # "set" modifiers (Belt of Giant Strength, Headband of Intellect...)
        for _, m in mods:
            if m.get("type") == "set" and m.get("subType") == f"{name}-score":
                score = max(score, _value_of(m))

        if override.get(name):
            score = override[name]

        mod = ability_modifier(score)
        save_prof = _has(mods, "proficiency", f"{name}-saving-throws")
        save = mod + (prof if save_prof else 0)
        save += _sum_bonus(mods, f"{name}-saving-throws", "saving-throws")
        abilities[name] = Ability(name, score, mod, save, save_prof)
    return abilities


def _parse_skills(abilities, mods, prof: int) -> Dict[str, Skill]:
    global_bonus = _sum_bonus(mods, "ability-checks", "skill-checks")
    half_all = _has(mods, "half-proficiency", "ability-checks")  # Jack of All Trades

    skills: Dict[str, Skill] = {}
    for name, ability in SKILLS.items():
        st = SKILL_SUBTYPE[name]
        proficient = _has(mods, "proficiency", st)
        expertise = _has(mods, "expertise", st)
        half = _has(mods, "half-proficiency", st) or half_all

        mod = abilities[ability].modifier + global_bonus + _sum_bonus(mods, st)
        if expertise:
            mod += prof * 2
        elif proficient:
            mod += prof
        elif half:
            mod += prof // 2
        skills[name] = Skill(name, ability, mod, proficient, expertise)
    return skills


def _compute_max_hp(data: dict, abilities, mods, total_level: int) -> int:
    if data.get("overrideHitPoints"):
        return int(data["overrideHitPoints"])

    hp = (data.get("baseHitPoints") or 0)
    hp += abilities["constitution"].modifier * total_level
    hp += data.get("bonusHitPoints") or 0

    for source, m in mods:
        if m.get("type") == "bonus" and m.get("subType") == "hit-points":
            per = _value_of(m)
            hp += per * total_level if source in PER_LEVEL_HP_SOURCES else per
    return hp


def _compute_ac(data: dict, abilities, mods) -> Tuple[int, str]:
    dex = abilities["dexterity"].modifier
    equipped = [i for i in (data.get("inventory") or []) if i.get("equipped")]

    body_armor = [i for i in equipped
                  if (i.get("definition") or {}).get("armorTypeId") in (1, 2, 3)]
    shields = [i for i in equipped
               if (i.get("definition") or {}).get("armorTypeId") == 4]

    candidates: List[Tuple[int, str]] = []

    for item in body_armor:
        d = item["definition"]
        armor_type = d.get("armorTypeId")
        base = d.get("armorClass") or 10
        if armor_type == 1:      # light
            base += dex
        elif armor_type == 2:    # medium (cap +2)
            base += min(dex, 2)
        candidates.append((base, f"{d.get('name')} ({ARMOR_TYPE.get(armor_type)})"))

    # Unarmored Defense and friends: "set: unarmored-armor-class" carries the
    # second ability in statId (Barbarian = CON, Monk = WIS).
    unarmored = 10 + dex
    label = "Unarmored (10 + DEX)"
    for _, m in mods:
        if m.get("type") == "set" and m.get("subType") == "unarmored-armor-class":
            stat = ABILITY_BY_ID.get(m.get("statId"))
            if stat:
                unarmored = max(unarmored, 10 + dex + abilities[stat].modifier)
                label = f"Unarmored Defense (10 + DEX + {ABBREV[stat]})"
    unarmored += _sum_bonus(mods, "unarmored-armor-class")
    candidates.append((unarmored, label))

    ac, source = max(candidates, key=lambda t: t[0])

    shield_ac = sum((s["definition"].get("armorClass") or 0) for s in shields)
    if shield_ac:
        ac += shield_ac
        source += f" + {shields[0]['definition'].get('name')}"

    # Rings of protection, +1 armour, Defense fighting style, ...
    ac += _sum_bonus(mods, "armor-class", "armored-armor-class")
    return ac, source


def _parse_speeds(data: dict, mods) -> Dict[str, int]:
    race = data.get("race") or {}
    weight_speeds = (race.get("weightSpeeds") or {}).get("normal") or {}
    speeds = {k: v for k, v in weight_speeds.items() if v}
    if not speeds:
        speeds = {"walk": 30}

    for _, m in mods:
        if m.get("type") == "set" and (m.get("subType") or "").startswith("innate-speed-"):
            key = m["subType"].replace("innate-speed-", "").replace("ing", "")
            speeds[key] = max(speeds.get(key, 0), _value_of(m))

    walk_bonus = _sum_bonus(mods, "speed", "unarmored-movement", "walking-speed")
    if walk_bonus:
        speeds["walk"] = speeds.get("walk", 30) + walk_bonus
    return {k: v for k, v in speeds.items() if v}


def _parse_senses(data: dict, mods) -> Dict[str, int]:
    senses: Dict[str, int] = {}
    for _, m in mods:
        st = m.get("subType") or ""
        if m.get("type") == "set" and st in ("darkvision", "blindsight", "truesight", "tremorsense"):
            senses[st] = max(senses.get(st, 0), _value_of(m))
    for s in data.get("customSenses") or []:
        if s.get("distance"):
            senses[f"custom-{s.get('senseId')}"] = s["distance"]
    return senses


def _proficiency_group(data: dict, mods, group: str) -> List[str]:
    """Split the flat proficiency list into armor / weapon / tool buckets."""
    armor_words = ("armor", "shield")
    weapon_words = ("weapon", "sword", "axe", "bow", "crossbow", "dagger", "club",
                    "hammer", "spear", "mace", "flail", "glaive", "halberd", "lance",
                    "pike", "rapier", "scimitar", "trident", "whip", "dart", "sling",
                    "javelin", "quarterstaff", "morningstar", "sickle", "blowgun",
                    "net", "firearm", "greataxe", "greatsword", "maul", "pistol",
                    "musket", "handaxe", "warhammer", "battleaxe", "shortbow", "longbow")

    out = []
    for st in _subtypes(mods, "proficiency"):
        if st.endswith("-saving-throws") or st in SKILL_SUBTYPE.values():
            continue
        low = st.lower()
        if group == "armor" and any(w in low for w in armor_words):
            out.append(titleize(st))
        elif group == "weapon" and any(w in low for w in weapon_words):
            out.append(titleize(st))
        elif group == "tool" and not any(w in low for w in armor_words + weapon_words):
            out.append(titleize(st))

    for p in data.get("customProficiencies") or []:
        name = p.get("name")
        if name and group == "tool":
            out.append(name)
    return sorted(set(out))


_FINESSE = "finesse"
_RANGED_TYPES = {2}  # attackType 1 = melee, 2 = ranged
# definition.categoryId on a weapon
WEAPON_CATEGORY = {1: "simple-weapons", 2: "martial-weapons"}


def _parse_attacks(data: dict, abilities, mods, prof: int) -> List[Attack]:
    weapon_profs = {s.lower() for s in _subtypes(mods, "proficiency")}
    attacks: List[Attack] = []

    for item in data.get("inventory") or []:
        d = item.get("definition") or {}
        if d.get("filterType") != "Weapon" or not item.get("equipped"):
            continue

        props = [p.get("name") for p in (d.get("properties") or []) if p.get("name")]
        prop_low = {p.lower() for p in props}
        ranged = d.get("attackType") in _RANGED_TYPES

        if _FINESSE in prop_low:
            ability = "dexterity" if abilities["dexterity"].modifier >= abilities["strength"].modifier else "strength"
        elif ranged:
            ability = "dexterity"
        else:
            ability = "strength"
        ab_mod = abilities[ability].modifier

        category = WEAPON_CATEGORY.get(d.get("categoryId"))
        name_subtype = (d.get("name") or "").lower().replace(" ", "-")
        proficient = (category in weapon_profs) or (name_subtype in weapon_profs)

        magic_bonus = sum(_value_of(g) for g in (d.get("grantedModifiers") or [])
                          if g.get("type") == "bonus" and g.get("subType") in
                          ("magic", "weapon-attacks", "weapon-attack-rolls"))

        dmg = d.get("damage") or {}
        dice = dmg.get("diceString") or (
            f"{dmg.get('diceCount', 1)}d{dmg.get('diceValue', 4)}" if dmg else "—")
        dmg_bonus = ab_mod + magic_bonus + (d.get("fixedDamage") or 0)
        damage = f"{dice}{fmt(dmg_bonus)}" if dmg_bonus else dice

        rng = None
        thrown = "thrown" in prop_low
        if d.get("range"):
            if ranged or thrown:
                rng = f"{d['range']}/{d.get('longRange') or d['range']} ft."
            else:
                rng = f"{d['range']} ft. reach"

        attacks.append(Attack(
            name=d.get("name") or "Weapon",
            attack_bonus=ab_mod + magic_bonus + (prof if proficient else 0),
            damage=damage,
            damage_type=d.get("damageType"),
            range=rng,
            properties=props,
            proficient=proficient,
            notes=f"uses {ABBREV[ability]}",
        ))

    attacks.sort(key=lambda a: a.name)
    return attacks


def _parse_inventory(data: dict) -> List[Item]:
    out = []
    for item in data.get("inventory") or []:
        d = item.get("definition") or {}
        out.append(Item(
            name=d.get("name") or "?",
            quantity=item.get("quantity") or 1,
            equipped=bool(item.get("equipped")),
            attuned=bool(item.get("isAttuned")),
            weight=float(d.get("weight") or 0),
            kind=d.get("filterType") or d.get("type"),
            rarity=d.get("rarity"),
            magic=bool(d.get("magic")),
            cost=d.get("cost"),
        ))
    return out


_ACTIVATION_TYPES = {
    1: "Action", 2: "No Action", 3: "Bonus Action", 4: "Reaction",
    5: "Minute", 6: "Hour", 7: "Special", 8: "Legendary Action",
}
_COMPONENT_LETTERS = {1: "V", 2: "S", 3: "M"}


def _spell_casting_time(d: dict) -> Optional[str]:
    act = d.get("activation") or {}
    atype = act.get("activationType")
    label = _ACTIVATION_TYPES.get(atype)
    n = act.get("activationTime")
    if not label:
        return None
    if n and n != 1:
        return f"{n} {label}s" if label in ("Minute", "Hour") else f"{label} ({n})"
    return "1 " + label if label in ("Minute", "Hour") else label


def _spell_range(d: dict) -> Optional[str]:
    r = d.get("range") or {}
    origin = r.get("origin")
    if origin in ("Self", "Touch"):
        text = origin
    elif r.get("rangeValue"):
        text = f"{r['rangeValue']} ft."
    else:
        return None
    if r.get("aoeType") and r.get("aoeValue"):
        text += f" ({r['aoeValue']} ft. {r['aoeType']})"
    return text


def _spell_duration(d: dict) -> Optional[str]:
    du = d.get("duration") or {}
    unit = du.get("durationUnit")
    if not unit:
        return None
    if unit == "Instantaneous":
        text = "Instantaneous"
    else:
        interval = du.get("durationInterval") or 1
        text = f"{interval} {unit}" + ("s" if interval != 1 else "")
    if d.get("concentration"):
        text = f"Concentration, up to {text}" if unit != "Instantaneous" else text
    return text


def _spell_components(d: dict) -> Optional[str]:
    comps = d.get("components") or []
    letters = [_COMPONENT_LETTERS[c] for c in comps if c in _COMPONENT_LETTERS]
    if not letters:
        return None
    text = ", ".join(letters)
    if "M" in letters and d.get("componentsDescription"):
        text += f" ({d['componentsDescription']})"
    return text


_DAMAGE_TYPES = ["acid", "bludgeoning", "cold", "fire", "force", "lightning", "necrotic",
                  "piercing", "poison", "psychic", "radiant", "slashing", "thunder"]
_BASE_DAMAGE_RE = re.compile(
    r"(\d+d\d+)(?:\s*\+\s*[\w.]+)?\s+(" + "|".join(_DAMAGE_TYPES) + r")\s+damage",
    re.IGNORECASE,
)
# Cantrip upgrade text reads like "...2d10 at 5th level, 3d10 at 11th level,
# and 4d10 at 17th level" or "...(2d10), 11th level (3d10)..." -- either
# ordering of the level and the dice.
_SCALING_RE = re.compile(
    r"(\d+d\d+)\D{0,12}?(\d+)(?:st|nd|rd|th)\s+level|"
    r"(\d+)(?:st|nd|rd|th)\s+level\D{0,12}?(\d+d\d+)",
    re.IGNORECASE,
)


def _spell_damage(d: dict, text: str) -> Tuple[Optional[str], Optional[str], List[Tuple[int, str]]]:
    """Best-effort damage type/dice for a cantrip.

    D&D Beyond's structured fields for this vary by spell, so this leans on
    whatever structured hints are present and falls back to reading the
    dice straight out of the rules text -- including the "cantrip upgrade"
    sentence that lists the dice at each higher level.
    """
    damage = d.get("damage") or {}
    dtype = damage.get("damageType") or d.get("damageType")
    dice = None
    dice_info = damage.get("damageDice") or damage.get("diceInfo") or {}
    if isinstance(dice_info, dict) and dice_info.get("diceCount") and dice_info.get("diceValue"):
        dice = f"{dice_info['diceCount']}d{dice_info['diceValue']}"

    if not dice or not dtype:
        m = _BASE_DAMAGE_RE.search(text or "")
        if m:
            dice = dice or m.group(1)
            dtype = dtype or m.group(2)

    scaling: List[Tuple[int, str]] = []
    ahl = d.get("atHigherLevels") or {}
    for entry in ahl.get("higherLevelDetails") or []:
        lvl = entry.get("level")
        die = entry.get("die") or entry.get("dice") or {}
        count = die.get("dieCount") or die.get("diceCount")
        value = die.get("dieValue") or die.get("diceValue")
        if lvl and count and value:
            scaling.append((int(lvl), f"{count}d{value}"))

    if not scaling:
        for m in _SCALING_RE.finditer(text or ""):
            if m.group(1):
                scaling.append((int(m.group(2)), m.group(1)))
            else:
                scaling.append((int(m.group(3)), m.group(4)))

    scaling.sort(key=lambda t: t[0])
    return (dtype.title() if dtype else None), dice, scaling


def _parse_spells(data: dict) -> List[Spell]:
    out: List[Spell] = []
    seen = set()

    def add(entry: dict, source: str):
        d = entry.get("definition") or {}
        name = d.get("name")
        if not name:
            return
        key = (name, source)
        if key in seen:
            return
        seen.add(key)
        description = d.get("description")
        dtype, base_dice, scaling = _spell_damage(d, description or "")
        out.append(Spell(
            name=name,
            level=d.get("level", 0) or 0,
            school=d.get("school"),
            prepared=bool(entry.get("prepared") or entry.get("alwaysPrepared")),
            source=source,
            ritual=bool(d.get("ritual")),
            concentration=bool(d.get("concentration")),
            description=description,
            casting_time=_spell_casting_time(d),
            range_text=_spell_range(d),
            duration_text=_spell_duration(d),
            components_text=_spell_components(d),
            damage_type=dtype,
            damage_base_dice=base_dice,
            damage_scaling=scaling,
        ))

    for bucket, entries in (data.get("spells") or {}).items():
        for entry in entries or []:
            add(entry, bucket)

    class_names = {c.get("id"): ((c.get("definition") or {}).get("name") or "class")
                   for c in (data.get("classes") or [])}
    for cs in data.get("classSpells") or []:
        label = class_names.get(cs.get("characterClassId"), "class")
        for entry in cs.get("spells") or []:
            add(entry, label.lower())

    out.sort(key=lambda s: (s.level, s.name))
    return out


def _parse_slots(slots) -> Dict[int, int]:
    # "available" is how many of this level's slots are *currently* unspent,
    # not the character's total -- a snapshot taken mid-adventure with slots
    # already spent would otherwise under-report (or drop entirely) levels
    # that have been used. Total capacity is available + used, same idea as
    # max HP being current + removed.
    out = {}
    for s in slots or []:
        total = (s.get("available") or 0) + (s.get("used") or 0)
        if total:
            out[s.get("level")] = total
    return out


def _parse_pact(pact) -> Dict[str, int]:
    for p in pact or []:
        total = (p.get("available") or 0) + (p.get("used") or 0)
        if total:
            return {"level": p.get("level"), "slots": total}
    return {}


# --------------------------------------------------------------------------
# Spell slot tables (SRD-standard). D&D Beyond's `spellSlots`/`pactMagic`
# fields turn out to read all-zero for at least some real characters (a
# level 4 Sorcerer, confirmed) even though they very much have slots -- that
# field tracks something other than total capacity. Total slots are a pure
# function of class level, so compute them from the standard tables and
# only fall back to whatever the API reported if a class can't be matched.
# --------------------------------------------------------------------------

_FULL_CASTER_SLOTS = {
    1: [2, 0, 0, 0, 0, 0, 0, 0, 0], 2: [3, 0, 0, 0, 0, 0, 0, 0, 0],
    3: [4, 2, 0, 0, 0, 0, 0, 0, 0], 4: [4, 3, 0, 0, 0, 0, 0, 0, 0],
    5: [4, 3, 2, 0, 0, 0, 0, 0, 0], 6: [4, 3, 3, 0, 0, 0, 0, 0, 0],
    7: [4, 3, 3, 1, 0, 0, 0, 0, 0], 8: [4, 3, 3, 2, 0, 0, 0, 0, 0],
    9: [4, 3, 3, 3, 1, 0, 0, 0, 0], 10: [4, 3, 3, 3, 2, 0, 0, 0, 0],
    11: [4, 3, 3, 3, 2, 1, 0, 0, 0], 12: [4, 3, 3, 3, 2, 1, 0, 0, 0],
    13: [4, 3, 3, 3, 2, 1, 1, 0, 0], 14: [4, 3, 3, 3, 2, 1, 1, 0, 0],
    15: [4, 3, 3, 3, 2, 1, 1, 1, 0], 16: [4, 3, 3, 3, 2, 1, 1, 1, 0],
    17: [4, 3, 3, 3, 2, 1, 1, 1, 1], 18: [4, 3, 3, 3, 3, 1, 1, 1, 1],
    19: [4, 3, 3, 3, 3, 2, 1, 1, 1], 20: [4, 3, 3, 3, 3, 2, 2, 1, 1],
}
_HALF_CASTER_SLOTS = {  # Paladin, Ranger -- 1st-5th level spells only
    1: [0, 0, 0, 0, 0], 2: [2, 0, 0, 0, 0], 3: [3, 0, 0, 0, 0], 4: [3, 0, 0, 0, 0],
    5: [4, 2, 0, 0, 0], 6: [4, 2, 0, 0, 0], 7: [4, 3, 0, 0, 0], 8: [4, 3, 0, 0, 0],
    9: [4, 3, 2, 0, 0], 10: [4, 3, 2, 0, 0], 11: [4, 3, 3, 0, 0], 12: [4, 3, 3, 0, 0],
    13: [4, 3, 3, 1, 0], 14: [4, 3, 3, 1, 0], 15: [4, 3, 3, 2, 0], 16: [4, 3, 3, 2, 0],
    17: [4, 3, 3, 3, 1], 18: [4, 3, 3, 3, 1], 19: [4, 3, 3, 3, 2], 20: [4, 3, 3, 3, 2],
}
_ARTIFICER_SLOTS = {  # rounds up instead of down, gets slots starting level 1
    1: [2, 0, 0, 0, 0], 2: [2, 0, 0, 0, 0], 3: [3, 0, 0, 0, 0], 4: [3, 0, 0, 0, 0],
    5: [4, 2, 0, 0, 0], 6: [4, 2, 0, 0, 0], 7: [4, 3, 0, 0, 0], 8: [4, 3, 0, 0, 0],
    9: [4, 3, 2, 0, 0], 10: [4, 3, 2, 0, 0], 11: [4, 3, 3, 0, 0], 12: [4, 3, 3, 0, 0],
    13: [4, 3, 3, 1, 0], 14: [4, 3, 3, 1, 0], 15: [4, 3, 3, 2, 0], 16: [4, 3, 3, 2, 0],
    17: [4, 3, 3, 3, 1], 18: [4, 3, 3, 3, 1], 19: [4, 3, 3, 3, 2], 20: [4, 3, 3, 3, 2],
}
_THIRD_CASTER_SLOTS = {  # Eldritch Knight, Arcane Trickster -- 1st-4th only
    1: [0, 0, 0, 0], 2: [0, 0, 0, 0], 3: [2, 0, 0, 0], 4: [3, 0, 0, 0],
    5: [3, 0, 0, 0], 6: [3, 0, 0, 0], 7: [4, 2, 0, 0], 8: [4, 2, 0, 0],
    9: [4, 2, 0, 0], 10: [4, 3, 0, 0], 11: [4, 3, 0, 0], 12: [4, 3, 0, 0],
    13: [4, 3, 2, 0], 14: [4, 3, 2, 0], 15: [4, 3, 2, 0], 16: [4, 3, 3, 0],
    17: [4, 3, 3, 0], 18: [4, 3, 3, 0], 19: [4, 3, 3, 1], 20: [4, 3, 3, 1],
}
_PACT_MAGIC_TABLE = {  # Warlock: (slot count, slot level) by warlock level
    1: (1, 1), 2: (2, 1), 3: (2, 2), 4: (2, 2), 5: (2, 3), 6: (2, 3),
    7: (2, 4), 8: (2, 4), 9: (2, 5), 10: (2, 5), 11: (3, 5), 12: (3, 5),
    13: (3, 5), 14: (3, 5), 15: (3, 5), 16: (3, 5), 17: (4, 5), 18: (4, 5),
    19: (4, 5), 20: (4, 5),
}
_FULL_CASTER_CLASSES = {"bard", "cleric", "druid", "sorcerer", "wizard"}
_HALF_CASTER_CLASSES = {"paladin", "ranger"}
_THIRD_CASTER_SUBCLASSES = {"eldritch knight", "arcane trickster"}


def _classify_casters(classes) -> List[Tuple[str, int]]:
    """(caster kind, level) for each class that draws on the shared slot
    pool. Warlock is excluded -- Pact Magic is a separate resource."""
    out = []
    for cl in classes:
        name = cl.name.lower()
        sub = (cl.subclass or "").lower()
        if name in _FULL_CASTER_CLASSES:
            out.append(("full", cl.level))
        elif name == "artificer":
            out.append(("artificer", cl.level))
        elif name in _HALF_CASTER_CLASSES:
            out.append(("half", cl.level))
        elif sub in _THIRD_CASTER_SUBCLASSES:
            out.append(("third", cl.level))
    return out


def _compute_spell_slots(classes) -> Dict[int, int]:
    casters = _classify_casters(classes)
    if not casters:
        return {}

    if len(casters) == 1:
        kind, level = casters[0]
        table = {"full": _FULL_CASTER_SLOTS, "half": _HALF_CASTER_SLOTS,
                 "artificer": _ARTIFICER_SLOTS, "third": _THIRD_CASTER_SLOTS}[kind]
        row = table[min(max(level, 1), 20)]
    else:
        # Multiclass spellcasting: combine into one caster level using the
        # PHB's fractional rule, then draw from the shared full-caster table.
        equiv = 0.0
        for kind, level in casters:
            if kind == "full":
                equiv += level
            elif kind == "half":
                equiv += level // 2
            elif kind == "artificer":
                equiv += math.ceil(level / 2)
            elif kind == "third":
                equiv += level // 3
        equiv = min(int(equiv), 20)
        row = _FULL_CASTER_SLOTS[equiv] if equiv > 0 else [0] * 9

    return {lvl: n for lvl, n in enumerate(row, start=1) if n}


def _compute_pact_slots(classes) -> Dict[str, int]:
    for cl in classes:
        if cl.name.lower() == "warlock":
            slots, slot_level = _PACT_MAGIC_TABLE[min(max(cl.level, 1), 20)]
            return {"level": slot_level, "slots": slots}
    return {}


# Not actual feats -- homebrew/optional entries that ride along in the same
# "feats" list on this character but shouldn't show up as such.
_IGNORED_FEATS = {"Dark Bargain", "Character Threads", "Runestones"}


def _parse_feats(data: dict) -> List[Feat]:
    out = []
    for f in data.get("feats") or []:
        d = f.get("definition") or {}
        name = d.get("name")
        if not name or name in _IGNORED_FEATS:
            continue
        out.append(Feat(name=name, description=d.get("description") or d.get("snippet")))
    return out


# --------------------------------------------------------------------------
# Pretty printing
# --------------------------------------------------------------------------


def _rule(title: str, width: int = 62) -> str:
    return f"\n{title}\n" + "-" * width


def render_sheet(c: Character) -> str:
    L: List[str] = []
    classes = " / ".join(str(x) for x in c.classes)
    L.append("=" * 62)
    L.append(f"  {c.name}  —  {c.race or '?'} {classes}")
    bits = [f"Level {c.level}", f"XP {c.xp}"]
    if c.background:
        bits.append(c.background)
    if c.alignment:
        bits.append(c.alignment)
    L.append("  " + "  |  ".join(bits))
    L.append("=" * 62)

    L.append(_rule("ABILITIES"))
    L.append("  " + "  ".join(f"{ABBREV[a]} {c.abilities[a].score:>2} ({fmt(c.abilities[a].modifier)})"
                              for a in ABILITIES))

    L.append(_rule("DEFENCE"))
    L.append(f"  AC {c.armor_class}   [{c.armor_class_source}]")
    hp = f"  HP {c.current_hit_points}/{c.max_hit_points}"
    if c.temp_hit_points:
        hp += f" (+{c.temp_hit_points} temp)"
    L.append(hp)
    L.append(f"  Hit Dice: " + ", ".join(f"{n}{die}" for die, n in c.hit_dice.items()))
    L.append(f"  Initiative {fmt(c.initiative)}   Prof {fmt(c.proficiency_bonus)}")
    L.append("  Speed: " + ", ".join(f"{k} {v} ft." for k, v in c.speeds.items()))
    if c.senses:
        L.append("  Senses: " + ", ".join(f"{titleize(k)} {v} ft." for k, v in c.senses.items()))
    L.append(f"  Passive: Perception {c.passive_perception}, "
             f"Investigation {c.passive_investigation}, Insight {c.passive_insight}")

    L.append(_rule("SAVING THROWS"))
    L.append("  " + "  ".join(
        f"{ABBREV[a]} {fmt(c.abilities[a].save)}{'*' if c.abilities[a].save_proficient else ' '}"
        for a in ABILITIES))

    L.append(_rule("SKILLS"))
    for name in sorted(c.skills):
        s = c.skills[name]
        mark = "E" if s.expertise else ("*" if s.proficient else " ")
        L.append(f"  {mark} {name.title():<18} {ABBREV[s.ability]}  {fmt(s.modifier)}")

    L.append(_rule("PROFICIENCIES"))
    for label, vals in (("Armor", c.armor_proficiencies),
                        ("Weapons", c.weapon_proficiencies),
                        ("Tools", c.tool_proficiencies),
                        ("Languages", c.languages)):
        L.append(f"  {label + ':':<11}{', '.join(vals) if vals else '—'}")
    if c.feats:
        L.append(f"  {'Feats:':<11}{', '.join(f.name for f in c.feats)}")

    if c.attacks:
        L.append(_rule("ATTACKS"))
        for a in c.attacks:
            extra = f"  [{a.range}]" if a.range else ""
            props = f"  ({', '.join(a.properties)})" if a.properties else ""
            L.append(f"  {a.name:<22} {fmt(a.attack_bonus):>3} to hit   "
                     f"{a.damage} {a.damage_type or ''}{extra}{props}")

    if c.spells:
        L.append(_rule("SPELLS"))
        if c.spell_slots:
            L.append("  Slots: " + ", ".join(f"L{lvl}×{n}" for lvl, n in sorted(c.spell_slots.items())))
        if c.pact_slots:
            L.append(f"  Pact Magic: {c.pact_slots.get('slots')} × level {c.pact_slots.get('level')}")
        by_level: Dict[int, List[str]] = {}
        for s in c.spells:
            tag = "" if s.prepared else " (unprepared)"
            by_level.setdefault(s.level, []).append(s.name + tag)
        for lvl in sorted(by_level):
            head = "Cantrips" if lvl == 0 else f"Level {lvl}"
            L.append(f"  {head}: " + ", ".join(sorted(by_level[lvl])))

    L.append(_rule("INVENTORY"))
    for i in c.inventory:
        flags = "".join(["E" if i.equipped else "", "A" if i.attuned else "", "*" if i.magic else ""])
        qty = f" x{i.quantity}" if i.quantity > 1 else ""
        L.append(f"  {flags:<3} {i.name}{qty}")
    if c.currencies:
        L.append("  Coin: " + ", ".join(f"{v} {k}" for k, v in c.currencies.items()))
    total_weight = sum(i.weight * i.quantity for i in c.inventory)
    L.append(f"  Carried weight: {total_weight:g} lb")

    if c.conditions:
        L.append(_rule("CONDITIONS"))
        L.append("  " + ", ".join(c.conditions))

    L.append("")
    L.append(f"  {c.url}")
    return "\n".join(L)


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------


def load_character(source: str, *, cobalt_session: Optional[str] = None) -> Character:
    """Parse a character from a URL, an id, or a path to a saved JSON file."""
    if os.path.exists(source):
        with open(source, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        data = payload.get("data", payload) if isinstance(payload, dict) else payload
        return parse_character(data)
    return parse_character(fetch_character_json(source, cobalt_session=cobalt_session))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Download and parse a D&D Beyond character sheet.",
        epilog="Private sheets need your CobaltSession cookie "
               "(env DDB_COBALT_SESSION or --cobalt).",
    )
    ap.add_argument("character", help="sheet URL, character id, or path to a saved JSON file")
    ap.add_argument("--cobalt", help="D&D Beyond CobaltSession cookie value")
    ap.add_argument("--json", action="store_true", help="print the parsed character as JSON")
    ap.add_argument("--raw", metavar="PATH", help="also save the raw API payload to PATH")
    ap.add_argument("--pdf", metavar="PATH", help="render a printable PDF character sheet to PATH")
    ap.add_argument("--timeout", type=int, default=30)
    args = ap.parse_args(argv)

    try:
        if os.path.exists(args.character):
            char = load_character(args.character)
        else:
            data = fetch_character_json(args.character, cobalt_session=args.cobalt,
                                        timeout=args.timeout)
            if args.raw:
                with open(args.raw, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, indent=2)
                print(f"raw JSON written to {args.raw}", file=sys.stderr)
            char = parse_character(data)
    except (DDBError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"network error: {exc}", file=sys.stderr)
        return 1

    if args.pdf:
        from character_sheet_pdf import render_pdf
        render_pdf(char, args.pdf)
        print(f"PDF character sheet written to {args.pdf}", file=sys.stderr)

    print(char.to_json() if args.json else render_sheet(char))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
