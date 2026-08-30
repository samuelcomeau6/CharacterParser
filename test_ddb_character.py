"""Tests for ddb_character.py.

The fixture below mirrors the real payload for D&D Beyond character 167142616
("Saphire", Human Barbarian 5, Path of the World Tree). The expected numbers
are the ones the D&D Beyond sheet itself renders.
"""

import json
import unittest

from ddb_character import (
    DDBError,
    parse_character,
    parse_character_id,
    ability_modifier,
)


def stat_block(values):
    return [{"id": i + 1, "name": None, "value": v} for i, v in enumerate(values)]


def mod(mtype, subtype, value=None, fixed=None, stat=None, component=None,
        requires_attunement=False):
    return {
        "type": mtype, "subType": subtype, "value": value, "fixedValue": fixed,
        "statId": stat, "componentId": component, "requiresAttunement": requires_attunement,
        "dice": None, "restriction": "",
    }


def weapon(name, *, category, dice, dtype, props, equipped, rng, long_range,
           attack_type=1, item_id=0, granted=None):
    return {
        "id": item_id, "quantity": 1, "equipped": equipped, "isAttuned": False,
        "definition": {
            "id": item_id, "name": name, "filterType": "Weapon", "type": name,
            "categoryId": category, "attackType": attack_type,
            "damage": {"diceString": dice, "diceCount": int(dice[0]), "diceValue": int(dice.split("d")[1])},
            "damageType": dtype, "weight": 2, "rarity": "Common", "magic": False,
            "range": rng, "longRange": long_range, "armorTypeId": None, "armorClass": None,
            "properties": [{"name": p} for p in props],
            "grantedModifiers": granted or [],
            "canAttune": False, "cost": 5,
        },
    }


SAPHIRE = {
    "id": 167142616,
    "name": "Saphire",
    "username": "samjcom",
    "inspiration": False,
    "baseHitPoints": 40,
    "bonusHitPoints": None,
    "overrideHitPoints": None,
    "removedHitPoints": 0,
    "temporaryHitPoints": 0,
    "currentXp": 0,
    "adjustmentXp": None,
    "alignmentId": None,
    "stats": stat_block([15, 13, 15, 8, 12, 8]),
    "bonusStats": stat_block([None] * 6),
    "overrideStats": stat_block([None] * 6),
    "background": {"definition": {"name": "Soldier"}},
    "race": {
        "fullName": "Human", "baseName": "Human",
        "weightSpeeds": {"normal": {"walk": 30, "fly": 0, "burrow": 0, "swim": 0, "climb": 0}},
    },
    "classes": [{
        "id": 1,
        "level": 5,
        "isStartingClass": True,
        "definition": {"name": "Barbarian", "hitDice": 12, "spellCastingAbilityId": None},
        "subclassDefinition": {"name": "Path of the World Tree"},
    }],
    "currencies": {"cp": 0, "sp": 0, "gp": 279, "ep": 0, "pp": 0},
    "conditions": [],
    "customProficiencies": [],
    "customSenses": [],
    "feats": [],
    "spells": {"race": [], "class": [], "background": [], "item": [], "feat": []},
    "classSpells": [{"characterClassId": 1, "spells": []}],
    "spellSlots": [],
    "pactMagic": [],
    "inventory": [
        {   # not equipped -> its +2 hit points must NOT apply
            "id": 100, "quantity": 2, "equipped": False, "isAttuned": False,
            "definition": {
                "id": 8960641, "name": "Potion of Healing", "filterType": "Potion",
                "weight": 0.5, "rarity": "Common", "magic": True, "canAttune": False,
                "armorTypeId": None, "armorClass": None, "cost": 50,
                "grantedModifiers": [mod("bonus", "hit-points", fixed=2, component=8960641)],
            },
        },
        weapon("Handaxe", category=1, dice="1d6", dtype="Slashing",
               props=["Light", "Thrown", "Vex"], equipped=True, rng=20, long_range=60, item_id=7),
        weapon("Handaxe", category=1, dice="1d6", dtype="Slashing",
               props=["Light", "Thrown", "Vex"], equipped=False, rng=20, long_range=60, item_id=7),
        weapon("Greataxe", category=2, dice="1d12", dtype="Slashing",
               props=["Heavy", "Two-Handed", "Cleave"], equipped=True, rng=5, long_range=5, item_id=9),
        weapon("Longsword", category=2, dice="1d8", dtype="Slashing",
               props=["Versatile", "Sap"], equipped=False, rng=5, long_range=5, item_id=11),
    ],
    "modifiers": {
        "race": [
            mod("proficiency", "perception"),
            mod("size", "medium"),
            mod("language", "common"),
            mod("language", "giant"),
            mod("language", "orc"),
        ],
        "class": [
            mod("set", "unarmored-armor-class", stat=3),
            mod("advantage", "dexterity-saving-throws"),
            mod("set", "subclass"),
            mod("proficiency", "nature"),
            mod("set", "extra-attacks", value=1, fixed=1),
            mod("bonus", "speed", value=10, fixed=10),
            mod("proficiency", "survival"),
            mod("proficiency", "athletics"),
            mod("proficiency", "strength-saving-throws"),
            mod("proficiency", "constitution-saving-throws"),
            mod("proficiency", "simple-weapons"),
            mod("proficiency", "martial-weapons"),
            mod("proficiency", "light-armor"),
            mod("proficiency", "medium-armor"),
            mod("proficiency", "shields"),
        ],
        "background": [
            mod("proficiency", "medicine"),
            mod("proficiency", "intimidation"),
            mod("proficiency", "playing-card-set"),
        ],
        "item": [mod("bonus", "hit-points", fixed=2, component=8960641)],
        "feat": [
            mod("bonus", "dexterity-score", value=1, fixed=1),
            mod("bonus", "constitution-score", value=1, fixed=1),
            mod("weapon-mastery", "cleave-greataxe"),
            mod("weapon-mastery", "vex-handaxe"),
            mod("bonus", "constitution-score", value=2, fixed=2),
            mod("bonus", "strength-score", value=1, fixed=1),
        ],
        "condition": [],
    },
}


