from .config import *
import uuid

"""
Дикая Охота 3.0 — слой "физики мира" морской боевой RPG.

Всё в этом файле — чистые функции без обращения к БД и без self.
Они существуют для одной цели: LLM ничего не имеет права утверждать
о состоянии мира напрямую. Она только ПРЕДЛАГАЕТ (текст/JSON), а
код здесь проверяет/обрезает/чинит это предложение до безопасной
структуры, прежде чем оно попадёт в hunt_player_state / hunt_facts /
hunt_ships.

    LLM = генератор и рассказчик.
    Этот модуль + database.py = закон физики.

Сеттинг: экипаж корабля-призрака против вражеских судов в открытом
море. Личное снаряжение экипажа (сабли, пистолеты, аптечки, такелаж)
живёт здесь же — это то, чем игрок реально может воспользоваться в
бою (см. HuntMixin._hunt_use_item), в отличие от того, что просто
придумала LLM в художественном тексте.
"""


HUNT_ROLE_PERMISSIONS = {
    "captain": {
        "FIRE_CANNON", "BOARD", "MANEUVER", "REPAIR",
        "COMMAND", "CREW_ACTION", "INSPECT", "USE_ITEM",
        "TALK", "ASSIGN_CREW", "INSPECT_CREW", "USE_FOOD",
        "FIGHT_FIRE", "LOAD_CANNON", "CHANGE_AMMO",
        "AIM_CANNON", "TAKE_FROM_SHIP_INVENTORY",
    },
    "first_officer": {
        "FIRE_CANNON", "BOARD", "MANEUVER", "REPAIR",
        "COMMAND", "CREW_ACTION", "INSPECT", "USE_ITEM",
        "TALK", "ASSIGN_CREW",
    },
    "navigator": {
        "MANEUVER", "INSPECT", "TALK", "USE_ITEM",
    },
    "boatswain": {
        "REPAIR", "ASSIGN_CREW", "CREW_ACTION", "INSPECT",
        "TALK", "USE_ITEM", "FIGHT_FIRE",
    },
    "gunner": {
        "FIRE_CANNON", "ASSIGN_CREW", "CREW_ACTION", "INSPECT",
        "TALK", "USE_ITEM", "LOAD_CANNON", "CHANGE_AMMO",
        "AIM_CANNON",
    },
    "doctor": {
        "INSPECT_CREW", "TREAT_CREW", "STABILIZE", "REVIVE",
        "ADMINISTER_STIMULANT", "TAKE_FROM_SHIP_INVENTORY",
        "INSPECT", "TALK", "USE_ITEM",
    },
    "cook": {
        "USE_FOOD", "BOARD", "INSPECT", "TALK", "USE_ITEM",
    },
    "marine": {
        "BOARD", "INSPECT", "TALK", "USE_ITEM",
    },
    "sailor": {
        "REPAIR", "FIRE_CANNON", "BOARD", "INSPECT", "TALK",
        "USE_ITEM",
    },
    "crew": {
        "TALK", "USE_ITEM", "INSPECT",
    },
}


def get_role_for_ship_position(position):
    """Возвращает каноническую роль Action API по users.ship_position."""
    return SHIP_POSITION_TO_ROLE.get(position, "crew")


def can_role_do(role, intent):
    """RBAC: имеет ли роль право на это намерение."""
    return intent in HUNT_ROLE_PERMISSIONS.get(role, set())


# ============================================================
# GENERIC HELPERS
# ============================================================

def _clamp_int(value, lo, hi, default):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def slugify(text, fallback="x"):
    text = str(text or "").strip().casefold()
    text = re.sub(r"[^\w]+", "_", text, flags=re.UNICODE)
    text = text.strip("_")
    return text[:40] or fallback


def load_json_dict(raw_json):
    data = safe_json_loads(raw_json, {})
    return data if isinstance(data, dict) else {}


def load_json_list(raw_json):
    data = safe_json_loads(raw_json, [])
    return data if isinstance(data, list) else []


# ============================================================
# ITEMS / INVENTORY
# ============================================================

ITEM_TAG_WHITELIST = {
    "melee", "ranged", "heavy", "light", "armor",
    "utility", "heal", "rope", "food", "tool",
    "trap", "container",
}

