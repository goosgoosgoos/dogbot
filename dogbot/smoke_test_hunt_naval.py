"""
Изолированный smoke-тест морского движка «Дикой Охоты» после
переодевания hunt_world.py и дописывания USE_ITEM в hunt.py.

Сеть/БД недоступны в песочнице, поэтому:
  - hunt_world.py (чистые функции без I/O) тестируется напрямую;
  - HuntMixin.* тестируется с лёгким fake-объектом self.db, который
    просто записывает, какие методы были вызваны и с какими
    аргументами — вместо реального aiosqlite (см. smoke_stubs/).

Тестируется РЕАЛЬНЫЙ код dogbot/hunt.py и dogbot/hunt_world.py, а не
его копия.

Раздел 3 — регресс на баг из аудита: в _hunt_autonomous_event
собственный корабль (own) не обновлялся внутри цикла по врагам, из-за
чего при нескольких врагах, стреляющих в один тик, до БД доезжал
только последний выстрел — предыдущие попадания молча терялись.

Раздел 4 — регресс на другую находку из аудита: _hunt_generate_world
писал регион/погоду/локацию в БД, но ни один нарративный промпт это
не читал обратно (get_hunt_facts вообще нигде не вызывался). Проверяем,
что _hunt_world_flavor() реально попадает в промпт, а не просто
существует как мёртвый геттер.

Раздел 5 — регресс на третью находку: hunt_get_open_boarding() искала
status='in_progress', но _hunt_board() никогда не писал такой статус
(абордаж решается одним броском за один вызов), поэтому защита от
повторного/параллельного абордажа одной и той же цели никогда не
срабатывала. Тестируется НАСТОЯЩИЙ hunt_try_claim_boarding/
hunt_resolve_boarding из dogbot/database.py против настоящей
in-memory SQLite (aiosqlite в песочнице недоступен — см.
smoke_stubs/aiosqlite.py, — поэтому стандартный sqlite3 обёрнут в
тонкий асинхронный фасад именно для этого раздела).

Раздел 6 — переход времени Охоты на UTC. Раньше _hunt_now() отдавал
datetime.now(DOG_TIMEZONE), а _hunt_dt() для naive-таймстампов из БД
тоже предполагал DOG_TIMEZONE — но now_iso() (общий хелпер БД) пишет
naive datetime.now(), которое на типичном сервере/контейнере и есть
UTC. Теперь _hunt_now()/_hunt_dt() работают в UTC, а DOG_TIMEZONE
используется только на границах: разбор времени, введённого
человеком (_parse_hunt_time), и форматирование для показа в чате.
"""
import asyncio
import os
import random
import sqlite3
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
_STUBS_DIR = os.path.join(_PROJECT_ROOT, "smoke_stubs")

sys.path.insert(0, _STUBS_DIR)
sys.path.insert(0, _PROJECT_ROOT)

from datetime import datetime, timedelta, timezone  # noqa: E402

from dogbot import database as db_module  # noqa: E402
from dogbot import hunt_world as world  # noqa: E402
from dogbot.config import DOG_TIMEZONE, HUNT_START_LOCATION_KEY  # noqa: E402
from dogbot.hunt import HuntMixin  # noqa: E402

passed = 0
failed = 0


def check(label, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"OK   {label}")
    else:
        failed += 1
        print(f"FAIL {label}")


check("RBAC: канонир может FIRE_CANNON",
      world.can_role_do("gunner", "FIRE_CANNON") is True)
check("RBAC: врач не может FIRE_CANNON",
      world.can_role_do("doctor", "FIRE_CANNON") is False)
check("RBAC: роль матрос не может CHANGE_AMMO",
      world.can_role_do("sailor", "CHANGE_AMMO") is False)


# ============================================================
# 1) hunt_world.py — морская тема, без леса
# ============================================================

world_data = world.sanitize_world({})
check("sanitize_world() fallback -> морской регион",
      world_data["region_name"] == world.DEFAULT_WORLD["region_name"])
check("sanitize_world() fallback без леса",
      "бор" not in world_data["region_name"].lower())

loc = world.sanitize_location({})
check("sanitize_location() fallback -> морская локация",
      loc["title"] == world.DEFAULT_START_LOCATION["title"])