class TestIdParsing(unittest.TestCase):
    def test_forms(self):
        for src in [
            "https://www.dndbeyond.com/characters/167142616",
            "https://www.dndbeyond.com/characters/167142616/builder",
            "http://dndbeyond.com/character/167142616",
            "167142616",
            "  167142616  ",
        ]:
            self.assertEqual(parse_character_id(src), 167142616, src)

    def test_bad(self):
        with self.assertRaises(ValueError):
            parse_character_id("not-a-character")


class TestAbilityModifier(unittest.TestCase):
    def test_table(self):
        self.assertEqual(ability_modifier(1), -5)
        self.assertEqual(ability_modifier(8), -1)
        self.assertEqual(ability_modifier(10), 0)
        self.assertEqual(ability_modifier(11), 0)
        self.assertEqual(ability_modifier(18), 4)
        self.assertEqual(ability_modifier(20), 5)


class TestSaphire(unittest.TestCase):
    """Every expectation here matches what dndbeyond.com renders for this sheet."""

    @classmethod
    def setUpClass(cls):
        cls.c = parse_character(SAPHIRE)

    def test_identity(self):
        self.assertEqual(self.c.name, "Saphire")
        self.assertEqual(self.c.race, "Human")
        self.assertEqual(self.c.background, "Soldier")
        self.assertEqual(self.c.level, 5)
        self.assertEqual(str(self.c.classes[0]), "Barbarian (Path of the World Tree) 5")

    def test_ability_scores(self):
        expected = {"strength": 16, "dexterity": 14, "constitution": 18,
                    "intelligence": 8, "wisdom": 12, "charisma": 8}
        got = {k: v.score for k, v in self.c.abilities.items()}
        self.assertEqual(got, expected)

    def test_ability_modifiers(self):
        expected = {"strength": 3, "dexterity": 2, "constitution": 4,
                    "intelligence": -1, "wisdom": 1, "charisma": -1}
        got = {k: v.modifier for k, v in self.c.abilities.items()}
        self.assertEqual(got, expected)

    def test_proficiency_bonus(self):
        self.assertEqual(self.c.proficiency_bonus, 3)

    def test_hit_points(self):
        # 40 base + (CON 4 x 5 levels); the un-equipped potion's +2 must not count
        self.assertEqual(self.c.max_hit_points, 60)
        self.assertEqual(self.c.current_hit_points, 60)

    def test_armor_class(self):
        # Unarmored Defense: 10 + DEX 2 + CON 4
        self.assertEqual(self.c.armor_class, 16)
        self.assertIn("Unarmored Defense", self.c.armor_class_source)

    def test_initiative_and_speed(self):
        self.assertEqual(self.c.initiative, 2)
        self.assertEqual(self.c.speeds["walk"], 40)  # 30 + Fast Movement

    def test_saving_throws(self):
        saves = {k: v.save for k, v in self.c.abilities.items()}
        self.assertEqual(saves, {"strength": 6, "dexterity": 2, "constitution": 7,
                                 "intelligence": -1, "wisdom": 1, "charisma": -1})
        self.assertTrue(self.c.abilities["strength"].save_proficient)
        self.assertFalse(self.c.abilities["wisdom"].save_proficient)

    def test_skills(self):
        s = self.c.skills
        self.assertEqual(s["athletics"].modifier, 6)      # STR 3 + prof
        self.assertEqual(s["perception"].modifier, 4)     # WIS 1 + prof
        self.assertEqual(s["survival"].modifier, 4)
        self.assertEqual(s["medicine"].modifier, 4)
        self.assertEqual(s["intimidation"].modifier, 2)   # CHA -1 + prof
        self.assertEqual(s["nature"].modifier, 2)         # INT -1 + prof
        self.assertEqual(s["acrobatics"].modifier, 2)     # DEX, untrained
        self.assertEqual(s["arcana"].modifier, -1)        # INT, untrained
        self.assertTrue(s["athletics"].proficient)
        self.assertFalse(s["stealth"].proficient)

    def test_passives(self):
        self.assertEqual(self.c.passive_perception, 14)
        self.assertEqual(self.c.passive_investigation, 9)
        self.assertEqual(self.c.passive_insight, 11)

    def test_proficiency_buckets(self):
        self.assertEqual(self.c.armor_proficiencies,
                         ["Light Armor", "Medium Armor", "Shields"])
        self.assertEqual(self.c.weapon_proficiencies,
                         ["Martial Weapons", "Simple Weapons"])
        self.assertEqual(self.c.tool_proficiencies, ["Playing Card Set"])
        self.assertEqual(self.c.languages, ["Common", "Giant", "Orc"])

    def test_attacks(self):
        names = [a.name for a in self.c.attacks]
        self.assertEqual(names, ["Greataxe", "Handaxe"])  # only equipped weapons
        greataxe = self.c.attacks[0]
        self.assertEqual(greataxe.attack_bonus, 6)        # STR 3 + prof 3
        self.assertEqual(greataxe.damage, "1d12+3")
        self.assertEqual(greataxe.range, "5 ft. reach")
        handaxe = self.c.attacks[1]
        self.assertEqual(handaxe.attack_bonus, 6)
        self.assertEqual(handaxe.damage, "1d6+3")
        self.assertEqual(handaxe.range, "20/60 ft.")

    def test_inventory_and_coin(self):
        potion = next(i for i in self.c.inventory if i.name == "Potion of Healing")
        self.assertEqual(potion.quantity, 2)
        self.assertFalse(potion.equipped)
        self.assertEqual(self.c.currencies, {"gp": 279})

    def test_serialisable(self):
        payload = json.loads(self.c.to_json())
        self.assertEqual(payload["name"], "Saphire")
        self.assertEqual(payload["armor_class"], 16)
        self.assertNotIn("raw", payload)

    def test_renders(self):
        text = self.c.summary()
        self.assertIn("Saphire", text)
        self.assertIn("AC 16", text)
        self.assertIn("HP 60/60", text)