# Резервные комплекты личного снаряжения экипажа — используются,
# если LLM не смогла (или отказалась) сгенерировать инвентарь
# валидным JSON. Тематически это то, что матрос держит при себе на
# случай абордажа или аварии, а не судовое вооружение (пушки живут
# отдельно в hunt_weapons и генерируются вместе с кораблём).
DEFAULT_INVENTORY_KITS = [
    [
        {"id": "cutlass", "name": "Абордажная сабля", "tags": ["melee"], "damage": 12, "durability": 40, "uses": 1},
        {"id": "leather_jerkin", "name": "Кожаная куртка", "tags": ["armor"], "damage": 0, "durability": 50, "uses": 1},
        {"id": "rum_flask", "name": "Фляга рома с корабельной аптечки", "tags": ["heal"], "damage": 0, "durability": 100, "uses": 2},
        {"id": "tarred_rope", "name": "Просмолённый конец", "tags": ["rope", "utility"], "damage": 0, "durability": 100, "uses": 1},
    ],
    [
        {"id": "flintlock_pistol", "name": "Кремнёвый пистолет", "tags": ["ranged"], "damage": 9, "durability": 45, "uses": 1},
        {"id": "oilskin_coat", "name": "Просмолённый плащ", "tags": ["armor", "utility"], "damage": 0, "durability": 50, "uses": 1},
        {"id": "medicine_chest_kit", "name": "Набор из корабельной аптеки", "tags": ["heal"], "damage": 0, "durability": 100, "uses": 2},
    ],
    [
        {"id": "boarding_axe", "name": "Абордажный топор", "tags": ["melee", "heavy"], "damage": 14, "durability": 35, "uses": 1},
        {"id": "signal_lantern", "name": "Сигнальный фонарь", "tags": ["utility"], "damage": 3, "durability": 30, "uses": 3},
    ],
]


def starting_ship_inventory():
    """Детерминированный стартовый корабельный inventory Hunt World."""
    return [
        {"item_type": "cannonball", "item_name": "Корабельные ядра", "quantity": 30},
        {"item_type": "grapeshot", "item_name": "Картечь", "quantity": 8},
        {"item_type": "powder", "item_name": "Пороховой запас", "quantity": 6},
        {"item_type": "water", "item_name": "Питьевая вода", "quantity": 12},
        {"item_type": "firefighting_water", "item_name": "Вода для тушения пожара", "quantity": 8},
        {"item_type": "sailcloth", "item_name": "Парусная ткань", "quantity": 8},
        {"item_type": "rope", "item_name": "Такелажный трос", "quantity": 6},
        {"item_type": "wood", "item_name": "Доски для корпуса", "quantity": 8},
        {"item_type": "planks", "item_name": "Запасной планкинг", "quantity": 6},
        {"item_type": "tar", "item_name": "Корабельная смола", "quantity": 3},
        {"item_type": "tools", "item_name": "Ремонтный инструмент", "quantity": 2},
        {"item_type": "bandage", "item_name": "Бинты", "quantity": 10},
        {"item_type": "medicine", "item_name": "Лекарства", "quantity": 5},
        {"item_type": "medical_kit", "item_name": "Медицинский набор", "quantity": 2},
        {"item_type": "stimulant", "item_name": "Стимулятор", "quantity": 1},
        {"item_type": "food", "item_name": "Провизия", "quantity": 10},
        {"item_type": "rations", "item_name": "Сухпайки", "quantity": 6},
        {"item_type": "special_food", "item_name": "Особая еда кока", "quantity": 2},
        {"item_type": "cutlass", "item_name": "Абордажные сабли", "quantity": 5},
        {"item_type": "pistol", "item_name": "Пистолеты", "quantity": 3},
        {"item_type": "boarding_weapon", "item_name": "Абордажное оружие", "quantity": 4},
        {"item_type": "lantern", "item_name": "Фонари", "quantity": 2},
        {"item_type": "oil", "item_name": "Ламповое масло", "quantity": 2},
    ]


def sanitize_item(raw):
    if isinstance(raw, str):
        raw = {"name": raw}

    if not isinstance(raw, dict):
        return None

    name = str(raw.get("name") or "").strip()[:60]

    if not name:
        return None

    tags = []
    tags_raw = raw.get("tags")

    if isinstance(tags_raw, list):
        for t in tags_raw[:5]:
            t = str(t).strip().casefold()
            if t in ITEM_TAG_WHITELIST:
                tags.append(t)

    damage = raw.get("damage")
    damage = _clamp_int(damage, 0, 35, 0) if damage is not None else 0

    durability = raw.get("durability")
    durability = _clamp_int(durability, 1, 100, 100) if durability is not None else 100

    uses = raw.get("uses")
    uses = _clamp_int(uses, 1, 10, 1) if uses is not None else 1

    item_type = str(raw.get("item_type") or slugify(name, fallback="item"))[:40]
    instance_id = str(raw.get("instance_id") or "").strip()[:80]
    if not instance_id:
        instance_id = f"{item_type}_{uuid.uuid4().hex[:12]}"

    return {
        "id": instance_id,
        "instance_id": instance_id,
        "item_type": item_type,
        "name": name,
        "tags": tags,
        "damage": damage,
        "durability": durability,
        "uses": uses,
    }