check("sanitize_location() exits не про болото",
      all("болот" not in e.lower() for e in loc["exits"]))

kit = world.random_starting_inventory()
check("random_starting_inventory() непустой", len(kit) > 0)
check("random_starting_inventory() имеет уникальные instance ID", len({i["id"] for i in kit}) == len(kit))
check("random_starting_inventory() без 'ржавого тесака' из леса",
      all("ржавый тесак" not in i["name"].lower() for i in kit))

item = world.find_item(kit, kit[0]["name"])
check("find_item() находит существующий предмет", item is not None and item["id"] == kit[0]["id"])

missing = world.find_item(kit, "предмет_которого_точно_нет_12345")
check("find_item() не находит несуществующий предмет", missing is None)

reduced = world.consume_item_use(kit, kit[0]["id"])
before_uses = kit[0]["uses"]
after_item = next((i for i in reduced if i["id"] == kit[0]["id"]), None)
if before_uses > 1:
    check("consume_item_use() тратит один заряд", after_item is not None and after_item["uses"] == before_uses - 1)
else:
    check("consume_item_use() удаляет предмет при исчерпании uses", after_item is None)

badges = world.compute_relationship_badges([
    {"user_id": "u1", "relationships_json": "{}", "catches": 3},
    {"user_id": "u2", "relationships_json": "{}", "catches": 1},
])
check("compute_relationship_badges() -> crew_favorite (не pack_favorite)",
      "crew_favorite" in badges and badges["crew_favorite"][0] == "u1")


# ============================================================
# 2) HuntMixin._hunt_use_item / _hunt_resolve_action / _hunt_finish
# ============================================================