class TestEdgeCases(unittest.TestCase):
    def test_armored_beats_unarmored(self):
        data = json.loads(json.dumps(SAPHIRE))
        data["inventory"].append({
            "id": 200, "quantity": 1, "equipped": True, "isAttuned": False,
            "definition": {"id": 200, "name": "Half Plate", "filterType": "Armor",
                           "armorTypeId": 2, "armorClass": 15, "weight": 40,
                           "rarity": "Common", "magic": False, "canAttune": False,
                           "grantedModifiers": [], "properties": []},
        })
        data["inventory"].append({
            "id": 201, "quantity": 1, "equipped": True, "isAttuned": False,
            "definition": {"id": 201, "name": "Shield", "filterType": "Armor",
                           "armorTypeId": 4, "armorClass": 2, "weight": 6,
                           "rarity": "Common", "magic": False, "canAttune": False,
                           "grantedModifiers": [], "properties": []},
        })
        c = parse_character(data)
        # Half plate 15 + min(DEX 2, 2) = 17, beats unarmored 16; +2 shield
        self.assertEqual(c.armor_class, 19)
        self.assertIn("Half Plate", c.armor_class_source)

    def test_override_hp_and_damage_taken(self):
        data = json.loads(json.dumps(SAPHIRE))
        data["overrideHitPoints"] = 99
        data["removedHitPoints"] = 12
        data["temporaryHitPoints"] = 5
        c = parse_character(data)
        self.assertEqual(c.max_hit_points, 99)
        self.assertEqual(c.current_hit_points, 87)
        self.assertEqual(c.temp_hit_points, 5)

    def test_score_override_wins(self):
        data = json.loads(json.dumps(SAPHIRE))
        data["overrideStats"][0]["value"] = 20  # strength
        c = parse_character(data)
        self.assertEqual(c.abilities["strength"].score, 20)
        self.assertEqual(c.abilities["strength"].modifier, 5)

    def test_set_score_item(self):
        data = json.loads(json.dumps(SAPHIRE))
        data["modifiers"]["item"].append(
            mod("set", "strength-score", value=21, fixed=21, component=300))
        data["inventory"].append({
            "id": 300, "quantity": 1, "equipped": True, "isAttuned": True,
            "definition": {"id": 300, "name": "Belt of Fire Giant Strength",
                           "filterType": "Wondrous item", "canAttune": True,
                           "armorTypeId": None, "armorClass": None, "weight": 1,
                           "rarity": "Legendary", "magic": True,
                           "grantedModifiers": [], "properties": []},
        })
        c = parse_character(data)
        self.assertEqual(c.abilities["strength"].score, 21)

    def test_unattuned_item_modifier_ignored(self):
        data = json.loads(json.dumps(SAPHIRE))
        data["modifiers"]["item"].append(
            mod("bonus", "armor-class", value=1, fixed=1, component=301,
                requires_attunement=True))
        data["inventory"].append({
            "id": 301, "quantity": 1, "equipped": True, "isAttuned": False,
            "definition": {"id": 301, "name": "Ring of Protection",
                           "filterType": "Ring", "canAttune": True,
                           "armorTypeId": None, "armorClass": None, "weight": 0,
                           "rarity": "Rare", "magic": True,
                           "grantedModifiers": [], "properties": []},
        })
        self.assertEqual(parse_character(data).armor_class, 16)

        data["inventory"][-1]["isAttuned"] = True
        self.assertEqual(parse_character(data).armor_class, 17)

    def test_expertise_and_multiclass_prof(self):
        data = json.loads(json.dumps(SAPHIRE))
        data["classes"].append({
            "id": 2, "level": 3, "isStartingClass": False,
            "definition": {"name": "Rogue", "hitDice": 8, "spellCastingAbilityId": None},
            "subclassDefinition": None,
        })
        data["modifiers"]["class"].append(mod("expertise", "stealth"))
        data["modifiers"]["class"].append(mod("proficiency", "stealth"))
        c = parse_character(data)
        self.assertEqual(c.level, 8)
        self.assertEqual(c.proficiency_bonus, 3)
        self.assertEqual(c.skills["stealth"].modifier, 2 + 6)  # DEX + 2x prof
        self.assertTrue(c.skills["stealth"].expertise)
        self.assertEqual(c.hit_dice, {"d12": 5, "d8": 3})

    def test_per_level_hp_feat(self):
        data = json.loads(json.dumps(SAPHIRE))
        data["modifiers"]["feat"].append(mod("bonus", "hit-points", value=2, fixed=2))
        c = parse_character(data)
        self.assertEqual(c.max_hit_points, 60 + 10)  # Tough: 2 per level

    def test_error_payload(self):
        from ddb_character import _unwrap
        body = json.dumps({"success": False, "message": "An unexpected error has occurred",
                           "data": {"serverMessage": "Unauthorized Access Attempt."}}).encode()
        with self.assertRaises(DDBError):
            _unwrap(body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