def sanitize_inventory(raw_list, max_items=HUNT_MAX_INVENTORY_ITEMS):
    result = []

    if isinstance(raw_list, list):
        for entry in raw_list:
            item = sanitize_item(entry)

            if item:
                result.append(item)

            if len(result) >= max_items:
                break

    return result


def random_starting_inventory():
    # Каждый экземпляр получает собственный ID: два одинаковых предмета
    # больше не расходуются/изнашиваются одновременно.
    return [sanitize_item(item) for item in random.choice(DEFAULT_INVENTORY_KITS)]


def find_item(inventory, hint):
    """
    Нечёткий поиск предмета в инвентаре по тексту игрока.
    Возвращает dict предмета или None — если игрок утверждает,
    что у него есть предмет, которого нет, находка не создаётся.
    """

    if not hint or not isinstance(inventory, list) or not inventory:
        return None

    hint_cf = str(hint).strip().casefold()

    if not hint_cf:
        return None

    for item in inventory:
        name_cf = str(item.get("name", "")).casefold()
        if name_cf and (hint_cf in name_cf or name_cf in hint_cf):
            return item

    names = [str(item.get("name", "")) for item in inventory]
    close = difflib.get_close_matches(hint, names, n=1, cutoff=0.55)

    if close:
        for item in inventory:
            if item.get("name") == close[0]:
                return item

    return None


def has_tag(item, tag):
    return bool(item) and tag in (item.get("tags") or [])


def remove_item(inventory, item_id):
    return [i for i in inventory if i.get("id") != item_id]


def weapon_damage(item, bare_hands_damage=HUNT_BARE_HANDS_DAMAGE):
    if not item:
        return bare_hands_damage
    return max(bare_hands_damage, _clamp_int(item.get("damage"), 0, 35, bare_hands_damage))


def apply_durability_hit(inventory, item_id, wear=15):
    """Изнашивает предмет; при durability<=0 он выпадает из инвентаря."""

    result = []

    for item in inventory:

        if item.get("id") == item_id:
            item = dict(item)
            current = _clamp_int(item.get("durability"), 0, 100, 100)
            item["durability"] = max(0, current - wear)

            if item["durability"] <= 0:
                continue

        result.append(item)

    return result


def consume_item_use(inventory, item_id):
    """
    Тратит один заряд (uses) предмета — используется при активном
    применении личного снаряжения в бою (аптечка, инструмент и т.д.).
    При исчерпании uses предмет выпадает из инвентаря, как и при
    полном износе durability.
    """

    result = []

    for item in inventory:

        if item.get("id") == item_id:
            item = dict(item)
            current = _clamp_int(item.get("uses"), 0, 10, 1)
            item["uses"] = max(0, current - 1)

            if item["uses"] <= 0:
                continue

        result.append(item)

    return result


# ============================================================
# WORLD / LOCATIONS
# ============================================================

DEFAULT_WORLD = {
    "region_name": "Пролив Мертвяков",
    "weather": "низкий холодный туман стелется над водой",
    "world_time": "глухая ночь",
    "danger_level": 2,
}

DEFAULT_START_LOCATION = {
    "title": "Открытая вода у скалистого мыса",
    "description": (
        "Чёрная вода едва плещет о борт. Слева по курсу — острые рифы "
        "мыса, справа — плотная стена тумана, в которой время от "
        "времени мелькают чужие огни. Такелаж поскрипывает, экипаж "
        "молчит и слушает море."
    ),
    "tags": ["open_water", "fog", "rocky_cape"],
    "exits": ["в открытое море", "на восток вдоль рифов", "в туман"],
}


def sanitize_world(raw):
    if not isinstance(raw, dict):
        raw = {}

    region = str(raw.get("region_name") or DEFAULT_WORLD["region_name"]).strip()[:60]
    weather = str(raw.get("weather") or DEFAULT_WORLD["weather"]).strip()[:80]
    world_time = str(raw.get("world_time") or DEFAULT_WORLD["world_time"]).strip()[:40]
    danger = _clamp_int(raw.get("danger_level"), 1, 5, DEFAULT_WORLD["danger_level"])

    return {
        "region_name": region or DEFAULT_WORLD["region_name"],
        "weather": weather or DEFAULT_WORLD["weather"],
        "world_time": world_time or DEFAULT_WORLD["world_time"],
        "danger_level": danger,
    }