class FakeDB:
    def __init__(self):
        self.calls = []
        self.ships = [
            {"id": 42, "side": "player", "name": "Немезида", "hp": 40, "max_hp": 100,
             "sails": 80, "crew_morale": 50, "status": "damaged"},
        ]
        self.weapons = {}  # ship_id -> [weapon dict, ...]
        self.hunt_world = {}  # hunt_id -> dict
        self.hunt_locations = {}  # (hunt_id, location_key) -> dict

    async def get_ship_position(self, user_id):
        # Для smoke-теста достаточно роли, которой разрешён USE_ITEM.
        return "кок"

    async def hunt_get_ships(self, hunt_id):
        self.calls.append(("hunt_get_ships", hunt_id))
        return self.ships

    async def hunt_update_ship(self, ship_id, **fields):
        self.calls.append(("hunt_update_ship", ship_id, fields))
        self.last_ship_update = fields
        for ship in self.ships:
            if ship["id"] == ship_id:
                ship.update(fields)

    async def hunt_apply_ship_delta(self, ship_id, **deltas):
        self.calls.append(("hunt_apply_ship_delta", ship_id, deltas))
        for ship in self.ships:
            if ship["id"] == ship_id:
                ship["hp"] = max(0, min(ship.get("max_hp", 99999), ship.get("hp", 0) + deltas.get("hp_delta", 0)))
                ship["sails"] = max(0, min(100, ship.get("sails", 0) + deltas.get("sails_delta", 0)))
                ship["crew_morale"] = max(0, min(100, ship.get("crew_morale", 0) + deltas.get("morale_delta", 0)))
                ship["distance"] = max(5, min(100, ship.get("distance", 50) + deltas.get("distance_delta", 0)))
                ship["heading"] = (ship.get("heading", 0) + deltas.get("heading_delta", 0)) % 360
                return dict(ship)
        return None

    async def hunt_claim_weapon_shot(self, weapon_id, now_text, reload_seconds):
        self.calls.append(("hunt_claim_weapon_shot", weapon_id, now_text, reload_seconds))
        return True

    async def hunt_apply_crew_damage(self, hunt_id, ship_id, damage):
        self.calls.append(("hunt_apply_crew_damage", hunt_id, ship_id, damage))
        return await self.hunt_get_ship(ship_id)

    async def hunt_get_crew_role(self, hunt_id, user_id):
        return None

    async def hunt_get_ship_crew_summary(self, hunt_id, ship_id):
        ship = await self.hunt_get_ship(ship_id)
        if not ship:
            return None
        return {"ship": ship, "crew_count": int(ship.get("crew_count", 4) or 4), "crew_hp": int(ship.get("crew_hp", 400) or 400), "crew_dead": 0}

    async def hunt_ship_inventory_consume_many(self, hunt_id, requirements):
        self.calls.append(("hunt_ship_inventory_consume_many", hunt_id, requirements))
        return True

    async def hunt_ship_inventory_consume(self, hunt_id, item_type, quantity=1):
        self.calls.append(("hunt_ship_inventory_consume", hunt_id, item_type, quantity))
        return True

    async def hunt_ship_inventory_get(self, hunt_id):
        return []

    async def hunt_log_action(self, *args, **kwargs):
        self.calls.append(("hunt_log_action", args, kwargs))

    async def update_hunt_player_state(self, hunt_id, user_id, **fields):
        self.calls.append(("update_hunt_player_state", hunt_id, user_id, fields))
        self.last_inventory_json = fields.get("inventory_json")

    # --- нужно для _hunt_autonomous_event ---
    async def hunt_get_weapons(self, ship_id):
        self.calls.append(("hunt_get_weapons", ship_id))
        return self.weapons.get(ship_id, [])

    async def hunt_update_weapon(self, weapon_id, **fields):
        self.calls.append(("hunt_update_weapon", weapon_id, fields))
        for weapons in self.weapons.values():
            for w in weapons:
                if w["id"] == weapon_id:
                    w.update(fields)

    async def update_hunt_pack_state(self, hunt_id, **fields):
        self.calls.append(("update_hunt_pack_state", hunt_id, fields))

    async def hunt_get_ship(self, ship_id):
        for ship in self.ships:
            if ship["id"] == ship_id:
                return dict(ship)
        return None

    # --- нужно для _hunt_world_flavor ---
    async def get_hunt_world(self, hunt_id):
        self.calls.append(("get_hunt_world", hunt_id))
        return self.hunt_world.get(hunt_id)

    async def get_hunt_location(self, hunt_id, location_key):
        self.calls.append(("get_hunt_location", hunt_id, location_key))
        return self.hunt_locations.get((hunt_id, location_key))

    # --- нужно для _hunt_finish ---
    async def get_all_hunt_player_states(self, hunt_id):
        self.calls.append(("get_all_hunt_player_states", hunt_id))
        return getattr(self, "player_states", [])

    async def finish_hunt(self, hunt_id):
        self.calls.append(("finish_hunt", hunt_id))
        return True

    async def award_hunt_reward_once(self, hunt_id, user_id, coins, rep, description=""):
        self.calls.append(("award_hunt_reward_once", hunt_id, user_id, coins, rep))
        return coins, True


class FakeBot(HuntMixin):
    def __init__(self):
        self.db = FakeDB()
        self.room = "hunt@example.com"
        self.sent_messages = []
        self.captured_prompts = []

    async def call_openrouter(self, messages, *args, **kwargs):
        # В smoke-тестах сеть недоступна: нарратив не проверяем по
        # содержанию ответа, но сам промпт сохраняем — так можно
        # убедиться, что нужные данные реально в него попадают
        # (см. проверку world_flavor ниже), а не просто вычисляются
        # и выбрасываются.
        self.captured_prompts.append(messages[0]["content"] if messages else "")
        return ""

    def send_message(self, **kwargs):
        self.sent_messages.append(kwargs)