def sanitize_location(raw, fallback=None):
    fallback = fallback or DEFAULT_START_LOCATION

    if not isinstance(raw, dict):
        raw = {}

    title = str(raw.get("title") or fallback["title"]).strip()[:60] or fallback["title"]

    description = str(
        raw.get("description") or fallback["description"]
    ).strip()[:500] or fallback["description"]

    tags = []
    tags_raw = raw.get("tags")

    if isinstance(tags_raw, list):
        for t in tags_raw[:8]:
            t = str(t).strip()
            if t:
                tags.append(slugify(t))

    if not tags:
        tags = list(fallback.get("tags", []))

    exits = []
    exits_raw = raw.get("exits")

    if isinstance(exits_raw, list):
        for e in exits_raw[:6]:
            if isinstance(e, dict):
                label = str(e.get("label") or e.get("key") or "").strip()
            else:
                label = str(e).strip()

            if label:
                exits.append(label[:40])

    if not exits:
        exits = list(fallback.get("exits", []))

    return {
        "title": title,
        "description": description,
        "tags": tags,
        "exits": exits,
    }


def match_exit(exits, text):
    """
    Пытается сопоставить свободный текст игрока ("идём в открытое
    море") с одним из известных направлений текущей локации. Если
    ничего не подходит достаточно уверенно — возвращает None.
    """

    if not exits or not text:
        return None

    text_cf = text.strip().casefold()

    for label in exits:
        label_cf = label.casefold()
        if label_cf in text_cf or text_cf in label_cf:
            return label

    close = difflib.get_close_matches(text, exits, n=1, cutoff=0.45)

    return close[0] if close else None


# ============================================================
# RELATIONSHIPS (для "мягкой" памяти между игроками экипажа)
# ============================================================

def load_relationships(raw_json):
    return load_json_dict(raw_json)


def bump_relationship(rel_map, other_user_id, **deltas):
    entry = rel_map.get(other_user_id) or {
        "trust": 0,
        "helped": 0,
        "betrayed": 0,
        "attacked": 0,
        "saved": 0,
    }

    for key, delta in deltas.items():
        entry[key] = int(entry.get(key, 0)) + delta

    rel_map[other_user_id] = entry

    return rel_map


def compute_relationship_badges(states):
    """
    states: список dict из hunt_player_state (уже словари).
    Возвращает лидеров по категориям для финального отыгрыша —
    "Спаситель", "Предатель", "Самый опасный", "Любимец экипажа".
    Каждый лидер — (user_id, значение) либо ключ отсутствует,
    если ни у кого значение не набралось выше нуля.

    Примечание: в текущей морской версии отношения между игроками
    не заполняются активным боевым циклом (экипаж действует как
    единое судно, личного PvP нет — см. README), поэтому эта
    функция сейчас не вызывается из HuntMixin. Она сохранена как
    готовый строительный блок на случай, если в будущем появится
    механика личных заслуг/предательств внутри экипажа.
    """

    helped_totals = Counter()
    betrayed_totals = Counter()
    attacked_totals = Counter()

    for state in states:
        rel = load_relationships(state.get("relationships_json"))
        actor = state["user_id"]

        for _other_id, counters in rel.items():
            if not isinstance(counters, dict):
                continue

            helped_totals[actor] += int(counters.get("helped", 0))
            betrayed_totals[actor] += int(counters.get("betrayed", 0))
            attacked_totals[actor] += int(counters.get("attacked", 0))

    leaders = {}

    if helped_totals:
        top_id, top_val = helped_totals.most_common(1)[0]
        if top_val > 0:
            leaders["saviour"] = (top_id, top_val)

    if betrayed_totals:
        top_id, top_val = betrayed_totals.most_common(1)[0]
        if top_val > 0:
            leaders["traitor"] = (top_id, top_val)

    if attacked_totals:
        top_id, top_val = attacked_totals.most_common(1)[0]
        if top_val > 0:
            leaders["dangerous"] = (top_id, top_val)

    crew_favorite = None
    best_catches = 0

    for state in states:
        catches = int(state.get("catches") or 0)
        if catches > best_catches:
            best_catches = catches
            crew_favorite = state["user_id"]

    if crew_favorite and best_catches > 0:
        leaders["crew_favorite"] = (crew_favorite, best_catches)

    return leaders