async def run_async_checks():
    bot = FakeBot()

    hunt = {"id": 1}
    heal_item = {"id": "rum_flask", "name": "Фляга рома с корабельной аптечки",
                 "tags": ["heal"], "damage": 0, "durability": 100, "uses": 2}
    state = {
        "user_id": "u1", "nickname": "Джек",
        "inventory_json": '[{"id": "rum_flask", "name": "Фляга рома с корабельной аптечки", "tags": ["heal"], "damage": 0, "durability": 100, "uses": 2}]',
    }
    own = {"id": 42, "hp": 40, "max_hp": 100, "sails": 80, "crew_morale": 50}

    result = await bot._hunt_use_item(hunt, state, own, {"weapon_hint": "ром"})
    check("_hunt_use_item(heal) -> success", result.get("success") is True)
    check("_hunt_use_item(heal) -> hp_gain > 0", result.get("effect", {}).get("hp_gain", 0) > 0)
    check("_hunt_use_item(heal) вызвал атомарное изменение корабля", any(c[0] == "hunt_apply_ship_delta" for c in bot.db.calls))
    check("_hunt_use_item(heal) записал новый инвентарь", bot.db.last_inventory_json is not None)

    result_missing = await bot._hunt_use_item(hunt, state, own, {"weapon_hint": "чего-то-там-нет"})
    check("_hunt_use_item(нет предмета) -> success=False", result_missing.get("success") is False)
    check("_hunt_use_item(нет предмета) -> reason=item_not_found", result_missing.get("reason") == "item_not_found")

    parsed_use = {"intent": "USE_ITEM", "weapon_hint": "ром"}
    dispatched = await bot._hunt_resolve_action(hunt, state, parsed_use)
    check("_hunt_resolve_action(USE_ITEM) доходит до _hunt_use_item", dispatched.get("action") == "USE_ITEM")

    parsed_talk = {"intent": "TALK", "command": "Держим строй!"}
    talk_result = await bot._hunt_resolve_action(hunt, state, parsed_talk)
    check("_hunt_resolve_action(TALK) -> success", talk_result.get("success") is True)
    check("_hunt_resolve_action(TALK) -> action=TALK", talk_result.get("action") == "TALK")

    fallback_use_ok = bot._hunt_result_fallback({"action": "USE_ITEM", "success": True, "item": "Фляга рома"})
    check("_hunt_result_fallback(USE_ITEM ok) содержит имя предмета", "Фляга рома" in fallback_use_ok)

    fallback_use_fail = bot._hunt_result_fallback({"action": "USE_ITEM", "success": False, "reason": "item_not_found"})
    check("_hunt_result_fallback(USE_ITEM fail) не пустой", bool(fallback_use_fail))


asyncio.run(run_async_checks())


# ============================================================
# 3) _hunt_autonomous_event — урон нескольких врагов за один тик
#    не должен "съедаться" (регресс на баг, где own оставался
#    несвежим весь цикл, и записывался только последний выстрел).
# ============================================================

async def run_autonomous_event_checks():
    bot = FakeBot()
    bot.db.ships = [
        {"id": 42, "side": "player", "name": "Немезида", "hp": 500, "max_hp": 500,
         "sails": 100, "crew_morale": 50, "status": "afloat"},
    ]
    own = {"id": 42, "hp": 500, "max_hp": 500, "sails": 100, "crew_morale": 50}
    enemies = [
        {"id": 1, "name": "Враг-1", "distance": 50, "tactics": "aggressive", "crew_morale": 50},
        {"id": 2, "name": "Враг-2", "distance": 50, "tactics": "aggressive", "crew_morale": 50},
        {"id": 3, "name": "Враг-3", "distance": 50, "tactics": "aggressive", "crew_morale": 50},
    ]
    for enemy in enemies:
        bot.db.weapons[enemy["id"]] = [
            {"id": 1000 + enemy["id"], "name": "пушка", "ammo": 3, "damage": 100,
             "range": 90, "reload": 10, "ready_at": None},
        ]
    hunt = {"id": 1}

    orig_random = random.random
    orig_randint = random.randint
    try:
        random.random = lambda: 0.0  # каждый выстрел гарантированно попадает
        random.randint = lambda a, b: (a + b) // 2  # детерминированный урон
        await bot._hunt_autonomous_event(hunt, own, enemies)
    finally:
        random.random = orig_random
        random.randint = orig_randint

    fire_calls = [c for c in bot.db.calls if c[0] == "hunt_apply_ship_delta" and c[1] == 42]
    check("_hunt_autonomous_event() записал по выстрелу на каждого врага",
          len(fire_calls) == len(enemies))

    final_hp = bot.db.ships[0]["hp"]
    # (min+max)//2 для damage = randint(max(8, 100-20), 100+10) = randint(80, 110) -> 95
    expected_hp = 500 - len(enemies) * 95
    check(
        f"_hunt_autonomous_event() накапливает урон от {len(enemies)} врагов "
        f"(HP={final_hp}, ожидалось {expected_hp}, а не {500 - 95})",
        final_hp == expected_hp,
    )


asyncio.run(run_autonomous_event_checks())


# ============================================================
# 4) _hunt_world_flavor — сгенерированный мир должен реально
#    попадать в промпты, а не только записываться в БД.
# ============================================================

async def run_world_flavor_checks():
    bot = FakeBot()
    hunt_id = 7

    empty_flavor = await bot._hunt_world_flavor(hunt_id)
    check("_hunt_world_flavor() без данных в БД возвращает пустую строку",
          empty_flavor == "")

    bot.db.hunt_world[hunt_id] = {
        "region_name": "Гнилые Отмели",
        "weather": "густой туман",
        "world_time": "предрассветные сумерки",
        "danger_level": 4,
    }
    bot.db.hunt_locations[(hunt_id, HUNT_START_LOCATION_KEY)] = {
        "title": "Разбитые рифы",
        "description": "цепь скал, торчащих из воды у самого фарватера",
        "tags": ["опасно"],
        "exits": {},
    }

    flavor = await bot._hunt_world_flavor(hunt_id)
    check("_hunt_world_flavor() содержит регион", "Гнилые Отмели" in flavor)
    check("_hunt_world_flavor() содержит погоду", "густой туман" in flavor)
    check("_hunt_world_flavor() содержит локацию", "Разбитые рифы" in flavor)

    # Теперь проверяем, что это реально доезжает до LLM-промпта,
    # а не просто корректно форматируется и выбрасывается.
    bot.db.ships = [
        {"id": 42, "side": "player", "name": "Немезида", "hp": 400, "max_hp": 500,
         "sails": 90, "crew_morale": 50, "status": "afloat"},
    ]
    own = {"id": 42, "hp": 400, "max_hp": 500, "sails": 90, "crew_morale": 50}
    enemies = [
        {"id": 1, "name": "Враг-1", "distance": 60, "tactics": "cautious", "crew_morale": 50},
    ]
    hunt = {"id": hunt_id}
    await bot._hunt_autonomous_event(hunt, own, enemies)

    check(
        "_hunt_autonomous_event() передал world_flavor в промпт LLM",
        any("Гнилые Отмели" in p for p in bot.captured_prompts),
    )

    bot2 = FakeBot()
    hunt2_id = 8
    bot2.db.hunt_world[hunt2_id] = {
        "region_name": "Пролив Вдовы",
        "weather": "штиль",
        "world_time": "полночь",
        "danger_level": 2,
    }
    bot2.db.hunt_locations[(hunt2_id, HUNT_START_LOCATION_KEY)] = {
        "title": "Мёртвая вода",
        "description": "неподвижная гладь без единого ветерка",
        "tags": [],
        "exits": {},
    }
    bot2.db.ships = [
        {"id": 99, "side": "player", "name": "Скиталец", "hp": 300, "max_hp": 500,
         "sails": 50, "crew_morale": 40, "status": "damaged"},
    ]
    await bot2._hunt_finish({"id": hunt2_id})
    check(
        "_hunt_finish() передал world_flavor в финальный промпт LLM",
        any("Пролив Вдовы" in p for p in bot2.captured_prompts),
    )


asyncio.run(run_world_flavor_checks())


# ============================================================
# 5) hunt_try_claim_boarding / hunt_resolve_boarding — реальная
#    защита от повторного/параллельного абордажа одной и той же
#    цели (регресс на мёртвую hunt_get_open_boarding).
# ============================================================

class _AsyncCursor:
    def __init__(self, cursor):
        self._cursor = cursor
        self.lastrowid = cursor.lastrowid
        self.rowcount = cursor.rowcount

    async def fetchone(self):
        return self._cursor.fetchone()

    async def fetchall(self):
        return self._cursor.fetchall()


class _AsyncConn:
    """Тончайший асинхронный фасад над стандартным sqlite3 —
    aiosqlite в песочнице недоступен (см. smoke_stubs/aiosqlite.py),
    а логике hunt_try_claim_boarding/hunt_resolve_boarding нужен
    настоящий SQL, а не пересказ его поведения в Python."""

    def __init__(self, sqlite_conn):
        self._conn = sqlite_conn
        self._conn.row_factory = sqlite3.Row

    async def execute(self, sql, params=()):
        return _AsyncCursor(self._conn.execute(sql, params))

    async def commit(self):
        self._conn.commit()

    async def rollback(self):
        self._conn.rollback()


class TinyBoardingDB:
    def __init__(self):
        self.conn = _AsyncConn(sqlite3.connect(":memory:"))
        self.write_lock = asyncio.Lock()

    async def setup(self, ship_statuses):
        await self.conn.execute(
            "CREATE TABLE hunt_ships (id INTEGER PRIMARY KEY, hunt_id INTEGER DEFAULT 1, status TEXT DEFAULT 'afloat')"
        )
        await self.conn.execute("""
            CREATE TABLE hunt_boardings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hunt_id INTEGER NOT NULL,
                attacker_ship_id INTEGER NOT NULL,
                defender_ship_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                progress INTEGER DEFAULT 0,
                attacker_crew INTEGER DEFAULT 0,
                defender_crew INTEGER DEFAULT 0,
                started_at TIMESTAMP,
                resolved_at TIMESTAMP,
                result_json TEXT DEFAULT '{}'
            )
        """)
        await self.conn.execute("CREATE UNIQUE INDEX idx_tiny_active_boarding ON hunt_boardings(hunt_id, defender_ship_id) WHERE status='in_progress'")
        for ship_id, status in ship_statuses.items():
            await self.conn.execute(
                "INSERT INTO hunt_ships (id, hunt_id, status) VALUES (?, 1, ?)", (ship_id, status),
            )
        await self.conn.commit()


# Тот же приём, что и в dogbot/database.py: вешаем настоящие функции
# модуля на тестовый класс вместо переписывания их логики заново.
TinyBoardingDB.hunt_try_claim_boarding = db_module._hunt_try_claim_boarding
TinyBoardingDB.hunt_resolve_boarding = db_module._hunt_resolve_boarding


async def run_boarding_claim_checks():
    db = TinyBoardingDB()
    await db.setup({1: "afloat", 2: "afloat"})
    hunt_id, attacker_id, defender_id = 1, 1, 2

    claimed1, boarding_id1, reason1 = await db.hunt_try_claim_boarding(hunt_id, attacker_id, defender_id)
    check("hunt_try_claim_boarding() первая заявка на свежую пару разрешена", claimed1 is True)
    check("hunt_try_claim_boarding() вернул id заявки", boarding_id1 is not None)

    claimed2, _, reason2 = await db.hunt_try_claim_boarding(hunt_id, attacker_id, defender_id)
    check(
        "hunt_try_claim_boarding() ВТОРАЯ заявка на ту же пару, пока первая не решена, отклонена "
        "(регресс: раньше эта защита не срабатывала никогда)",
        claimed2 is False,
    )
    check("hunt_try_claim_boarding() причина отказа — boarding_in_progress", reason2 == "boarding_in_progress")

    # Первая заявка отбита ("repelled", цель не захвачена) — новая попытка разрешена.
    await db.hunt_resolve_boarding(boarding_id1, "repelled", 40, 5, 6)
    claimed3, boarding_id3, reason3 = await db.hunt_try_claim_boarding(hunt_id, attacker_id, defender_id)
    check("hunt_try_claim_boarding() после repelled — новая попытка разрешена", claimed3 is True)

    # Вторая заявка выиграна ("won") — цель считается захваченной извне (как это делает _hunt_board).
    await db.hunt_resolve_boarding(boarding_id3, "won", 100, 5, 3)
    await db.conn.execute("UPDATE hunt_ships SET status='captured' WHERE id=?", (defender_id,))
    await db.conn.commit()

    claimed4, _, reason4 = await db.hunt_try_claim_boarding(hunt_id, attacker_id, defender_id)
    check("hunt_try_claim_boarding() на уже захваченную цель — отказ", claimed4 is False)
    check("hunt_try_claim_boarding() причина отказа — already_taken", reason4 == "already_taken")

    # Другая пара (другой атакующий, та же цель) уже захваченную цель тоже не берёт.
    claimed5, _, reason5 = await db.hunt_try_claim_boarding(hunt_id, 99, defender_id)
    check("hunt_try_claim_boarding() already_taken действует для любого атакующего", claimed5 is False)

    # Несуществующая цель.
    claimed6, _, reason6 = await db.hunt_try_claim_boarding(hunt_id, attacker_id, 424242)
    check("hunt_try_claim_boarding() на несуществующую цель — отказ no_target", claimed6 is False and reason6 == "no_target")

    # Параллельные заявки на свежую пару: ровно одна должна пройти.
    db2 = TinyBoardingDB()
    await db2.setup({10: "afloat", 20: "afloat"})
    results = await asyncio.gather(
        db2.hunt_try_claim_boarding(1, 10, 20),
        db2.hunt_try_claim_boarding(1, 10, 20),
    )
    claims = [r[0] for r in results]
    check(
        "hunt_try_claim_boarding() при двух параллельных заявках проходит ровно одна",
        claims.count(True) == 1 and claims.count(False) == 1,
    )
    db3 = TinyBoardingDB()
    await db3.setup({10: "afloat", 20: "afloat", 30: "afloat"})
    results = await asyncio.gather(
        db3.hunt_try_claim_boarding(1, 10, 20),
        db3.hunt_try_claim_boarding(1, 30, 20),
    )
    claims = [r[0] for r in results]
    check(
        "hunt_try_claim_boarding() при разных атакующих на одну цель проходит ровно одна",
        claims.count(True) == 1 and claims.count(False) == 1,
    )


asyncio.run(run_boarding_claim_checks())


# ============================================================
# 6) UTC: _hunt_now / _hunt_dt / _parse_hunt_time
# ============================================================

async def run_utc_checks():
    bot = FakeBot()

    now = bot._hunt_now()
    check("_hunt_now() возвращает aware datetime", now.tzinfo is not None)
    check("_hunt_now() именно в UTC (offset 0)", now.utcoffset() == timedelta(0))

    # Старая naive-строка (как писал now_iso() до перехода на UTC) —
    # должна читаться как UTC, а не как DOG_TIMEZONE (+7 по умолчанию).
    naive = "2026-01-01T12:00:00"
    parsed_naive = bot._hunt_dt(naive)
    check(
        "_hunt_dt() трактует старую naive-строку как UTC, а не DOG_TIMEZONE "
        f"(получили {parsed_naive.isoformat()})",
        parsed_naive == datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
    )

    # Новая aware-строка с явным смещением — должна корректно
    # нормализоваться в UTC (12:00+07:00 = 05:00 UTC).
    aware = "2026-01-01T12:00:00+07:00"
    parsed_aware = bot._hunt_dt(aware)
    check(
        "_hunt_dt() нормализует aware-строку с offset'ом в UTC "
        f"(получили {parsed_aware.isoformat()})",
        parsed_aware == datetime(2026, 1, 1, 5, 0, tzinfo=timezone.utc),
    )

    # _parse_hunt_time: человек пишет абсолютное время в СВОЁМ часовом
    # поясе (DOG_TIMEZONE), функция должна отдать эквивалент в UTC.
    start_at = bot._parse_hunt_time("2026-06-01 20:00")
    check("_parse_hunt_time() возвращает UTC-aware datetime", start_at.utcoffset() == timedelta(0))
    local_back = start_at.astimezone(DOG_TIMEZONE)
    check(
        "_parse_hunt_time() -> astimezone(DOG_TIMEZONE) возвращает исходные 20:00 "
        f"(получили {local_back.strftime('%H:%M')})",
        (local_back.hour, local_back.minute) == (20, 0),
    )

    # relative "%H:%M" тоже должен интерпретироваться в DOG_TIMEZONE,
    # а не в UTC (иначе "!охота начать 20:00" на сервере в UTC при
    # DOG_BOT_TZ_OFFSET_HOURS=7 назначил бы Охоту на 13:00 по факту).
    relative = bot._parse_hunt_time("23:59")
    check("_parse_hunt_time() относительного времени тоже в UTC на выходе", relative.utcoffset() == timedelta(0))
    relative_local = relative.astimezone(DOG_TIMEZONE)
    check(
        "_parse_hunt_time('23:59') соответствует 23:59 по DOG_TIMEZONE "
        f"(получили {relative_local.strftime('%H:%M')})",
        (relative_local.hour, relative_local.minute) == (23, 59),
    )


asyncio.run(run_utc_checks())


print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
