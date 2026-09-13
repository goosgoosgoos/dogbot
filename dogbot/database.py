from .config import *
from .hunt_world import random_starting_inventory

class UserFactsDB:
    """
    Основное асинхронное хранилище.

    users:
      - факты;
      - nickname;
      - balance;
      - respect_until;
      - reputation.

    market_lots:
      - ценные лоты.

    lot_investments:
      - отдельные инвестиционные позиции.

    transactions:
      - полный журнал движения денег.

    system_state:
      - банк;
      - инфляция;
      - время последней инфляции.
    """

    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self.conn = None
        self.write_lock = asyncio.Lock()

    # ========================================================
    # SETUP
    # ========================================================

    async def setup(self):
        if self.conn is not None:
            return

        self.conn = await aiosqlite.connect(
            self.db_path
        )

        self.conn.row_factory = aiosqlite.Row

        await self.conn.execute(
            "PRAGMA journal_mode=WAL"
        )

        await self.conn.execute(
            "PRAGMA busy_timeout=5000"
        )

        await self.conn.execute(
            "PRAGMA foreign_keys=ON"
        )

        await self.init_db()

    async def init_db(self):
        async with self.write_lock:

            await self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    nickname TEXT,
                    facts_json TEXT DEFAULT '[]',
                    last_updated TIMESTAMP,
                    balance REAL DEFAULT 0,
                    respect_until TIMESTAMP,
                    reputation INTEGER DEFAULT 0,
                    ship_position TEXT,
                    ship_position_changed_at TIMESTAMP
                )
                """
            )

            cursor = await self.conn.execute(
                "PRAGMA table_info(users)"
            )

            columns = {
                row["name"]
                for row in await cursor.fetchall()
            }

            migrations = {
                "nickname": "TEXT",
                "facts_json": "TEXT DEFAULT '[]'",
                "last_updated": "TIMESTAMP",
                "balance": "REAL DEFAULT 0",
                "respect_until": "TIMESTAMP",
                "reputation": "INTEGER DEFAULT 0",
                "facts_migrated": "INTEGER DEFAULT 0",
                "last_nick_update": "TIMESTAMP",
                "ship_position": "TEXT",
                "ship_position_changed_at": "TIMESTAMP",
            }

            for name, sql_type in migrations.items():
                if name not in columns:
                    await self.conn.execute(
                        f"ALTER TABLE users ADD COLUMN {name} {sql_type}"
                    )

            # ------------------------------------------------
            # INITIAL SHIP POSITIONS
            # ------------------------------------------------
            # Роли задаются по зарегистрированному нику. UPDATE безопасен:
            # существующая вручную назначенная роль не перетирается.
            await self.conn.execute(
                "UPDATE users SET ship_position=?, ship_position_changed_at=? WHERE ship_position IS NULL AND nickname=?",
                ("капитан", now_iso(), "russoturisto"),
            )
            await self.conn.execute(
                "UPDATE users SET ship_position=?, ship_position_changed_at=? WHERE ship_position IS NULL AND nickname=?",
                ("боцман", now_iso(), "goos"),
            )
            await self.conn.execute(
                "UPDATE users SET ship_position=?, ship_position_changed_at=? WHERE ship_position IS NULL AND nickname=?",
                ("кок", now_iso(), "arthoriapendragon"),
            )

            # ------------------------------------------------
            # USER FACTS (новая схема памяти)
            # ------------------------------------------------
            #
            # facts_json на users остаётся в схеме как легаси-поле
            # (для отката), но больше не читается: единственный
            # источник правды по фактам — эта таблица.

            await self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    category TEXT DEFAULT 'INFO',
                    text TEXT NOT NULL,
                    norm_text TEXT NOT NULL,
                    importance INTEGER DEFAULT 5,
                    confidence REAL DEFAULT 0.8,
                    negative INTEGER DEFAULT 0,
                    created_at TIMESTAMP,
                    updated_at TIMESTAMP,
                    last_used_at TIMESTAMP,
                    use_count INTEGER DEFAULT 0
                )
                """
            )

            await self.conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_user_facts_dedup
                ON user_facts(user_id, norm_text)
                """
            )

            await self.conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    idx_user_facts_user_importance
                ON user_facts(user_id, importance DESC)
                """
            )

            # ------------------------------------------------
            # WILD HUNT (Дикая Охота)
            # ------------------------------------------------

            await self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hunts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_jid TEXT NOT NULL,
                    status TEXT DEFAULT 'scheduled',
                    start_at TIMESTAMP NOT NULL,
                    registration_opens_at TIMESTAMP NOT NULL,
                    hints_sent INTEGER DEFAULT 0,
                    created_at TIMESTAMP,
                    started_at TIMESTAMP,
                    finished_at TIMESTAMP,
                    created_by TEXT
                )
                """
            )

            # Состояние вражеского флота + пейсинг событий живут
            # прямо на hunts (одна Охота = один морской бой).
            # Названия колонок (hound_count/rider_count) остались
            # от прежней лесной версии, но по смыслу теперь это
            # "число вражеских кораблей" / "их суммарный экипаж".
            # Добавлено позже исходной схемы -> через ALTER, как у users.

            cursor = await self.conn.execute(
                "PRAGMA table_info(hunts)"
            )

            hunt_columns = {
                row["name"]
                for row in await cursor.fetchall()
            }

            hunt_migrations = {
                "hound_count": (
                    f"INTEGER DEFAULT "
                    f"{HUNT_INITIAL_HOUNDS}"
                ),
                "rider_count": (
                    f"INTEGER DEFAULT "
                    f"{HUNT_INITIAL_RIDERS}"
                ),
                "primary_target_user_id": "TEXT",
                "next_event_at": "TIMESTAMP",
                "eliminated_count": "INTEGER DEFAULT 0",
            }

            for (
                name,
                sql_type,
            ) in hunt_migrations.items():

                if name not in hunt_columns:
                    await self.conn.execute(
                        f"ALTER TABLE hunts "
                        f"ADD COLUMN {name} {sql_type}"
                    )

            await self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hunt_participants (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    hunt_id INTEGER NOT NULL,
                    user_id TEXT NOT NULL,
                    nickname TEXT,
                    registered_at TIMESTAMP,
                    confirmed INTEGER DEFAULT 0
                )
                """
            )

            await self.conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_hunt_participants_unique
                ON hunt_participants(hunt_id, user_id)
                """
            )

            cursor = await self.conn.execute("PRAGMA table_info(hunt_participants)")
            participant_columns = {row["name"] for row in await cursor.fetchall()}
            if "rewarded" not in participant_columns:
                await self.conn.execute("ALTER TABLE hunt_participants ADD COLUMN rewarded INTEGER DEFAULT 0")

            await self.conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    idx_hunts_room_status
                ON hunts(room_jid, status)
                """
            )

            # Жёсткая гарантия: одна незавершённая Охота на комнату.
            # Старые дубликаты (если были созданы до этой миграции) закрываем,
            # оставляя последнюю запись источником истины.
            await self.conn.execute("""
                UPDATE hunts
                SET status='cancelled'
                WHERE status IN ('scheduled','preparing','active')
                  AND id NOT IN (
                    SELECT MAX(h2.id) FROM hunts h2
                    WHERE h2.room_jid=hunts.room_jid
                      AND h2.status IN ('scheduled','preparing','active')
                  )
            """)
            await self.conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_hunts_one_open_per_room
                ON hunts(room_jid)
                WHERE status IN ('scheduled','preparing','active')
            """)

            # ------------------------------------------------
            # WILD HUNT: PLAYER STATE
            # ------------------------------------------------
            #
            # Состояние конкретного игрока внутри одной Охоты.
            # Поля trail/fatigue/distance/status — наследие старой
            # версии на суше; активный морской боевой цикл ими не
            # пользуется (экипаж делит судьбу своего корабля, а не
            # личную "поимку" — см. HuntMixin._hunt_finish). Поле
            # inventory_json — наоборот, живое: личное снаряжение
            # экипажа, которое реально работает через USE_ITEM.

            await self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hunt_player_state (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    hunt_id INTEGER NOT NULL,
                    user_id TEXT NOT NULL,
                    nickname TEXT,
                    trail INTEGER DEFAULT 30,
                    fatigue INTEGER DEFAULT 0,
                    fear INTEGER DEFAULT 0,
                    distance INTEGER DEFAULT 60,
                    status TEXT DEFAULT 'free',
                    catches INTEGER DEFAULT 0,
                    hound_kills INTEGER DEFAULT 0,
                    helped_count INTEGER DEFAULT 0,
                    was_primary_target INTEGER DEFAULT 0,
                    never_caught INTEGER DEFAULT 1,
                    eliminated_order INTEGER,
                    eliminated_at TIMESTAMP,
                    last_action_at TIMESTAMP,
                    crew_status TEXT DEFAULT 'healthy',
                    created_at TIMESTAMP
                )
                """
            )

            await self.conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_hunt_player_state_unique
                ON hunt_player_state(hunt_id, user_id)
                """
            )

            # Дикая Охота 2.0: локация, инвентарь, HP и "мягкая"
            # память об отношениях с другими игроками — добавлено
            # позже исходной схемы, через тот же ALTER-паттерн, что
            # и у hunts.

            cursor = await self.conn.execute(
                "PRAGMA table_info(hunt_player_state)"
            )

            hunt_player_columns = {
                row["name"]
                for row in await cursor.fetchall()
            }

            hunt_player_migrations = {
                "hunt_role": "TEXT DEFAULT 'crew'",
                "location_key": (
                    f"TEXT DEFAULT "
                    f"'{HUNT_START_LOCATION_KEY}'"
                ),
                "inventory_json": "TEXT DEFAULT '[]'",
                "relationships_json": "TEXT DEFAULT '{}'",
                "hp": (
                    f"INTEGER DEFAULT "
                    f"{HUNT_STARTING_HP}"
                ),
                "crew_status": "TEXT DEFAULT 'healthy'",
                "created_at": "TIMESTAMP",
            }

            for (
                name,
                sql_type,
            ) in hunt_player_migrations.items():

                if name not in hunt_player_columns:
                    await self.conn.execute(
                        f"ALTER TABLE hunt_player_state "
                        f"ADD COLUMN {name} {sql_type}"
                    )

            await self.conn.execute(
                "UPDATE hunt_player_state SET created_at=COALESCE(created_at,last_action_at) "
                "WHERE created_at IS NULL"
            )

            # ------------------------------------------------
            # WILD HUNT 2.0: WORLD STATE
            # ------------------------------------------------
            #
            # Одна строка на Охоту: погода/время/регион/уровень
            # опасности. Локации и факты живут в отдельных
            # append-only таблицах ниже.

            await self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hunt_world (
                    hunt_id INTEGER PRIMARY KEY,
                    region_name TEXT,
                    weather TEXT,
                    world_time TEXT,
                    danger_level INTEGER DEFAULT 2,
                    updated_at TIMESTAMP
                )
                """
            )

            await self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hunt_locations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    hunt_id INTEGER NOT NULL,
                    location_key TEXT NOT NULL,
                    title TEXT,
                    description TEXT,
                    exits_json TEXT DEFAULT '[]',
                    tags_json TEXT DEFAULT '[]',
                    created_at TIMESTAMP
                )
                """
            )

            await self.conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_hunt_locations_unique
                ON hunt_locations(hunt_id, location_key)
                """
            )

            # Мировая память: append-only. "Изменение" факта — это
            # новая строка с тем же fact_key; при чтении берётся
            # только последняя строка на каждый fact_key, так что
            # LLM физически не может увидеть противоречие.

            await self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS hunt_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    hunt_id INTEGER NOT NULL,
                    location_key TEXT,
                    fact_key TEXT,
                    fact_text TEXT NOT NULL,
                    visibility TEXT DEFAULT 'public',
                    created_by TEXT,
                    created_at TIMESTAMP
                )
                """
            )

            await self.conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                    idx_hunt_facts_lookup
                ON hunt_facts(hunt_id, fact_key, id)
                """
            )


            # ------------------------------------------------
            # SEA HUNT 3.0: SHIPS / NPCs / WEAPONS / BOARDING / LOG
            # ------------------------------------------------
            # Фактическое состояние боя хранится нормализованно.
            # JSON используется только для входных/описательных данных.
            await self.conn.execute("""
                CREATE TABLE IF NOT EXISTS hunt_ships (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    hunt_id INTEGER NOT NULL,
                    side TEXT NOT NULL,
                    name TEXT NOT NULL,
                    class_name TEXT,
                    hp INTEGER NOT NULL,
                    max_hp INTEGER NOT NULL,
                    sails INTEGER DEFAULT 100,
                    crew_morale INTEGER DEFAULT 70,
                    crew_count INTEGER DEFAULT 0,
                    crew_hp INTEGER DEFAULT 100,
                    crew_max_hp INTEGER DEFAULT 100,
                    registered_crew_count INTEGER DEFAULT 0,
                    auxiliary_crew_count INTEGER DEFAULT 0,
                    aux_total INTEGER DEFAULT 0,
                    aux_healthy INTEGER DEFAULT 0,
                    aux_wounded INTEGER DEFAULT 0,
                    aux_unconscious INTEGER DEFAULT 0,
                    aux_dead INTEGER DEFAULT 0,
                    distance INTEGER DEFAULT 50,
                    speed INTEGER DEFAULT 40,
                    heading INTEGER DEFAULT 0,
                    fire_level INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'afloat',
                    personality TEXT,
                    tactics TEXT,
                    ai_json TEXT DEFAULT '{}',
                    created_at TIMESTAMP
                )
            """)
            cursor = await self.conn.execute("PRAGMA table_info(hunt_ships)")
            ship_columns = {row["name"] for row in await cursor.fetchall()}

            ship_migrations = {
                "crew_hp": "INTEGER DEFAULT 100",
                "crew_max_hp": "INTEGER DEFAULT 100",
                "registered_crew_count": "INTEGER DEFAULT 0",
                "auxiliary_crew_count": "INTEGER DEFAULT 0",
                "aux_total": "INTEGER DEFAULT 0",
                "aux_healthy": "INTEGER DEFAULT 0",
                "aux_wounded": "INTEGER DEFAULT 0",
                "aux_unconscious": "INTEGER DEFAULT 0",
                "aux_dead": "INTEGER DEFAULT 0",
                "fire_level": "INTEGER DEFAULT 0",
            }

            for name, sql_type in ship_migrations.items():
                if name not in ship_columns:
                    await self.conn.execute(
                        f"ALTER TABLE hunt_ships ADD COLUMN {name} {sql_type}"
                    )
            await self.conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_hunt_ships_hunt_side
                ON hunt_ships(hunt_id, side, status)
            """)
            await self.conn.execute("""
                CREATE TABLE IF NOT EXISTS hunt_crew_assignments (
                    hunt_id INTEGER NOT NULL,
                    user_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    assigned_by TEXT,
                    updated_at TIMESTAMP,
                    PRIMARY KEY (hunt_id, user_id)
                )
            """)
            await self.conn.execute("""
                CREATE TABLE IF NOT EXISTS hunt_action_gate (
                    hunt_id INTEGER PRIMARY KEY,
                    last_action_at TIMESTAMP,
                    actor_id TEXT
                )
            """)
            await self.conn.execute("""
                CREATE TABLE IF NOT EXISTS hunt_npcs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    hunt_id INTEGER NOT NULL,
                    ship_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    hp INTEGER DEFAULT 80,
                    status TEXT DEFAULT 'alive',
                    personality TEXT,
                    traits_json TEXT DEFAULT '[]',
                    ai_json TEXT DEFAULT '{}',
                    created_at TIMESTAMP
                )
            """)
            await self.conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_hunt_npcs_ship
                ON hunt_npcs(hunt_id, ship_id, status)
            """)
            await self.conn.execute("""
                CREATE TABLE IF NOT EXISTS hunt_weapons (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    hunt_id INTEGER NOT NULL,
                    ship_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    side TEXT,
                    damage INTEGER DEFAULT 70,
                    range INTEGER DEFAULT 70,
                    ammo INTEGER DEFAULT 6,
                    ammo_type TEXT DEFAULT 'cannonball',
                    reload INTEGER DEFAULT 45,
                    ready_at TIMESTAMP,
                    status TEXT DEFAULT 'ready'
                )
            """)
            cur = await self.conn.execute("PRAGMA table_info(hunt_weapons)")
            weapon_columns = {row["name"] for row in await cur.fetchall()}
            if "ammo_type" not in weapon_columns:
                await self.conn.execute("ALTER TABLE hunt_weapons ADD COLUMN ammo_type TEXT DEFAULT 'cannonball'")

            await self.conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_hunt_weapons_ship
                ON hunt_weapons(hunt_id, ship_id)
            """)
            await self.conn.execute("""
                CREATE TABLE IF NOT EXISTS hunt_boardings (
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
            await self.conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_hunt_boardings_hunt
                ON hunt_boardings(hunt_id, status)
            """)
            await self.conn.execute("""
                DELETE FROM hunt_boardings
                WHERE status='in_progress' AND id NOT IN (
                    SELECT MIN(id) FROM hunt_boardings
                    WHERE status='in_progress'
                    GROUP BY hunt_id, defender_ship_id
                )
            """)
            await self.conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_hunt_boarding_active_target
                ON hunt_boardings(hunt_id, defender_ship_id)
                WHERE status='in_progress'
            """)
            await self.conn.execute("""
                CREATE TABLE IF NOT EXISTS hunt_action_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    hunt_id INTEGER NOT NULL,
                    actor_type TEXT NOT NULL,
                    actor_id TEXT,
                    entity_type TEXT,
                    action TEXT NOT NULL,
                    payload_json TEXT DEFAULT '{}',
                    result_json TEXT DEFAULT '{}',
                    created_at TIMESTAMP
                )
            """)
            await self.conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_hunt_action_log_hunt
                ON hunt_action_log(hunt_id, id)
            """)
            await self.conn.execute("""
                CREATE TABLE IF NOT EXISTS hunt_ship_inventory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    hunt_id INTEGER NOT NULL,
                    item_type TEXT NOT NULL,
                    item_name TEXT NOT NULL,
                    quantity INTEGER DEFAULT 0,
                    effects_json TEXT DEFAULT '{}',
                    created_at TIMESTAMP,
                    updated_at TIMESTAMP
                )
            """)
            await self.conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_hunt_ship_inventory_unique
                ON hunt_ship_inventory(hunt_id, item_type)
            """)

            await self.conn.commit()

            # ------------------------------------------------
            # SYSTEM STATE
            # ------------------------------------------------

            await self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS system_state (
                    key TEXT PRIMARY KEY,
                    value REAL
                )
                """
            )

            await self.conn.execute(
                """
                INSERT OR IGNORE INTO system_state
                    (key, value)
                VALUES
                    ('bank_balance', ?)
                """,
                (INITIAL_BANK_BALANCE,),
            )

            await self.conn.execute(
                """
                INSERT OR IGNORE INTO system_state
                    (key, value)
                VALUES
                    ('inflation_multiplier', 1.0)
                """
            )

            await self.conn.execute(
                """
                INSERT OR IGNORE INTO system_state
                    (key, value)
                VALUES
                    ('last_inflation_ts', ?)
                """,
                (time.time(),),
            )

            await self.conn.execute(
                """
                INSERT OR IGNORE INTO system_state
                    (key, value)
                VALUES
                    ('drunk_level', 0)
                """
            )

            await self.conn.execute(
                """
                INSERT OR IGNORE INTO system_state
                    (key, value)
                VALUES
                    ('last_drunk_decay_ts', ?)
                """,
                (time.time(),),
            )

            # ------------------------------------------------
            # МОДЕРАЦИЯ: вайтлист по bare JID (!вайтлист, см.
            # commands.py/moderation.py) — переживает перезапуск,
            # в отличие от mod_fixed_jids/mod_fixed_nicks в
            # moderation.py, которые только в памяти процесса.
            # added_by хранит bare JID того, кто добавил запись
            # (см. MODERATION_WHITELIST_ADMIN_JIDS в config.py) —
            # не ник, ник ничего не доказывает задним числом.
            # ------------------------------------------------

            await self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS moderation_whitelist (
                    jid TEXT PRIMARY KEY,
                    added_by TEXT,
                    added_at REAL
                )
                """
            )

            # ------------------------------------------------
            # MARKET
            # ------------------------------------------------

            await self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS market_lots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_name TEXT NOT NULL,
                    full_info TEXT NOT NULL,
                    base_price REAL NOT NULL,
                    multiplier REAL DEFAULT 1.0,
                    status TEXT DEFAULT 'active',
                    owner TEXT,
                    investments_json TEXT DEFAULT '{}',
                    created_at TIMESTAMP
                )
                """
            )

            # Старые БД могли не иметь времени создания лота.
            cursor = await self.conn.execute("PRAGMA table_info(market_lots)")
            market_columns = {row["name"] for row in await cursor.fetchall()}
            if "created_at" not in market_columns:
                await self.conn.execute(
                    "ALTER TABLE market_lots ADD COLUMN created_at TIMESTAMP"
                )
            await self.conn.execute(
                "UPDATE market_lots SET created_at=? WHERE created_at IS NULL",
                (now_iso(),),
            )

            # ------------------------------------------------
            # INVESTMENTS
            # ------------------------------------------------

            await self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS lot_investments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lot_id INTEGER NOT NULL,
                    user_id TEXT NOT NULL,
                    amount REAL NOT NULL DEFAULT 0,
                    created_at TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP NOT NULL,
                    status TEXT DEFAULT 'active',
                    UNIQUE(lot_id, user_id),
                    FOREIGN KEY(lot_id)
                        REFERENCES market_lots(id)
                )
                """
            )

            await self.conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_lot_investments_user
                ON lot_investments(user_id, status)
                """
            )

            await self.conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_lot_investments_lot
                ON lot_investments(lot_id, status)
                """
            )

            # ------------------------------------------------
            # ROOM PRESENCE SNAPSHOTS
            # ------------------------------------------------
            # Исторические снимки присутствия в MUC. Каждый снимок
            # содержит JSON-массив участников на конкретный момент.
            # Храним последние 100 снимков — этого достаточно, чтобы
            # восстановить, кто был в комнате раньше, даже если он
            # уже вышел к моменту текущего сообщения.
            await self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS room_presence_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    room_jid TEXT NOT NULL,
                    captured_at TIMESTAMP NOT NULL,
                    users_json TEXT NOT NULL
                )
                """
            )

            await self.conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_presence_snapshots_time
                ON room_presence_snapshots(room_jid, captured_at DESC)
                """
            )

            # ------------------------------------------------
            # TRANSACTION HISTORY
            # ------------------------------------------------

            await self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT,
                    amount REAL NOT NULL,
                    balance_after REAL,
                    type TEXT NOT NULL,
                    description TEXT,
                    lot_id INTEGER,
                    related_user TEXT,
                    created_at TIMESTAMP NOT NULL
                )
                """
            )

            await self.conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_transactions_user
                ON transactions(user_id, id DESC)
                """
            )

            await self.conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_transactions_lot
                ON transactions(lot_id, id DESC)
                """
            )

            # ------------------------------------------------
            # MIGRATE OLD JSON INVESTMENTS
            # ------------------------------------------------

            await self._migrate_old_investments_locked()

            # ------------------------------------------------
            # MIGRATE OLD facts_json -> user_facts
            # ------------------------------------------------

            await self._migrate_legacy_facts_locked()

            await self.conn.commit()

    async def _migrate_legacy_facts_locked(self):
        """
        Разовый перенос facts_json (старая схема) в user_facts
        (новая схема с весами/confidence/cooldown).

        Помечает users.facts_migrated = 1, чтобы не сканировать
        facts_json заново при каждом старте. facts_json не
        удаляется — это чистый бэкап на случай отката.
        """

        cursor = await self.conn.execute(
            """
            SELECT user_id, facts_json
            FROM users
            WHERE
                (facts_migrated IS NULL OR facts_migrated != 1)
                AND facts_json IS NOT NULL
                AND facts_json != '[]'
            """
        )

        rows = await cursor.fetchall()

        for row in rows:

            legacy_facts = safe_json_loads(
                row["facts_json"],
                [],
            )

            if isinstance(legacy_facts, list) and legacy_facts:

                clean = self.sanitize_facts(
                    legacy_facts
                )

                if clean:
                    await self._upsert_facts_nolock(
                        row["user_id"],
                        clean,
                    )

        await self.conn.execute(
            """
            UPDATE users
            SET facts_migrated = 1
            WHERE facts_migrated IS NULL OR facts_migrated != 1
            """
        )

        if rows:
            logging.info(
                "[ПАМЯТЬ] Перенесено пользователей "
                "из facts_json в user_facts: %d",
                len(rows),
            )

    async def _migrate_old_investments_locked(self):
        """
        Переносит старые investments_json в lot_investments.

        Не удаляет investments_json, чтобы не ломать старую БД.
        """

        cursor = await self.conn.execute(
            """
            SELECT id, investments_json
            FROM market_lots
            WHERE investments_json IS NOT NULL
              AND investments_json != '{}'
            """
        )

        lots = await cursor.fetchall()

        for lot in lots:
            investments = safe_json_loads(
                lot["investments_json"],
                {},
            )

            if not isinstance(investments, dict):
                continue

            for user_id, amount in investments.items():

                try:
                    amount = float(amount)
                except (
                    ValueError,
                    TypeError,
                ):
                    continue

                if amount <= 0:
                    continue

                existing = await self.conn.execute(
                    """
                    SELECT id
                    FROM lot_investments
                    WHERE lot_id = ?
                      AND user_id = ?
                    """,
                    (
                        int(lot["id"]),
                        str(user_id),
                    ),
                )

                row = await existing.fetchone()

                if row:
                    continue

                timestamp = now_iso()

                await self.conn.execute(
                    """
                    INSERT INTO lot_investments
                        (
                            lot_id,
                            user_id,
                            amount,
                            created_at,
                            updated_at,
                            status
                        )
                    VALUES (?, ?, ?, ?, ?, 'active')
                    """,
                    (
                        int(lot["id"]),
                        str(user_id),
                        amount,
                        timestamp,
                        timestamp,
                    ),
                )

        logging.info(
            "[ЭКОНОМИКА] Проверена миграция старых инвестиций."
        )

    # ========================================================
    # МОДЕРАЦИЯ: ВАЙТЛИСТ (bare JID)
    # ========================================================

    async def moderation_whitelist_add(self, jid, added_by):
        jid = str(jid).strip().casefold()
        async with self.write_lock:
            await self.conn.execute(
                """
                INSERT INTO moderation_whitelist (jid, added_by, added_at)
                VALUES (?, ?, ?)
                ON CONFLICT(jid) DO UPDATE SET
                    added_by = excluded.added_by,
                    added_at = excluded.added_at
                """,
                (jid, str(added_by or "").strip().casefold(), time.time()),
            )
            await self.conn.commit()

    async def moderation_whitelist_remove(self, jid):
        jid = str(jid).strip().casefold()
        async with self.write_lock:
            cursor = await self.conn.execute(
                "DELETE FROM moderation_whitelist WHERE jid = ?",
                (jid,),
            )
            await self.conn.commit()
            return cursor.rowcount > 0

    async def moderation_whitelist_list(self):
        cursor = await self.conn.execute(
            """
            SELECT jid, added_by, added_at
            FROM moderation_whitelist
            ORDER BY added_at
            """
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ========================================================
    # USERS
    # ========================================================

    async def ensure_user(self, user_id):
        async with self.write_lock:
            await self.conn.execute(
                """
                INSERT OR IGNORE INTO users
                    (
                        user_id,
                        nickname,
                        facts_json,
                        last_updated,
                        balance,
                        reputation
                    )
                VALUES (?, NULL, '[]', ?, 0, 0)
                """,
                (
                    user_id,
                    now_iso(),
                ),
            )

            await self.conn.commit()

    async def get_user_info(self, user_id):
        """
        Лёгкая карточка пользователя: кличка + репутация.

        Факты сюда больше не входят — за них отвечают
        get_user_facts() / select_memory_context(), чтобы
        нигде в коде случайно не всплыл "дамп" всех фактов.
        """

        cursor = await self.conn.execute(
            """
            SELECT nickname, reputation
            FROM users
            WHERE user_id = ?
            """,
            (user_id,),
        )

        row = await cursor.fetchone()

        if not row:
            return None, 0

        reputation = (
            row["reputation"]
            if row["reputation"] is not None
            else 0
        )

        return (
            row["nickname"],
            int(reputation),
        )

    async def get_user_dossier(self, user_id):
        cursor = await self.conn.execute(
            """
            SELECT
                nickname,
                balance,
                respect_until,
                reputation,
                ship_position
            FROM users
            WHERE user_id = ?
            """,
            (user_id,),
        )

        row = await cursor.fetchone()

        if not row:
            return {
                "nickname": None,
                "balance": 0.0,
                "respect_active": False,
                "reputation": 0,
                "ship_position": None,
                "title": self.get_reputation_title(0),
                "facts": [],
            }

        respect_active = False

        if row["respect_until"]:
            try:
                respect_active = (
                    datetime.fromisoformat(
                        str(row["respect_until"])
                    )
                    > datetime.now()
                )
            except (
                ValueError,
                TypeError,
            ):
                pass

        reputation = int(
            row["reputation"] or 0
        )

        # Explicit Memory: пользователь сам спросил "что ты помнишь" —
        # тут можно (в пределах лимита) отдать честный полный список,
        # а не взвешенную случайную выборку.
        facts = await self.get_user_facts(
            user_id,
            limit=EXPLICIT_MEMORY_LIMIT,
        )

        return {
            "nickname": row["nickname"],
            "balance": round(
                float(row["balance"] or 0),
                2,
            ),
            "respect_active": respect_active,
            "reputation": reputation,
            "ship_position": row["ship_position"],
            "title": self.get_reputation_title(
                reputation
            ),
            "facts": facts,
        }

    async def update_nickname(
        self,
        user_id,
        nickname,
    ):
        nickname = str(nickname).strip()

        nickname = re.sub(
            r"\s+",
            " ",
            nickname,
        )[:30]

        if not nickname:
            return False

        timestamp = now_iso()

        async with self.write_lock:
            await self.conn.execute(
                """
                INSERT INTO users
                    (
                        user_id,
                        nickname,
                        facts_json,
                        last_updated,
                        balance,
                        reputation,
                        last_nick_update
                    )
                VALUES (?, ?, '[]', ?, 0, 0, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    nickname = excluded.nickname,
                    last_updated = excluded.last_updated,
                    last_nick_update = excluded.last_nick_update
                """,
                (
                    user_id,
                    nickname,
                    timestamp,
                    timestamp,
                ),
            )

            await self.conn.commit()

        return True

    async def touch_nick_update(self, user_id, when=None):
        """
        Обновляет только "дату последней проверки клички", не
        трогая саму кличку — используется, когда Пёс решил НЕ
        выдавать новую кличку, чтобы фоновый цикл не дёргал
        этого пользователя снова раньше следующего интервала.
        """
        timestamp = when or now_iso()

        async with self.write_lock:
            await self.conn.execute(
                """
                UPDATE users
                SET last_nick_update = ?
                WHERE user_id = ?
                """,
                (
                    timestamp,
                    user_id,
                ),
            )

            await self.conn.commit()

    async def get_nickname_check_candidates(self, limit=NICKNAME_BATCH_SIZE):
        """
        Возвращает до `limit` согласившихся на обработку данных
        пользователей, у которых давно (или ни разу) не
        проверялась кличка — самые "просроченные" первыми.
        Дозирует нагрузку на LLM: вызывающий код сам решает,
        кому из кандидатов уже реально пора (см.
        NICKNAME_MIN/MAX_INTERVAL_DAYS в config.py).
        """
        cursor = await self.conn.execute(
            """
            SELECT user_id, nickname, reputation, last_nick_update
            FROM users
            WHERE consent_at IS NOT NULL
            ORDER BY
                CASE WHEN last_nick_update IS NULL THEN 0 ELSE 1 END,
                last_nick_update ASC
            LIMIT ?
            """,
            (int(limit),),
        )

        rows = await cursor.fetchall()

        return [dict(row) for row in rows]

    # ========================================================
    # REPUTATION
    # ========================================================

    @staticmethod
    def get_reputation_title(rep):
        if rep <= -300:
            return "🤬 Враг народа"

        if rep <= -100:
            return "😠 Подозрительный"

        if rep < 100:
            return "😐 Нейтрал"

        if rep < 300:
            return "🙂 Приятель"

        return "👑 Авторитет"

    async def get_reputation(self, user_id):
        _, rep = await self.get_user_info(
            user_id
        )

        return rep

    async def add_reputation(
        self,
        user_id,
        amount,
    ):
        amount = int(amount)

        amount = clamp(
            amount,
            -30,
            30,
        )

        async with self.write_lock:

            cursor = await self.conn.execute(
                """
                SELECT reputation
                FROM users
                WHERE user_id = ?
                """,
                (user_id,),
            )

            row = await cursor.fetchone()

            current = (
                int(row["reputation"] or 0)
                if row
                else 0
            )

            new_rep = clamp(
                current + amount,
                -500,
                500,
            )

            await self.conn.execute(
                """
                INSERT INTO users
                    (
                        user_id,
                        facts_json,
                        last_updated,
                        reputation
                    )
                VALUES (?, '[]', ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    reputation = excluded.reputation,
                    last_updated = excluded.last_updated
                """,
                (
                    user_id,
                    now_iso(),
                    new_rep,
                ),
            )

            await self.conn.commit()

        return new_rep

    # ========================================================
    # RESPECT
    # ========================================================

    async def set_respect(
        self,
        user_id,
        days,
    ):
        until = (
            datetime.now()
            + timedelta(days=days)
        )

        async with self.write_lock:
            await self.conn.execute(
                """
                INSERT INTO users
                    (
                        user_id,
                        facts_json,
                        last_updated,
                        respect_until
                    )
                VALUES (?, '[]', ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    respect_until = excluded.respect_until,
                    last_updated = excluded.last_updated
                """,
                (
                    user_id,
                    now_iso(),
                    until.isoformat(),
                ),
            )

            await self.conn.commit()

    async def get_respect_status(
        self,
        user_id,
    ):
        cursor = await self.conn.execute(
            """
            SELECT respect_until
            FROM users
            WHERE user_id = ?
            """,
            (user_id,),
        )

        row = await cursor.fetchone()

        if not row or not row["respect_until"]:
            return False

        try:
            return (
                datetime.fromisoformat(
                    str(row["respect_until"])
                )
                > datetime.now()
            )
        except (
            ValueError,
            TypeError,
        ):
            return False

    # ========================================================
    # ОПЬЯНЕНИЕ ПСА (!напоить)
    # ========================================================
    #
    # Глобальное состояние бота (не привязано к конкретному
    # пользователю, в отличие от respect_until выше) — хранится
    # в system_state ключом 'drunk_level', как bank_balance и
    # inflation_multiplier.

    async def get_drunk_level(self):
        cursor = await self.conn.execute(
            """
            SELECT value
            FROM system_state
            WHERE key = 'drunk_level'
            """
        )

        row = await cursor.fetchone()

        if not row or row["value"] is None:
            return 0

        return int(
            clamp(
                float(row["value"]),
                0,
                MAX_DRUNK_LEVEL,
            )
        )

    async def add_drunk_level(
        self,
        step=1,
    ):
        """
        Атомарно сдвигает стадию опьянения на `step`
        (отрицательный step — трезвеет), в границах
        0..MAX_DRUNK_LEVEL.

        Возвращает новую стадию.
        """

        async with self.write_lock:

            cursor = await self.conn.execute(
                """
                SELECT value
                FROM system_state
                WHERE key = 'drunk_level'
                """
            )

            row = await cursor.fetchone()

            current = (
                int(row["value"])
                if row and row["value"] is not None
                else 0
            )

            new_level = max(
                0,
                min(
                    current + step,
                    MAX_DRUNK_LEVEL,
                ),
            )

            await self.conn.execute(
                """
                INSERT INTO system_state
                    (key, value)
                VALUES
                    ('drunk_level', ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value
                """,
                (new_level,),
            )

            await self.conn.commit()

            return new_level

    async def get_last_drunk_decay_ts(self):
        cursor = await self.conn.execute(
            """
            SELECT value
            FROM system_state
            WHERE key = 'last_drunk_decay_ts'
            """
        )

        row = await cursor.fetchone()

        if not row or row["value"] is None:
            return time.time()

        return float(row["value"])

    async def set_last_drunk_decay_ts(
        self,
        ts,
    ):
        async with self.write_lock:
            await self.conn.execute(
                """
                INSERT INTO system_state
                    (key, value)
                VALUES
                    ('last_drunk_decay_ts', ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value
                """,
                (ts,),
            )

            await self.conn.commit()

    async def check_and_apply_drunk_decay(self):
        """
        Раз в DRUNK_DECAY_HOURS часов Пёс трезвеет на 1
        стадию. Вызывается из фонового цикла раз в минуту
        (см. drunk_decay_loop в core.py) — сам решает,
        прошло ли достаточно времени.
        """

        now = time.time()

        last = (
            await self.get_last_drunk_decay_ts()
        )

        if (
            now - last
            < DRUNK_DECAY_INTERVAL_SECONDS
        ):
            return False

        steps = int(
            (now - last)
            // DRUNK_DECAY_INTERVAL_SECONDS
        )

        if steps <= 0:
            return False

        level = (
            await self.get_drunk_level()
        )

        if level <= 0:
            # Уже трезвый — просто сдвигаем часы,
            # чтобы не копить "долг" отрезвления.
            await self.set_last_drunk_decay_ts(
                now
            )
            return False

        await self.add_drunk_level(-steps)

        await self.set_last_drunk_decay_ts(
            now
        )

        return True

    async def apply_missed_drunk_decay(self):
        """
        Догоняющее отрезвление на случай, если бот был
        офлайн дольше DRUNK_DECAY_HOURS — вызывается один
        раз при входе в комнату (см. core.py), аналогично
        apply_missed_inflation().
        """

        await self.check_and_apply_drunk_decay()

    # ========================================================
    # FACTS
    # ========================================================
    #
    # Схема памяти (см. доку "Пёс, реально меня помнит"):
    #
    #   БД (до 500 фактов/пользователя)
    #        │
    #        ▼
    #   select_memory_context()  -- НЕ дампит всё в LLM
    #        │
    #        ├── relevant (по теме сообщения, через search_facts)
    #        ├── ambient (1-3 взвешенно-случайных)
    #        └── негативные факты — приоритетно, если релевантны
    #        │
    #        ▼
    #   2-5 фактов -> LLM
    #
    # Explicit-режим (пользователь сам спросил "!досье") идёт
    # в обход селектора через get_user_facts(limit=...).

    @staticmethod
    def _fact_row_to_dict(row):
        created_at = row["created_at"] or ""

        return {
            "id": row["id"],
            "fact": row["text"],
            "category": row["category"] or "INFO",
            "importance": int(
                row["importance"]
                if row["importance"] is not None
                else 5
            ),
            "confidence": float(
                row["confidence"]
                if row["confidence"] is not None
                else 0.8
            ),
            "negative": bool(
                row["negative"]
            ),
            "date": str(created_at)[:10]
            or "?",
            "last_used_at": row["last_used_at"],
            "use_count": int(
                row["use_count"] or 0
            ),
        }

    @staticmethod
    def _extract_keywords(text):
        if not text:
            return set()

        tokens = re.findall(
            r"[\w\-]+",
            str(text).casefold(),
        )

        return {
            token
            for token in tokens
            if len(token) >= 3
            and token not in MEMORY_STOPWORDS
        }

    async def cleanup_old_facts(
        self,
        days=90,
    ):
        """
        Чистит только рядовые старые факты. Отрицательные
        (границы/просьбы пользователя) и очень важные (>=9)
        не трогает — они не должны "протухать".
        """

        cutoff = (
            datetime.now()
            - timedelta(days=days)
        )

        cutoff_date = cutoff.strftime(
            "%Y-%m-%d"
        )

        async with self.write_lock:
            cursor = await self.conn.execute(
                """
                DELETE FROM user_facts
                WHERE
                    created_at < ?
                    AND negative = 0
                    AND importance < 9
                """,
                (cutoff_date,),
            )

            await self.conn.commit()

        deleted = (
            cursor.rowcount
            if cursor.rowcount and cursor.rowcount > 0
            else 0
        )

        if deleted:
            logging.info(
                "[ПАМЯТЬ] Очищено старых фактов: %d",
                deleted,
            )

    def sanitize_facts(self, facts):
        if not isinstance(facts, list):
            return []

        result = []

        for fact in facts:

            if not isinstance(
                fact,
                dict,
            ):
                continue

            text = str(
                fact.get(
                    "fact",
                    "",
                )
            ).strip()

            if not text:
                continue

            lower = text.casefold()

            if any(
                marker in lower
                for marker in SECRET_MARKERS
            ):
                logging.warning(
                    "[ПАМЯТЬ] Заблокирован потенциальный секрет: %s",
                    text[:120],
                )
                continue

            if contains_high_entropy_secret(text):
                logging.warning(
                    "[ПАМЯТЬ] Заблокирован высокоэнтропийный "
                    "токен в факте: %s",
                    text[:120],
                )
                continue

            category = str(
                fact.get(
                    "category",
                    "INFO",
                )
            ).upper().strip()

            if category in FORBIDDEN_FACT_CATEGORIES:
                logging.warning(
                    "[ПАМЯТЬ] Заблокирована секретная категория: %s",
                    category,
                )
                continue

            if category not in FACT_CATEGORIES:
                category = "INFO"

            try:
                importance = int(
                    fact.get(
                        "importance",
                        5,
                    )
                )
            except (
                ValueError,
                TypeError,
            ):
                importance = 5

            importance = clamp(
                importance,
                1,
                10,
            )

            try:
                confidence = float(
                    fact.get(
                        "confidence",
                        0.8,
                    )
                )
            except (
                ValueError,
                TypeError,
            ):
                confidence = 0.8

            confidence = clamp(
                confidence,
                0.0,
                1.0,
            )

            negative = bool(
                fact.get(
                    "negative",
                    False,
                )
            )

            result.append(
                {
                    "fact": text[:150],
                    "category": category,
                    "importance": importance,
                    "confidence": confidence,
                    "negative": negative,
                }
            )

        return result

    async def _upsert_facts_nolock(
        self,
        user_id,
        clean_facts,
    ):
        """
        Вставляет/обновляет факты в user_facts.

        ВАЖНО: требует, чтобы self.write_lock уже был захвачен
        вызывающим кодом (используется и из add_facts(), и из
        миграции при старте).
        """

        now = now_iso()

        for fact in clean_facts:

            norm = normalize_answer(
                fact["fact"]
            )

            if not norm:
                continue

            await self.conn.execute(
                """
                INSERT INTO user_facts (
                    user_id, category, text, norm_text,
                    importance, confidence, negative,
                    created_at, updated_at, use_count
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(user_id, norm_text) DO UPDATE SET
                    category = CASE
                        WHEN excluded.importance >= user_facts.importance
                        THEN excluded.category
                        ELSE user_facts.category
                    END,
                    text = CASE
                        WHEN excluded.importance >= user_facts.importance
                        THEN excluded.text
                        ELSE user_facts.text
                    END,
                    importance = MAX(
                        user_facts.importance,
                        excluded.importance
                    ),
                    confidence = excluded.confidence,
                    negative = excluded.negative OR user_facts.negative,
                    updated_at = excluded.updated_at
                """,
                (
                    user_id,
                    fact["category"],
                    fact["fact"],
                    norm,
                    fact["importance"],
                    fact["confidence"],
                    int(fact["negative"]),
                    now,
                    now,
                ),
            )

    async def _enforce_fact_cap_nolock(
        self,
        user_id,
        max_facts,
    ):
        """
        Держит не больше max_facts фактов на пользователя.
        Отрицательные факты и более важные выживают первыми.
        """

        cursor = await self.conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM user_facts
            WHERE user_id = ?
            """,
            (user_id,),
        )

        row = await cursor.fetchone()

        count = row["c"] if row else 0

        if count <= max_facts:
            return

        await self.conn.execute(
            """
            DELETE FROM user_facts
            WHERE user_id = ? AND id NOT IN (
                SELECT id FROM user_facts
                WHERE user_id = ?
                ORDER BY
                    negative DESC,
                    importance DESC,
                    updated_at DESC
                LIMIT ?
            )
            """,
            (
                user_id,
                user_id,
                max_facts,
            ),
        )

    async def add_facts(
        self,
        user_id,
        new_facts,
        max_facts=None,
    ):
        clean = self.sanitize_facts(
            new_facts
        )

        if not clean:
            return False

        if max_facts is None:
            max_facts = MAX_FACTS_PER_USER

        async with self.write_lock:

            await self._upsert_facts_nolock(
                user_id,
                clean,
            )

            await self._enforce_fact_cap_nolock(
                user_id,
                max_facts,
            )

            await self.conn.commit()

        logging.info(
            "[ПАМЯТЬ] %s: обработано новых фактов: %d",
            user_id,
            len(clean),
        )

        return True

    async def get_user_facts(
        self,
        user_id,
        limit=None,
    ):
        """
        Explicit-режим / служебные нужды: полный (в пределах
        limit) список фактов, отсортированный по важности.

        Не использовать напрямую для промпта LLM в обычном
        разговоре — для этого есть select_memory_context().
        """

        query = """
            SELECT id, category, text, importance, confidence,
                   negative, created_at, updated_at,
                   last_used_at, use_count
            FROM user_facts
            WHERE user_id = ?
            ORDER BY importance DESC, updated_at DESC
        """

        params = [user_id]

        if limit:
            query += " LIMIT ?"
            params.append(int(limit))

        cursor = await self.conn.execute(
            query,
            params,
        )

        rows = await cursor.fetchall()

        return [
            self._fact_row_to_dict(row)
            for row in rows
        ]

    async def search_facts(
        self,
        user_id,
        query,
        limit=10,
    ):
        """
        Доказательный поиск (Explicit/Relevant Memory): ищет факты,
        реально связанные по смыслу с запросом, а не любой факт,
        задевший хотя бы одно ключевое слово.

        Раньше это был чистый OR по LIKE (текст ИЛИ категория
        содержит любое из ключевых слов), а сортировка шла по
        importance/updated_at — то есть релевантность самому запросу
        вообще не участвовала в выборе и в порядке результатов, и
        факт с одним случайным совпадением проходил наравне с фактом,
        совпавшим по всем словам запроса. Плюс сравнение с category
        (название категории, например "TECH") позволяло случайному
        слову-омониму вытянуть факты всей категории.

        Теперь: OR по LIKE (только text) используется исключительно
        как дешёвый SQL-префильтр кандидатов, а реальная релевантность
        (сколько ключевых слов запроса реально встретилось в тексте
        факта) считается в Python — тем же способом, что и в
        select_memory_context() — и факты без единого совпадения
        (relevance == 0) в результат вообще не попадают.
        """

        keywords = self._extract_keywords(
            query
        )

        if not keywords:
            return []

        conditions = []
        params = [user_id]

        for keyword in keywords:
            conditions.append("text LIKE ?")
            params.append(f"%{keyword}%")

        where = " OR ".join(conditions)

        cursor = await self.conn.execute(
            f"""
            SELECT id, category, text, importance, confidence,
                   negative, created_at, updated_at,
                   last_used_at, use_count
            FROM user_facts
            WHERE user_id = ? AND ({where})
            """,
            params,
        )

        rows = await cursor.fetchall()

        scored = []

        for row in rows:

            text_lower = (row["text"] or "").casefold()

            relevance = sum(
                1
                for kw in keywords
                if kw in text_lower
            )

            if relevance <= 0:
                continue

            importance = int(
                row["importance"]
                if row["importance"] is not None
                else 5
            )

            scored.append((relevance, importance, row))

        scored.sort(
            key=lambda item: (
                item[0],
                item[1],
                str(item[2]["updated_at"] or ""),
            ),
            reverse=True,
        )

        return [
            self._fact_row_to_dict(row)
            for _, _, row in scored[: int(limit)]
        ]

    async def select_memory_context(
        self,
        user_id,
        message_text=None,
        min_facts=None,
        max_facts=None,
        category=None,
    ):
        """
        Ambient + Relevant Memory за один проход.

        Не грузит LLM всеми фактами: берёт маленькую взвешенно-
        случайную выборку (2-5 фактов по умолчанию), смещённую
        в сторону темы текущего сообщения, важности, новизны
        (давно не всплывавшие факты) и отрицательных фактов
        (границы/просьбы пользователя — с бонусом к score).

        category (необязательно): ограничить выборку одной
        категорией (PREFERENCE/EVENT/INFO/HOBBY/WORK/TECH/
        PERSONAL) — используется для router-driven mention-facts
        в llm.py, где категорию выбирает LLM-роутер по смыслу
        сообщения, а не берётся "какой факт попадётся".

        Использованные факты помечаются (last_used_at/use_count),
        чтобы у них был cooldown и Пёс не зацикливался на одном
        и том же факте.
        """

        min_facts = (
            min_facts
            if min_facts is not None
            else MEMORY_CONTEXT_MIN_FACTS
        )

        max_facts = (
            max_facts
            if max_facts is not None
            else MEMORY_CONTEXT_MAX_FACTS
        )

        if category:
            cursor = await self.conn.execute(
                """
                SELECT id, category, text, importance, confidence,
                       negative, created_at, updated_at,
                       last_used_at, use_count
                FROM user_facts
                WHERE user_id = ? AND category = ?
                """,
                (user_id, category),
            )
        else:
            cursor = await self.conn.execute(
                """
                SELECT id, category, text, importance, confidence,
                       negative, created_at, updated_at,
                       last_used_at, use_count
                FROM user_facts
                WHERE user_id = ?
                """,
                (user_id,),
            )

        rows = await cursor.fetchall()

        if not rows:
            return []

        keywords = self._extract_keywords(
            message_text
        )

        now = datetime.now()

        scored = []

        for row in rows:

            text_lower = (
                row["text"] or ""
            ).casefold()

            relevance = 0

            if keywords:
                relevance = min(
                    sum(
                        1
                        for kw in keywords
                        if kw in text_lower
                    ),
                    3,
                )

            hours_since_use = None

            if row["last_used_at"]:
                try:
                    last_used = (
                        datetime.fromisoformat(
                            str(row["last_used_at"])
                        )
                    )
                    hours_since_use = (
                        now - last_used
                    ).total_seconds() / 3600
                except (
                    ValueError,
                    TypeError,
                ):
                    hours_since_use = None

            on_cooldown = (
                hours_since_use is not None
                and hours_since_use
                < MEMORY_COOLDOWN_HOURS
            )

            use_count = int(
                row["use_count"] or 0
            )

            novelty = max(
                0,
                3 - use_count,
            )

            importance = int(
                row["importance"]
                if row["importance"] is not None
                else 5
            )

            score = (
                importance
                * MEMORY_WEIGHT_IMPORTANCE
                + novelty * MEMORY_WEIGHT_NOVELTY
                + relevance
                * MEMORY_WEIGHT_RELEVANCE
            )

            if on_cooldown:
                score -= (
                    MEMORY_WEIGHT_RECENT_PENALTY
                )

            if row["negative"]:
                score += MEMORY_NEGATIVE_BONUS

            scored.append(
                (score, relevance, row)
            )

        scored.sort(
            key=lambda item: item[0],
            reverse=True,
        )

        # Отрицательные факты, реально связанные с текущей темой,
        # включаем всегда — это границы/просьбы пользователя.
        forced = {
            row["id"]: row
            for score, relevance, row in scored
            if row["negative"] and relevance > 0
        }

        pool = [
            (score, row)
            for score, relevance, row in scored
            if row["id"] not in forced
        ][:MEMORY_TOP_CANDIDATES]

        target_count = clamp(
            random.randint(
                min_facts,
                max(min_facts, max_facts),
            ),
            len(forced),
            len(rows),
        )

        chosen = dict(forced)

        candidates = [row for _, row in pool]
        weights = [
            max(score, 1)
            for score, _ in pool
        ]

        while (
            len(chosen) < target_count
            and candidates
        ):
            picked = random.choices(
                candidates,
                weights=weights,
                k=1,
            )[0]

            idx = candidates.index(picked)
            candidates.pop(idx)
            weights.pop(idx)

            chosen[picked["id"]] = picked

        chosen_facts = list(chosen.values())

        if chosen_facts:
            now_s = now_iso()

            async with self.write_lock:
                await self.conn.executemany(
                    """
                    UPDATE user_facts
                    SET
                        last_used_at = ?,
                        use_count = use_count + 1
                    WHERE id = ?
                    """,
                    [
                        (now_s, row["id"])
                        for row in chosen_facts
                    ],
                )

                await self.conn.commit()

        return [
            self._fact_row_to_dict(row)
            for row in chosen_facts
        ]

    # ========================================================
    # WILD HUNT (Дикая Охота)
    # ========================================================
    #
    # Одна Охота на комнату одновременно. Жизненный цикл:
    #
    #   scheduled (регистрация открыта, идут намёки)
    #        │  T=0: snapshot зарегистрированных с активной
    #        │       сессией -> confirmed=1
    #        ▼
    #   active (идёт сама Охота)
    #        │  по истечении HUNT_DURATION_MINUTES
    #        ▼
    #   finished
    #
    # cancelled — если админ отменил до старта.

    @staticmethod
    def _hunt_row_to_dict(row):
        return dict(row) if row else None

    async def create_hunt(
        self,
        room_jid,
        start_at,
        created_by,
        created_by_nickname=None,
    ):
        """
        Планирует новую Охоту. Возвращает hunt dict, либо
        None, если в этой комнате уже есть запланированная
        или идущая Охота.
        """

        # Хранится всегда в UTC (см. hunt_now() в config.py). Вызывающая
        # сторона (HuntMixin._parse_hunt_time) уже отдаёт start_at в UTC —
        # normalize здесь на случай других вызывающих.
        if start_at.tzinfo is None:
            start_at = start_at.replace(tzinfo=timezone.utc)
        else:
            start_at = start_at.astimezone(timezone.utc)

        registration_opens_at = (
            start_at
            - timedelta(
                minutes=HUNT_REGISTRATION_MINUTES
            )
        )

        now = now_iso()

        async with self.write_lock:

            # Проверка находится внутри write_lock, а partial UNIQUE index
            # дополнительно защищает от гонок/других процессов.
            cursor_check = await self.conn.execute(
                """
                SELECT * FROM hunts
                WHERE room_jid=? AND status IN ('scheduled','preparing','active')
                ORDER BY id DESC LIMIT 1
                """, (room_jid,)
            )
            if await cursor_check.fetchone():
                return None

            cursor = await self.conn.execute(
                """
                INSERT INTO hunts (
                    room_jid, status, start_at,
                    registration_opens_at, hints_sent,
                    created_at, created_by
                )
                VALUES (?, 'scheduled', ?, ?, 0, ?, ?)
                """,
                (
                    room_jid,
                    start_at.isoformat(
                        timespec="seconds"
                    ),
                    registration_opens_at.isoformat(
                        timespec="seconds"
                    ),
                    now,
                    created_by,
                ),
            )

            hunt_id = cursor.lastrowid

            # Админ, назначивший Охоту, автоматически входит в экипаж.
            # Делаем это в той же транзакции, чтобы не было состояния
            # "Охота создана, а создатель не записан".
            if created_by is not None:
                await self.conn.execute(
                    """
                    INSERT INTO hunt_participants (
                        hunt_id, user_id, nickname, registered_at, confirmed
                    )
                    VALUES (?, ?, ?, ?, 0)
                    ON CONFLICT(hunt_id, user_id) DO UPDATE SET
                        nickname=excluded.nickname
                    """,
                    (hunt_id, str(created_by), created_by_nickname, now),
                )

            await self.conn.commit()

        return await self.get_hunt_by_id(
            hunt_id
        )

    async def get_hunt_by_id(
        self,
        hunt_id,
    ):
        cursor = await self.conn.execute(
            "SELECT * FROM hunts WHERE id = ?",
            (hunt_id,),
        )

        row = await cursor.fetchone()

        return self._hunt_row_to_dict(row)

    async def get_current_hunt(
        self,
        room_jid,
    ):
        """
        Единственная запланированная/идущая Охота в этой
        комнате, если есть (status IN scheduled, preparing, active).
        """

        cursor = await self.conn.execute(
            """
            SELECT * FROM hunts
            WHERE room_jid = ?
                AND status IN ('scheduled', 'preparing', 'active')
            ORDER BY id DESC
            LIMIT 1
            """,
            (room_jid,),
        )

        row = await cursor.fetchone()

        return self._hunt_row_to_dict(row)

    async def get_last_hunt(
        self,
        room_jid,
    ):
        """Последняя Охота в комнате в любом статусе (для !охота, когда всё уже кончилось)."""

        cursor = await self.conn.execute(
            """
            SELECT * FROM hunts
            WHERE room_jid = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (room_jid,),
        )

        row = await cursor.fetchone()

        return self._hunt_row_to_dict(row)

    async def register_hunt_participant(
        self,
        hunt_id,
        user_id,
        nickname,
    ):
        now = now_iso()

        async with self.write_lock:

            await self.conn.execute(
                """
                INSERT INTO hunt_participants (
                    hunt_id, user_id, nickname,
                    registered_at, confirmed
                )
                VALUES (?, ?, ?, ?, 0)
                ON CONFLICT(hunt_id, user_id) DO UPDATE SET
                    nickname = excluded.nickname,
                    registered_at = excluded.registered_at
                """,
                (
                    hunt_id,
                    user_id,
                    nickname,
                    now,
                ),
            )

            await self.conn.commit()

    async def is_hunt_participant(
        self,
        hunt_id,
        user_id,
    ):
        cursor = await self.conn.execute(
            """
            SELECT 1 FROM hunt_participants
            WHERE hunt_id = ? AND user_id = ?
            LIMIT 1
            """,
            (hunt_id, user_id),
        )

        return bool(
            await cursor.fetchone()
        )

    async def get_hunt_participants(
        self,
        hunt_id,
        confirmed_only=False,
    ):
        query = (
            "SELECT * FROM hunt_participants "
            "WHERE hunt_id = ?"
        )

        if confirmed_only:
            query += " AND confirmed = 1"

        cursor = await self.conn.execute(
            query,
            (hunt_id,),
        )

        rows = await cursor.fetchall()

        return [
            dict(row) for row in rows
        ]

    async def mark_hunt_hints_sent(
        self,
        hunt_id,
        count,
    ):
        async with self.write_lock:

            await self.conn.execute(
                """
                UPDATE hunts
                SET hints_sent = ?
                WHERE id = ?
                """,
                (count, hunt_id),
            )

            await self.conn.commit()

    async def snapshot_hunt_start(
        self,
        hunt_id,
        present_user_ids,
    ):
        """Атомарно фиксирует присутствующий экипаж и переводит scheduled -> preparing."""
        async with self.write_lock:
            cur = await self.conn.execute(
                "SELECT status FROM hunts WHERE id=?", (hunt_id,)
            )
            row = await cur.fetchone()
            if not row:
                return []

            status = row["status"]
            if status not in ("scheduled", "preparing", "active"):
                return []

            ids = [str(x) for x in (present_user_ids or [])]
            if ids and status == "scheduled":
                placeholders = ",".join("?" for _ in ids)
                await self.conn.execute(
                    f"UPDATE hunt_participants SET confirmed=1 WHERE hunt_id=? AND user_id IN ({placeholders})",
                    (hunt_id, *ids),
                )

            if status == "scheduled":
                await self.conn.execute(
                    "UPDATE hunts SET status='preparing' WHERE id=? AND status='scheduled'",
                    (hunt_id,),
                )

            cur = await self.conn.execute(
                "SELECT * FROM hunt_participants WHERE hunt_id=? AND confirmed=1 ORDER BY id",
                (hunt_id,),
            )
            confirmed = [dict(r) for r in await cur.fetchall()]
            await self.conn.commit()
            return confirmed

    async def activate_hunt(self, hunt_id):
        """Атомарно переводит preparing -> active после полной подготовки."""
        now = hunt_now().isoformat(timespec="seconds")
        async with self.write_lock:
            cur = await self.conn.execute(
                """UPDATE hunts SET status='active', started_at=COALESCE(started_at, ?)
                   WHERE id=? AND status='preparing'""",
                (now, hunt_id),
            )
            await self.conn.commit()
            return cur.rowcount == 1

    async def finish_hunt(
        self,
        hunt_id,
    ):
        now = hunt_now().isoformat(timespec="seconds")

        async with self.write_lock:
            cur = await self.conn.execute(
                """
                UPDATE hunts
                SET status = 'finished', finished_at = ?
                WHERE id = ? AND status IN ('scheduled','preparing','active')
                """,
                (now, hunt_id),
            )
            await self.conn.commit()
            return cur.rowcount == 1

    async def cancel_hunt(
        self,
        hunt_id,
    ):
        async with self.write_lock:

            await self.conn.execute(
                """
                UPDATE hunts
                SET status = 'cancelled'
                WHERE id = ? AND status IN ('scheduled','preparing')
                """,
                (hunt_id,),
            )

            await self.conn.commit()

    # ========================================================
    # WILD HUNT: PLAYER STATE + ФЛОТ ПРОТИВНИКА
    # ========================================================
    #
    # Большинство полей ниже (trail/fatigue/distance/status/catches)
    # — наследие лесной версии 2.0 и активным морским боевым циклом
    # не читаются (см. dogbot/hunt.py, HuntMixin). Живое поле —
    # inventory_json: реальное личное снаряжение экипажа, которое
    # работает через HuntMixin._hunt_use_item.

    _HUNT_PLAYER_STATE_COLUMNS = {
        "trail",
        "fatigue",
        "fear",
        "distance",
        "status",
        "catches",
        "hound_kills",
        "helped_count",
        "was_primary_target",
        "never_caught",
        "eliminated_order",
        "eliminated_at",
        "last_action_at",
        "location_key",
        "inventory_json",
        "relationships_json",
        "hp",
        "hunt_role",
    }

    _HUNT_PACK_STATE_COLUMNS = {
        "hound_count",
        "rider_count",
        "primary_target_user_id",
        "next_event_at",
        "eliminated_count",
    }

    async def ensure_hunt_player_states(
        self,
        hunt_id,
        participants,
        starting_location_key=HUNT_START_LOCATION_KEY,
        inventories=None,
    ):
        """
        Заводит строку состояния для каждого подтверждённого
        участника (participants: список dict с user_id,
        nickname). Если строка уже есть — не трогает её.

        inventories: опциональный dict user_id -> список уже
        провалидированных (hunt_world.sanitize_inventory)
        предметов. Если для участника записи нет — используется
        случайный резервный комплект.
        """

        inventories = inventories or {}

        async with self.write_lock:

            for participant in participants:

                trail = clamp(
                    HUNT_STARTING_TRAIL
                    + random.randint(-10, 10),
                    0,
                    100,
                )

                distance = clamp(
                    HUNT_STARTING_DISTANCE
                    + random.randint(-10, 10),
                    0,
                    100,
                )

                inventory = inventories.get(
                    participant["user_id"]
                ) or random_starting_inventory()

                await self.conn.execute(
                    """
                    INSERT INTO hunt_player_state (
                        hunt_id, user_id, nickname, trail,
                        fatigue, fear, distance, status,
                        last_action_at, location_key,
                        inventory_json, relationships_json, hp, created_at
                    )
                    VALUES (
                        ?, ?, ?, ?, 0, 0, ?, 'free',
                        NULL, ?, ?, '{}', ?, ?
                    )
                    ON CONFLICT(hunt_id, user_id) DO NOTHING
                    """,
                    (
                        hunt_id,
                        participant["user_id"],
                        participant.get("nickname"),
                        trail,
                        distance,
                        starting_location_key,
                        json.dumps(
                            inventory,
                            ensure_ascii=False,
                        ),
                        HUNT_STARTING_HP,
                        now_iso(),
                    ),
                )

            await self.conn.commit()

    async def get_hunt_player_state(
        self,
        hunt_id,
        user_id,
    ):
        cursor = await self.conn.execute(
            """
            SELECT * FROM hunt_player_state
            WHERE hunt_id = ? AND user_id = ?
            """,
            (hunt_id, user_id),
        )

        row = await cursor.fetchone()

        return dict(row) if row else None

    async def get_hunt_player_state_by_nickname(
        self,
        hunt_id,
        nickname,
    ):
        if not nickname:
            return None

        cursor = await self.conn.execute(
            """
            SELECT * FROM hunt_player_state
            WHERE hunt_id = ?
                AND nickname IS NOT NULL
                AND LOWER(nickname) = LOWER(?)
            LIMIT 1
            """,
            (hunt_id, nickname),
        )

        row = await cursor.fetchone()

        return dict(row) if row else None

    async def get_all_hunt_player_states(
        self,
        hunt_id,
        active_only=False,
    ):
        query = (
            "SELECT * FROM hunt_player_state "
            "WHERE hunt_id = ?"
        )

        if active_only:
            query += (
                " AND status != 'eliminated'"
            )

        cursor = await self.conn.execute(
            query,
            (hunt_id,),
        )

        rows = await cursor.fetchall()

        return [
            dict(row) for row in rows
        ]

    async def try_claim_hunt_action(self, hunt_id, user_id, cooldown_seconds):
        """Атомарно резервирует окно действия игрока. Возвращает (ok, wait_seconds)."""
        now = hunt_now()
        now_text = now.isoformat(timespec='seconds')
        async with self.write_lock:
            cur = await self.conn.execute(
                "SELECT last_action_at FROM hunt_player_state WHERE hunt_id=? AND user_id=?",
                (hunt_id, user_id),
            )
            row = await cur.fetchone()
            if not row:
                return False, 0
            last = row['last_action_at']
            if last:
                try:
                    last_dt = datetime.fromisoformat(last)
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    else:
                        last_dt = last_dt.astimezone(timezone.utc)
                    elapsed = (now - last_dt).total_seconds()
                    remaining = float(cooldown_seconds) - elapsed
                    if remaining > 0:
                        return False, int(remaining + 0.999)
                except (TypeError, ValueError):
                    pass
            await self.conn.execute(
                "UPDATE hunt_player_state SET last_action_at=? WHERE hunt_id=? AND user_id=?",
                (now_text, hunt_id, user_id),
            )
            await self.conn.commit()
            return True, 0

    async def award_hunt_reward_once(self, hunt_id, user_id, coins, rep, description="Награда за морскую «Дикую Охоту»"):
        """Атомарно выдаёт награду ровно один раз для участника Охоты."""
        coins = max(0.0, float(coins))
        rep = int(rep)
        async with self.write_lock:
            cur = await self.conn.execute(
                "SELECT rewarded FROM hunt_participants WHERE hunt_id=? AND user_id=?",
                (hunt_id, user_id),
            )
            row = await cur.fetchone()
            if not row or int(row["rewarded"] or 0):
                return 0.0, False

            bank_cur = await self.conn.execute("SELECT value FROM system_state WHERE key='bank_balance'")
            bank_row = await bank_cur.fetchone()
            bank = float(bank_row["value"] or 0) if bank_row else 0.0
            actual = min(coins, max(bank, 0.0))

            if actual > 0:
                await self.conn.execute("UPDATE system_state SET value=value-? WHERE key='bank_balance'", (actual,))

            user_cur = await self.conn.execute("SELECT balance, reputation FROM users WHERE user_id=?", (user_id,))
            user_row = await user_cur.fetchone()
            old_balance = float(user_row["balance"] or 0) if user_row else 0.0
            old_rep = int(user_row["reputation"] or 0) if user_row else 0
            new_balance = old_balance + actual
            new_rep = clamp(old_rep + max(-30, min(30, rep)), -500, 500)

            await self.conn.execute(
                """INSERT INTO users(user_id, balance, facts_json, last_updated, reputation)
                   VALUES (?, ?, '[]', ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET balance=excluded.balance, last_updated=excluded.last_updated, reputation=excluded.reputation""",
                (user_id, new_balance, now_iso(), new_rep),
            )
            if actual > 0:
                await self.record_transaction_locked(user_id, actual, new_balance, "HUNT_REWARD", description, None)
            await self.conn.execute(
                "UPDATE hunt_participants SET rewarded=1 WHERE hunt_id=? AND user_id=?",
                (hunt_id, user_id),
            )
            await self.conn.commit()
            return round(actual, 2), True

    async def update_hunt_player_state(
        self,
        hunt_id,
        user_id,
        **fields,
    ):
        fields = {
            key: value
            for key, value in fields.items()
            if key
            in self._HUNT_PLAYER_STATE_COLUMNS
        }

        if not fields:
            return

        set_clause = ", ".join(
            f"{key} = ?" for key in fields
        )

        params = list(
            fields.values()
        ) + [hunt_id, user_id]

        async with self.write_lock:

            await self.conn.execute(
                f"""
                UPDATE hunt_player_state
                SET {set_clause}
                WHERE hunt_id = ? AND user_id = ?
                """,
                params,
            )

            await self.conn.commit()

    async def update_hunt_pack_state(
        self,
        hunt_id,
        **fields,
    ):
        fields = {
            key: value
            for key, value in fields.items()
            if key
            in self._HUNT_PACK_STATE_COLUMNS
        }

        if not fields:
            return

        set_clause = ", ".join(
            f"{key} = ?" for key in fields
        )

        params = list(
            fields.values()
        ) + [hunt_id]

        async with self.write_lock:

            await self.conn.execute(
                f"""
                UPDATE hunts
                SET {set_clause}
                WHERE id = ?
                """,
                params,
            )

            await self.conn.commit()

    # ========================================================
    # WILD HUNT 2.0: WORLD / LOCATIONS / FACTS
    # ========================================================
    #
    # Мир — это тоже "закон физики", не LLM. LLM один раз
    # предлагает начальный мир и, по ходу дела, продолжения
    # локаций; эти методы фиксируют предложенное как канон
    # (append-only для фактов) после того, как hunt_world.py
    # уже провалидировал структуру.

    async def create_hunt_world(
        self,
        hunt_id,
        region_name,
        weather,
        world_time,
        danger_level,
    ):
        async with self.write_lock:

            await self.conn.execute(
                """
                INSERT INTO hunt_world (
                    hunt_id, region_name, weather,
                    world_time, danger_level, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(hunt_id) DO UPDATE SET
                    region_name = excluded.region_name,
                    weather = excluded.weather,
                    world_time = excluded.world_time,
                    danger_level = excluded.danger_level,
                    updated_at = excluded.updated_at
                """,
                (
                    hunt_id,
                    region_name,
                    weather,
                    world_time,
                    danger_level,
                    now_iso(),
                ),
            )

            await self.conn.commit()

    async def get_hunt_world(
        self,
        hunt_id,
    ):
        cursor = await self.conn.execute(
            "SELECT * FROM hunt_world WHERE hunt_id = ?",
            (hunt_id,),
        )

        row = await cursor.fetchone()

        return dict(row) if row else None

    async def create_hunt_location(
        self,
        hunt_id,
        location_key,
        title,
        description,
        exits,
        tags,
    ):
        """
        Заводит новую локацию, если такого location_key в этой
        Охоте ещё не было. Если уже была — ничего не делает
        (локации, в отличие от фактов о них, не переписываются
        задним числом методом здесь; для новых выходов есть
        add_hunt_location_exit).
        """

        async with self.write_lock:

            await self.conn.execute(
                """
                INSERT INTO hunt_locations (
                    hunt_id, location_key, title,
                    description, exits_json, tags_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(hunt_id, location_key) DO NOTHING
                """,
                (
                    hunt_id,
                    location_key,
                    title,
                    description,
                    json.dumps(exits, ensure_ascii=False),
                    json.dumps(tags, ensure_ascii=False),
                    now_iso(),
                ),
            )

            await self.conn.commit()

    async def add_hunt_location_exit(
        self,
        hunt_id,
        location_key,
        exit_label,
    ):
        """Дописывает новый обнаруженный выход к уже существующей локации."""

        location = await self.get_hunt_location(
            hunt_id,
            location_key,
        )

        if not location:
            return

        exits = safe_json_loads(
            location.get("exits_json"),
            [],
        )

        if not isinstance(exits, list):
            exits = []

        if exit_label in exits:
            return

        exits.append(exit_label)

        async with self.write_lock:

            await self.conn.execute(
                """
                UPDATE hunt_locations
                SET exits_json = ?
                WHERE hunt_id = ? AND location_key = ?
                """,
                (
                    json.dumps(exits, ensure_ascii=False),
                    hunt_id,
                    location_key,
                ),
            )

            await self.conn.commit()

    async def get_hunt_location(
        self,
        hunt_id,
        location_key,
    ):
        cursor = await self.conn.execute(
            """
            SELECT * FROM hunt_locations
            WHERE hunt_id = ? AND location_key = ?
            """,
            (hunt_id, location_key),
        )

        row = await cursor.fetchone()

        return dict(row) if row else None

    async def count_hunt_locations(
        self,
        hunt_id,
    ):
        cursor = await self.conn.execute(
            "SELECT COUNT(*) AS n FROM hunt_locations WHERE hunt_id = ?",
            (hunt_id,),
        )

        row = await cursor.fetchone()

        return int(row["n"]) if row else 0

    async def add_hunt_fact(
        self,
        hunt_id,
        fact_text,
        fact_key=None,
        location_key=None,
        visibility="public",
        created_by="system",
    ):
        """
        Append-only: всегда INSERT, никогда UPDATE. "Изменение"
        факта — это новая строка с тем же fact_key; get_hunt_facts
        отдаёт только последнюю версию каждого fact_key.
        """

        async with self.write_lock:

            await self.conn.execute(
                """
                INSERT INTO hunt_facts (
                    hunt_id, location_key, fact_key,
                    fact_text, visibility, created_by,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    hunt_id,
                    location_key,
                    fact_key,
                    fact_text,
                    visibility,
                    created_by,
                    now_iso(),
                ),
            )

            await self.conn.commit()

    async def get_hunt_facts(
        self,
        hunt_id,
        location_key=None,
        include_global=True,
        limit=200,
    ):
        """
        Возвращает последнюю версию каждого fact_key (append-only
        история схлопывается до текущего канона), отфильтрованную
        по локации — это и есть "туман войны" на уровне мира:
        игрок в своей локации не видит фактов чужой локации.
        """

        cursor = await self.conn.execute(
            """
            SELECT * FROM hunt_facts
            WHERE hunt_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (hunt_id, limit),
        )

        rows = [dict(row) for row in await cursor.fetchall()]

        seen_keys = set()
        result = []

        for row in rows:

            key = row.get("fact_key") or f"__id_{row['id']}"

            if key in seen_keys:
                continue

            seen_keys.add(key)

            row_location = row.get("location_key")

            is_global = row_location is None

            if is_global and not include_global:
                continue

            if (
                not is_global
                and location_key is not None
                and row_location != location_key
            ):
                continue

            if (
                not is_global
                and location_key is None
            ):
                continue

            result.append(row)

        return result

    # ========================================================
    # TRANSACTIONS
    # ========================================================

    async def record_transaction_locked(
        self,
        user_id,
        amount,
        balance_after,
        transaction_type,
        description="",
        lot_id=None,
        related_user=None,
    ):
        """
        Вызывается ТОЛЬКО внутри write_lock.
        amount:
          + начисление пользователю;
          - списание пользователя.
        """

        await self.conn.execute(
            """
            INSERT INTO transactions
                (
                    user_id,
                    amount,
                    balance_after,
                    type,
                    description,
                    lot_id,
                    related_user,
                    created_at
                )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                round(float(amount), 2),
                round(float(balance_after), 2),
                str(transaction_type)[:50],
                str(description)[:500],
                lot_id,
                related_user,
                now_iso(),
            ),
        )

    async def get_transactions(
        self,
        user_id,
        limit=15,
    ):
        limit = clamp(
            int(limit),
            1,
            100,
        )

        cursor = await self.conn.execute(
            """
            SELECT
                id,
                amount,
                balance_after,
                type,
                description,
                lot_id,
                related_user,
                created_at
            FROM transactions
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (
                user_id,
                limit,
            ),
        )

        return await cursor.fetchall()

    # ========================================================
    # ECONOMY
    # ========================================================

    async def get_inflation_multiplier(self):
        cursor = await self.conn.execute(
            """
            SELECT value
            FROM system_state
            WHERE key = 'inflation_multiplier'
            """
        )

        row = await cursor.fetchone()

        if not row:
            return MIN_INFLATION

        return round(
            clamp(
                float(row["value"]),
                MIN_INFLATION,
                MAX_INFLATION,
            ),
            4,
        )

    async def set_inflation_locked(
        self,
        value,
    ):
        value = clamp(
            float(value),
            MIN_INFLATION,
            MAX_INFLATION,
        )

        await self.conn.execute(
            """
            UPDATE system_state
            SET value = ?
            WHERE key = 'inflation_multiplier'
            """,
            (value,),
        )

        return value

    async def get_last_inflation_ts(self):
        cursor = await self.conn.execute(
            """
            SELECT value
            FROM system_state
            WHERE key = 'last_inflation_ts'
            """
        )

        row = await cursor.fetchone()

        if not row:
            return time.time()

        return float(
            row["value"]
        )

    async def set_last_inflation_ts(
        self,
        ts,
    ):
        async with self.write_lock:
            await self.conn.execute(
                """
                INSERT INTO system_state
                    (key, value)
                VALUES
                    ('last_inflation_ts', ?)
                ON CONFLICT(key)
                DO UPDATE SET
                    value = excluded.value
                """,
                (float(ts),),
            )

            await self.conn.commit()

    async def get_bank_balance(self):
        cursor = await self.conn.execute(
            """
            SELECT value
            FROM system_state
            WHERE key = 'bank_balance'
            """
        )

        row = await cursor.fetchone()

        if not row:
            return 0.0

        return round(
            max(
                0.0,
                float(row["value"]),
            ),
            2,
        )

    async def get_money_supply(self):
        """
        Деньги пользователей + резерв банка.

        Это используется только для стабилизации экономики.
        """

        cursor = await self.conn.execute(
            """
            SELECT COALESCE(
                SUM(
                    CASE
                        WHEN balance > 0
                        THEN balance
                        ELSE 0
                    END
                ),
                0
            ) AS total
            FROM users
            """
        )

        row = await cursor.fetchone()

        user_money = (
            float(row["total"] or 0)
            if row
            else 0.0
        )

        bank = await self.get_bank_balance()

        return round(
            user_money + bank,
            2,
        )

    async def add_to_bank(
        self,
        amount,
    ):
        amount = max(
            0.0,
            float(amount),
        )

        if amount <= 0:
            return

        async with self.write_lock:
            await self.conn.execute(
                """
                UPDATE system_state
                SET value = value + ?
                WHERE key = 'bank_balance'
                """,
                (amount,),
            )

            await self.conn.commit()

    async def apply_economic_adjustment(
        self,
        hours,
    ):
        """
        Стабилизирующая модель инфляции.

        При избытке денег:
          инфляция растёт.

        При недостатке денег:
          инфляция постепенно снижается.

        Есть абсолютный потолок x3.
        """

        hours = int(hours)

        if hours <= 0:
            return

        money_supply = (
            await self.get_money_supply()
        )

        inflation = (
            await self.get_inflation_multiplier()
        )

        pressure = (
            money_supply
            - TARGET_MONEY_SUPPLY
        ) * MONEY_SUPPLY_INFLATION_FACTOR

        hourly_delta = (
            BASE_INFLATION_PER_HOUR
            + pressure
        )

        hourly_delta = clamp(
            hourly_delta,
            -MAX_DEFLATION_PER_HOUR,
            0.02,
        )

        new_inflation = (
            inflation
            + hourly_delta * hours
        )

        new_inflation = clamp(
            new_inflation,
            MIN_INFLATION,
            MAX_INFLATION,
        )

        async with self.write_lock:

            await self.set_inflation_locked(
                new_inflation
            )

            await self.conn.execute(
                """
                UPDATE system_state
                SET value = ?
                WHERE key = 'last_inflation_ts'
                """,
                (
                    time.time(),
                ),
            )

            await self.conn.commit()

        logging.info(
            "[ЭКОНОМИКА] Денежная масса=%s, "
            "инфляция x%.4f -> x%.4f",
            money_supply,
            inflation,
            new_inflation,
        )

    async def apply_missed_inflation(self):
        now = time.time()

        last = (
            await self.get_last_inflation_ts()
        )

        diff = now - last

        if diff < 3600:
            return

        hours = int(
            diff // 3600
        )

        if hours <= 0:
            return

        # Важно: корректируем на часы простоя.
        await self.apply_economic_adjustment(
            hours
        )

    async def check_and_apply_inflation_hourly(self):
        now = time.time()

        last = (
            await self.get_last_inflation_ts()
        )

        if now - last < 3600:
            return False

        hours = int(
            (now - last) // 3600
        )

        if hours <= 0:
            return False

        await self.apply_economic_adjustment(
            hours
        )

        return True

    # ========================================================
    # BALANCES
    # ========================================================

    async def get_balance(
        self,
        user_id,
    ):
        cursor = await self.conn.execute(
            """
            SELECT balance
            FROM users
            WHERE user_id = ?
            """,
            (user_id,),
        )

        row = await cursor.fetchone()

        if not row:
            return 0.0

        return round(
            max(
                0.0,
                float(
                    row["balance"] or 0
                ),
            ),
            2,
        )

    async def add_coins(
        self,
        user_id,
        amount,
        transaction_type="SYSTEM_REWARD",
        description="Начисление коинов",
        lot_id=None,
        related_user=None,
    ):
        amount = float(amount)

        if amount == 0:
            return await self.get_balance(
                user_id
            )

        async with self.write_lock:

            cursor = await self.conn.execute(
                """
                SELECT balance
                FROM users
                WHERE user_id = ?
                """,
                (user_id,),
            )

            row = await cursor.fetchone()

            old_balance = (
                float(row["balance"] or 0)
                if row
                else 0.0
            )

            new_balance = max(
                0.0,
                old_balance + amount,
            )

            actual_delta = (
                new_balance
                - old_balance
            )

            await self.conn.execute(
                """
                INSERT INTO users
                    (
                        user_id,
                        balance,
                        facts_json,
                        last_updated
                    )
                VALUES (?, ?, '[]', ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    balance = ?,
                    last_updated = ?
                """,
                (
                    user_id,
                    new_balance,
                    now_iso(),
                    new_balance,
                    now_iso(),
                ),
            )

            await self.record_transaction_locked(
                user_id,
                actual_delta,
                new_balance,
                transaction_type,
                description,
                lot_id,
                related_user,
            )

            await self.conn.commit()

        return round(
            new_balance,
            2,
        )

    async def transfer_bank_to_user(
        self,
        user_id,
        amount,
        transaction_type="QUIZ_REWARD",
        description="Награда из банка",
        lot_id=None,
    ):
        amount = max(
            0.0,
            float(amount),
        )

        if amount <= 0:
            return 0.0

        async with self.write_lock:

            cursor = await self.conn.execute(
                """
                SELECT value
                FROM system_state
                WHERE key = 'bank_balance'
                """
            )

            row = await cursor.fetchone()

            bank = (
                float(row["value"])
                if row
                else 0.0
            )

            actual = min(
                amount,
                max(bank, 0.0),
            )

            if actual <= 0:
                return 0.0

            await self.conn.execute(
                """
                UPDATE system_state
                SET value = value - ?
                WHERE key = 'bank_balance'
                """,
                (actual,),
            )

            user_cursor = await self.conn.execute(
                """
                SELECT balance
                FROM users
                WHERE user_id = ?
                """,
                (user_id,),
            )

            user_row = (
                await user_cursor.fetchone()
            )

            old_balance = (
                float(
                    user_row["balance"] or 0
                )
                if user_row
                else 0.0
            )

            new_balance = (
                old_balance
                + actual
            )

            await self.conn.execute(
                """
                INSERT INTO users
                    (
                        user_id,
                        balance,
                        facts_json,
                        last_updated
                    )
                VALUES (?, ?, '[]', ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    balance = ?,
                    last_updated = ?
                """,
                (
                    user_id,
                    new_balance,
                    now_iso(),
                    new_balance,
                    now_iso(),
                ),
            )

            await self.record_transaction_locked(
                user_id,
                actual,
                new_balance,
                transaction_type,
                description,
                lot_id,
            )

            await self.conn.commit()

        return round(
            actual,
            2,
        )

    async def charge_user_and_credit_bank(
        self,
        user_id,
        amount,
        transaction_type="PURCHASE",
        description="Покупка",
        lot_id=None,
        related_user=None,
    ):
        """
        Атомарная покупка.

        Возвращает:
            success, new_balance
        """

        amount = float(amount)

        if amount <= 0:
            return False, await self.get_balance(
                user_id
            )

        async with self.write_lock:

            cursor = await self.conn.execute(
                """
                SELECT balance
                FROM users
                WHERE user_id = ?
                """,
                (user_id,),
            )

            row = await cursor.fetchone()

            balance = (
                float(row["balance"] or 0)
                if row
                else 0.0
            )

            if balance < amount:
                return False, round(
                    balance,
                    2,
                )

            new_balance = (
                balance - amount
            )

            await self.conn.execute(
                """
                UPDATE users
                SET
                    balance = ?,
                    last_updated = ?
                WHERE user_id = ?
                """,
                (
                    new_balance,
                    now_iso(),
                    user_id,
                ),
            )

            await self.conn.execute(
                """
                UPDATE system_state
                SET value = value + ?
                WHERE key = 'bank_balance'
                """,
                (amount,),
            )

            await self.record_transaction_locked(
                user_id,
                -amount,
                new_balance,
                transaction_type,
                description,
                lot_id,
                related_user,
            )

            await self.conn.commit()

            return True, round(
                new_balance,
                2,
            )

    # ========================================================
    # MARKET
    # ========================================================

    async def create_lot(
        self,
        item_name,
        full_info,
        base_price,
    ):
        item_name = str(
            item_name
        ).strip()[:100]

        full_info = str(
            full_info
        ).strip()[:3000]

        try:
            base_price = float(
                base_price
            )
        except (
            ValueError,
            TypeError,
        ):
            return None

        if not item_name or not full_info:
            return None

        if not (
            100 <= base_price <= 1_000_000
        ):
            return None

        async with self.write_lock:
            cursor = await self.conn.execute(
                """
                INSERT INTO market_lots
                    (
                        item_name,
                        full_info,
                        base_price,
                        multiplier,
                        status,
                        investments_json,
                        created_at
                    )
                VALUES (?, ?, ?, 1.0, 'active', '{}', ?)
                """,
                (
                    item_name,
                    full_info,
                    base_price,
                    now_iso(),
                ),
            )

            await self.conn.commit()

            return cursor.lastrowid

    async def get_active_lots(self):
        cursor = await self.conn.execute(
            """
            SELECT
                id,
                item_name,
                base_price,
                multiplier
            FROM market_lots
            WHERE status = 'active'
            ORDER BY id DESC
            """
        )

        return await cursor.fetchall()

    async def get_lot(
        self,
        lot_id,
    ):
        cursor = await self.conn.execute(
            """
            SELECT
                id,
                item_name,
                full_info,
                base_price,
                multiplier,
                status,
                investments_json,
                owner
            FROM market_lots
            WHERE id = ?
            """,
            (int(lot_id),),
        )

        return await cursor.fetchone()

    async def get_lot_investments(
        self,
        lot_id,
        only_active=True,
    ):
        if only_active:
            cursor = await self.conn.execute(
                """
                SELECT
                    id,
                    lot_id,
                    user_id,
                    amount,
                    created_at,
                    updated_at,
                    status
                FROM lot_investments
                WHERE lot_id = ?
                  AND status = 'active'
                  AND amount > 0
                ORDER BY amount DESC
                """,
                (int(lot_id),),
            )
        else:
            cursor = await self.conn.execute(
                """
                SELECT
                    id,
                    lot_id,
                    user_id,
                    amount,
                    created_at,
                    updated_at,
                    status
                FROM lot_investments
                WHERE lot_id = ?
                ORDER BY id
                """,
                (int(lot_id),),
            )

        return await cursor.fetchall()

    async def get_user_investments(
        self,
        user_id,
    ):
        cursor = await self.conn.execute(
            """
            SELECT
                li.id,
                li.lot_id,
                li.amount,
                li.created_at,
                li.updated_at,
                ml.item_name,
                ml.base_price,
                ml.multiplier,
                ml.status AS lot_status
            FROM lot_investments li
            JOIN market_lots ml
                ON ml.id = li.lot_id
            WHERE li.user_id = ?
              AND li.status = 'active'
              AND li.amount > 0
            ORDER BY li.id DESC
            """,
            (user_id,),
        )

        return await cursor.fetchall()

    async def get_user_lot_investment(
        self,
        user_id,
        lot_id,
    ):
        cursor = await self.conn.execute(
            """
            SELECT
                id,
                lot_id,
                user_id,
                amount,
                created_at,
                updated_at,
                status
            FROM lot_investments
            WHERE user_id = ?
              AND lot_id = ?
              AND status = 'active'
              AND amount > 0
            """,
            (
                user_id,
                int(lot_id),
            ),
        )

        return await cursor.fetchone()

    # ========================================================
    # INVESTMENT
    # ========================================================

    async def invest_in_lot(
        self,
        user_id,
        lot_id,
        amount,
    ):
        try:
            lot_id = int(lot_id)
            amount = float(amount)
        except (
            ValueError,
            TypeError,
        ):
            return False, (
                "ID лота и сумма должны быть числами."
            )

        if amount < MIN_INVESTMENT:
            return False, (
                f"Минимальная инвестиция: "
                f"{MIN_INVESTMENT:.2f} коинов."
            )

        if amount > MAX_INVESTMENT:
            return False, (
                f"Максимальная инвестиция за операцию: "
                f"{MAX_INVESTMENT:.2f} коинов."
            )

        async with self.write_lock:

            cursor = await self.conn.execute(
                """
                SELECT
                    id,
                    base_price,
                    multiplier,
                    status
                FROM market_lots
                WHERE id = ?
                """,
                (lot_id,),
            )

            lot = await cursor.fetchone()

            if not lot:
                return False, "Лот не найден."

            if lot["status"] != "active":
                return False, (
                    "Лот уже закрыт. "
                    "Инвестировать в него нельзя."
                )

            user_cursor = await self.conn.execute(
                """
                SELECT balance
                FROM users
                WHERE user_id = ?
                """,
                (user_id,),
            )

            user = await user_cursor.fetchone()

            balance = (
                float(
                    user["balance"] or 0
                )
                if user
                else 0.0
            )

            if balance < amount:
                return False, (
                    f"Недостаточно средств. "
                    f"Нужно {amount:.2f}, "
                    f"у тебя {balance:.2f}."
                )

            # ------------------------------------------------
            # Списываем деньги.
            # ------------------------------------------------

            new_balance = (
                balance - amount
            )

            await self.conn.execute(
                """
                UPDATE users
                SET
                    balance = ?,
                    last_updated = ?
                WHERE user_id = ?
                """,
                (
                    new_balance,
                    now_iso(),
                    user_id,
                ),
            )

            await self.conn.execute(
                """
                UPDATE system_state
                SET value = value + ?
                WHERE key = 'bank_balance'
                """,
                (amount,),
            )

            # ------------------------------------------------
            # Инвестиционная позиция.
            # ------------------------------------------------

            existing_cursor = await self.conn.execute(
                """
                SELECT
                    id,
                    amount
                FROM lot_investments
                WHERE lot_id = ?
                  AND user_id = ?
                  AND status = 'active'
                """,
                (
                    lot_id,
                    user_id,
                ),
            )

            existing = (
                await existing_cursor.fetchone()
            )

            timestamp = now_iso()

            if existing:

                new_amount = (
                    float(
                        existing["amount"]
                    )
                    + amount
                )

                await self.conn.execute(
                    """
                    UPDATE lot_investments
                    SET
                        amount = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        new_amount,
                        timestamp,
                        existing["id"],
                    ),
                )

            else:

                new_amount = amount

                await self.conn.execute(
                    """
                    INSERT INTO lot_investments
                        (
                            lot_id,
                            user_id,
                            amount,
                            created_at,
                            updated_at,
                            status
                        )
                    VALUES (?, ?, ?, ?, ?, 'active')
                    """,
                    (
                        lot_id,
                        user_id,
                        amount,
                        timestamp,
                        timestamp,
                    ),
                )

            # ------------------------------------------------
            # Хайп лота.
            # ------------------------------------------------

            new_multiplier = (
                float(lot["multiplier"])
                + (
                    amount
                    / max(
                        float(
                            lot["base_price"]
                        ),
                        1.0,
                    )
                )
                * 0.1
            )

            # Не даём хайпу бесконечно расти.
            new_multiplier = clamp(
                new_multiplier,
                1.0,
                10.0,
            )

            await self.conn.execute(
                """
                UPDATE market_lots
                SET
                    multiplier = ?,
                    investments_json = '{}'
                WHERE id = ?
                  AND status = 'active'
                """,
                (
                    new_multiplier,
                    lot_id,
                ),
            )

            await self.record_transaction_locked(
                user_id,
                -amount,
                new_balance,
                "INVEST",
                (
                    f"Инвестиция в лот #{lot_id} "
                    f"на {amount:.2f} коинов"
                ),
                lot_id,
            )

            await self.conn.commit()

            return True, (
                f"📈 Инвестировано {amount:.2f} коинов "
                f"в лот #{lot_id}.\n"
                f"Твоя доля: {new_amount:.2f} коинов.\n"
                f"Комиссия при досрочном выходе: "
                f"{INVESTMENT_EXIT_FEE * 100:.0f}%."
            )

    async def sell_investment(
        self,
        user_id,
        lot_id,
        amount=None,
    ):
        """
        Досрочная продажа инвестиции.

        amount=None:
            продаёт всю долю.

        amount=число:
            продаёт часть.

        После закрытия лота продавать нельзя.
        """

        try:
            lot_id = int(lot_id)
        except (
            ValueError,
            TypeError,
        ):
            return False, (
                "ID лота должен быть числом."
            )

        if amount is not None:
            try:
                amount = float(amount)
            except (
                ValueError,
                TypeError,
            ):
                return False, (
                    "Сумма должна быть числом."
                )

        async with self.write_lock:

            lot_cursor = await self.conn.execute(
                """
                SELECT
                    id,
                    item_name,
                    status
                FROM market_lots
                WHERE id = ?
                """,
                (lot_id,),
            )

            lot = await lot_cursor.fetchone()

            if not lot:
                return False, "Лот не найден."

            if lot["status"] != "active":
                return False, (
                    "Лот уже закрыт. "
                    "Доля по нему больше не продаётся."
                )

            inv_cursor = await self.conn.execute(
                """
                SELECT
                    id,
                    amount
                FROM lot_investments
                WHERE lot_id = ?
                  AND user_id = ?
                  AND status = 'active'
                """,
                (
                    lot_id,
                    user_id,
                ),
            )

            investment = (
                await inv_cursor.fetchone()
            )

            if not investment:
                return False, (
                    "У тебя нет активной доли "
                    "в этом лоте."
                )

            owned = float(
                investment["amount"]
            )

            if amount is None:
                sell_amount = owned
            else:
                sell_amount = amount

            if sell_amount <= 0:
                return False, (
                    "Сумма продажи должна быть "
                    "положительной."
                )

            if sell_amount > owned:
                return False, (
                    f"У тебя доля только "
                    f"{owned:.2f} коинов."
                )

            fee = round(
                sell_amount
                * INVESTMENT_EXIT_FEE,
                2,
            )

            payout = round(
                sell_amount - fee,
                2,
            )

            remaining = round(
                owned - sell_amount,
                2,
            )

            # ------------------------------------------------
            # Обновляем долю.
            # ------------------------------------------------

            if remaining <= 0:
                await self.conn.execute(
                    """
                    UPDATE lot_investments
                    SET
                        amount = 0,
                        status = 'sold',
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        now_iso(),
                        investment["id"],
                    ),
                )
            else:
                await self.conn.execute(
                    """
                    UPDATE lot_investments
                    SET
                        amount = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        remaining,
                        now_iso(),
                        investment["id"],
                    ),
                )

            # ------------------------------------------------
            # Деньги:
            #
            # инвестор получает сумму минус комиссию.
            # Комиссия остаётся в банке.
            # ------------------------------------------------

            bank_cursor = await self.conn.execute(
                """
                SELECT value
                FROM system_state
                WHERE key = 'bank_balance'
                """
            )

            bank_row = (
                await bank_cursor.fetchone()
            )

            bank = (
                float(
                    bank_row["value"]
                )
                if bank_row
                else 0.0
            )

            if bank < payout:
                # Теоретически банк может оказаться пустым
                # после старой версии БД.
                # Не проводим частичную операцию.
                await self.conn.rollback()

                return False, (
                    "В банке недостаточно ликвидности "
                    "для выхода из инвестиции. "
                    "Попробуй позже."
                )

            await self.conn.execute(
                """
                UPDATE system_state
                SET value = value - ?
                WHERE key = 'bank_balance'
                """,
                (payout,),
            )

            # Комиссия уже остаётся в банке.
            user_cursor = await self.conn.execute(
                """
                SELECT balance
                FROM users
                WHERE user_id = ?
                """,
                (user_id,),
            )

            user_row = (
                await user_cursor.fetchone()
            )

            old_balance = (
                float(
                    user_row["balance"] or 0
                )
                if user_row
                else 0.0
            )

            new_balance = (
                old_balance + payout
            )

            await self.conn.execute(
                """
                INSERT INTO users
                    (
                        user_id,
                        balance,
                        facts_json,
                        last_updated
                    )
                VALUES (?, ?, '[]', ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    balance = ?,
                    last_updated = ?
                """,
                (
                    user_id,
                    new_balance,
                    now_iso(),
                    new_balance,
                    now_iso(),
                ),
            )

            await self.record_transaction_locked(
                user_id,
                payout,
                new_balance,
                "INVEST_SELL",
                (
                    f"Продажа инвестиции "
                    f"по лоту #{lot_id}: "
                    f"{sell_amount:.2f}, "
                    f"комиссия {fee:.2f}"
                ),
                lot_id,
            )

            await self.conn.commit()

            return True, (
                f"📤 Доля продана.\n"
                f"Продано: {sell_amount:.2f}\n"
                f"Комиссия: {fee:.2f}\n"
                f"Получено: {payout:.2f}\n"
                f"Остаток доли: {remaining:.2f}"
            )

    async def buyout_lot(
        self,
        user_id,
        lot_id,
    ):
        """
        Полный выкуп проводится внутри одной транзакции.

        Покупатель:
          платит цену лота.

        Инвесторы:
          получают 150% своей доли * текущий inflation.

        После закрытия:
          активные инвестиционные позиции переводятся
          в status='paid'.
        """

        try:
            lot_id = int(lot_id)
        except (
            ValueError,
            TypeError,
        ):
            return False, (
                "ID лота должен быть числом."
            )

        async with self.write_lock:

            cursor = await self.conn.execute(
                """
                SELECT
                    id,
                    item_name,
                    full_info,
                    base_price,
                    multiplier,
                    status
                FROM market_lots
                WHERE id = ?
                """,
                (lot_id,),
            )

            lot = await cursor.fetchone()

            if not lot:
                return False, "Лот не найден."

            if lot["status"] != "active":
                return False, (
                    "Лот уже выкуплен."
                )

            # ------------------------------------------------
            # Инфляция.
            # ------------------------------------------------

            inf_cursor = await self.conn.execute(
                """
                SELECT value
                FROM system_state
                WHERE key = 'inflation_multiplier'
                """
            )

            inf_row = (
                await inf_cursor.fetchone()
            )

            inf = (
                float(
                    inf_row["value"]
                )
                if inf_row
                else 1.0
            )

            inf = clamp(
                inf,
                MIN_INFLATION,
                MAX_INFLATION,
            )

            current_price = round(
                float(
                    lot["base_price"]
                )
                * float(
                    lot["multiplier"]
                )
                * inf,
                2,
            )

            # ------------------------------------------------
            # Покупатель.
            # ------------------------------------------------

            user_cursor = await self.conn.execute(
                """
                SELECT balance
                FROM users
                WHERE user_id = ?
                """,
                (user_id,),
            )

            user_row = (
                await user_cursor.fetchone()
            )

            user_balance = (
                float(
                    user_row["balance"] or 0
                )
                if user_row
                else 0.0
            )

            if user_balance < current_price:
                return False, (
                    f"Цена лота {current_price:.2f}. "
                    f"У тебя только "
                    f"{user_balance:.2f}."
                )

            new_buyer_balance = (
                user_balance
                - current_price
            )

            await self.conn.execute(
                """
                UPDATE users
                SET
                    balance = ?,
                    last_updated = ?
                WHERE user_id = ?
                """,
                (
                    new_buyer_balance,
                    now_iso(),
                    user_id,
                ),
            )

            await self.conn.execute(
                """
                UPDATE system_state
                SET value = value + ?
                WHERE key = 'bank_balance'
                """,
                (current_price,),
            )

            await self.record_transaction_locked(
                user_id,
                -current_price,
                new_buyer_balance,
                "LOT_BUYOUT",
                (
                    f"Полный выкуп лота "
                    f"#{lot_id} «{lot['item_name']}»"
                ),
                lot_id,
            )

            # ------------------------------------------------
            # Инвесторы.
            # ------------------------------------------------

            inv_cursor = await self.conn.execute(
                """
                SELECT
                    id,
                    user_id,
                    amount
                FROM lot_investments
                WHERE lot_id = ?
                  AND status = 'active'
                  AND amount > 0
                """,
                (lot_id,),
            )

            investments = (
                await inv_cursor.fetchall()
            )

            payouts = []

            bank_cursor = await self.conn.execute(
                """
                SELECT value
                FROM system_state
                WHERE key = 'bank_balance'
                """
            )

            bank_row = (
                await bank_cursor.fetchone()
            )

            bank = (
                float(
                    bank_row["value"]
                )
                if bank_row
                else 0.0
            )

            for investment in investments:

                invested = max(
                    0.0,
                    float(
                        investment["amount"]
                    ),
                )

                requested = round(
                    invested
                    * INVESTOR_RETURN_MULTIPLIER
                    * inf,
                    2,
                )

                actual = min(
                    requested,
                    max(bank, 0.0),
                )

                if actual > 0:

                    await self.conn.execute(
                        """
                        UPDATE system_state
                        SET value = value - ?
                        WHERE key = 'bank_balance'
                        """,
                        (actual,),
                    )

                    investor_cursor = (
                        await self.conn.execute(
                            """
                            SELECT balance
                            FROM users
                            WHERE user_id = ?
                            """,
                            (
                                investment["user_id"],
                            ),
                        )
                    )

                    investor_row = (
                        await investor_cursor.fetchone()
                    )

                    old_balance = (
                        float(
                            investor_row[
                                "balance"
                            ]
                            or 0
                        )
                        if investor_row
                        else 0.0
                    )

                    new_balance = (
                        old_balance
                        + actual
                    )

                    await self.conn.execute(
                        """
                        INSERT INTO users
                            (
                                user_id,
                                balance,
                                facts_json,
                                last_updated
                            )
                        VALUES (?, ?, '[]', ?)
                        ON CONFLICT(user_id)
                        DO UPDATE SET
                            balance = ?,
                            last_updated = ?
                        """,
                        (
                            investment["user_id"],
                            new_balance,
                            now_iso(),
                            new_balance,
                            now_iso(),
                        ),
                    )

                    await self.record_transaction_locked(
                        investment["user_id"],
                        actual,
                        new_balance,
                        "INVEST_PAYOUT",
                        (
                            f"Выплата по лоту "
                            f"#{lot_id}: "
                            f"вложено {invested:.2f}"
                        ),
                        lot_id,
                        user_id,
                    )

                    bank -= actual

                payouts.append(
                    (
                        f"{investment['user_id']} "
                        f"получил {actual:.2f}"
                    )
                )

                await self.conn.execute(
                    """
                    UPDATE lot_investments
                    SET
                        amount = 0,
                        status = 'paid',
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        now_iso(),
                        investment["id"],
                    ),
                )

            # ------------------------------------------------
            # Закрываем лот.
            # ------------------------------------------------

            await self.conn.execute(
                """
                UPDATE market_lots
                SET
                    status = 'bought',
                    owner = ?
                WHERE id = ?
                  AND status = 'active'
                """,
                (
                    user_id,
                    lot_id,
                ),
            )

            await self.conn.commit()

            return True, {
                "info": lot["full_info"],
                "item": lot["item_name"],
                "price": current_price,
                "payouts": payouts,
            }

    async def pump_lot(
        self,
        lot_id,
        increment=0.05,
    ):
        try:
            lot_id = int(lot_id)
            increment = float(
                increment
            )
        except (
            ValueError,
            TypeError,
        ):
            return False

        if (
            increment <= 0
            or increment > 1.0
        ):
            return False

        async with self.write_lock:

            cursor = await self.conn.execute(
                """
                UPDATE market_lots
                SET multiplier =
                    MIN(
                        multiplier + ?,
                        10.0
                    )
                WHERE id = ?
                  AND status = 'active'
                """,
                (
                    increment,
                    lot_id,
                ),
            )

            await self.conn.commit()

            return (
                cursor.rowcount > 0
            )

    # ========================================================
    # SHUTDOWN
    # ========================================================

    async def close(self):
        if self.conn:
            await self.conn.close()
            self.conn = None


# ============================================================
# DOG BOT
# ============================================================


# ============================================================
# PRIVACY / ACCOUNTS / CHAT CONTEXT
# ============================================================

import hashlib as _hashlib
import secrets as _secrets
import hmac as _hmac


def _password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or _secrets.token_bytes(16)
    digest = _hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 240_000)
    return f"pbkdf2_sha256$240000${salt.hex()}${digest.hex()}"


def _password_verify(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = _hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        )
        return _hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


async def _init_privacy(self):
    async with self.write_lock:
        cursor = await self.conn.execute("PRAGMA table_info(users)")
        columns = {row["name"] for row in await cursor.fetchall()}
        if "password_hash" not in columns:
            await self.conn.execute("ALTER TABLE users ADD COLUMN password_hash TEXT")
        if "consent_at" not in columns:
            await self.conn.execute("ALTER TABLE users ADD COLUMN consent_at TIMESTAMP")

        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS privacy_consents (
                nickname TEXT PRIMARY KEY,
                consent_at TIMESTAMP NOT NULL
            )
        """)
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS user_aliases (
                nickname TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            )
        """)
        await self.conn.execute("CREATE INDEX IF NOT EXISTS idx_user_aliases_user ON user_aliases(user_id)")
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS context_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                nickname TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL,
                batch_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                sent_at TIMESTAMP,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            )
        """)
        await self.conn.execute("CREATE INDEX IF NOT EXISTS idx_context_pending ON context_messages(status, id)")

        # ------------------------------------------------------------
        # SHORT-TERM CHAT ARCHIVE
        # Полный MUC-журнал на 14 дней. Это отдельная таблица от
        # context_messages: последний нужен фоновой обработке памяти,
        # а этот — точечному историческому поиску. user_id nullable,
        # потому что архив намеренно сохраняет также незарегистрированные
        # сообщения; согласие по-прежнему требуется для персональной
        # памяти/LLM-контекста.
        # ------------------------------------------------------------
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_archive (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_jid TEXT NOT NULL,
                sender_nick TEXT NOT NULL,
                sender_jid TEXT,
                user_id TEXT,
                body TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL,
                stanza_id TEXT,
                mentions_json TEXT NOT NULL DEFAULT '[]',
                quoted_text TEXT,
                quote_author TEXT,
                quote_stanza_id TEXT,
                is_command INTEGER NOT NULL DEFAULT 0
            )
        """)
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_archive_room_time "
            "ON chat_archive(room_jid, created_at)"
        )
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_archive_room_sender_time "
            "ON chat_archive(room_jid, sender_nick, created_at)"
        )
        await self.conn.execute("""
            CREATE TABLE IF NOT EXISTS chat_archive_mentions (
                message_id INTEGER NOT NULL,
                nick TEXT NOT NULL,
                PRIMARY KEY(message_id, nick),
                FOREIGN KEY(message_id) REFERENCES chat_archive(id)
                    ON DELETE CASCADE
            )
        """)
        await self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_archive_mentions_nick "
            "ON chat_archive_mentions(nick, message_id)"
        )
        # Разовая уборка при старте — чтобы после простоя старше retention
        # база не сохраняла старые архивные строки дольше положенного.
        archive_cutoff = (
            datetime.now() - timedelta(days=max(int(CHAT_ARCHIVE_RETENTION_DAYS), 1))
        ).isoformat(timespec="seconds")
        await self.conn.execute(
            "DELETE FROM chat_archive WHERE created_at < ?",
            (archive_cutoff,),
        )
        await self.conn.commit()


async def _create_consent_record(self, nickname):
    nickname = str(nickname).strip()[:80]
    async with self.write_lock:
        await self.conn.execute(
            "INSERT OR REPLACE INTO privacy_consents(nickname, consent_at) VALUES (?, ?)",
            (nickname, now_iso()),
        )
        await self.conn.commit()


async def _has_consent_by_nick(self, nickname):
    nickname = str(nickname).strip()
    cursor = await self.conn.execute(
        "SELECT 1 FROM user_aliases a JOIN users u ON u.user_id=a.user_id WHERE a.nickname=? AND u.consent_at IS NOT NULL LIMIT 1",
        (nickname,),
    )
    if await cursor.fetchone():
        return True
    cursor = await self.conn.execute("SELECT 1 FROM privacy_consents WHERE nickname=? LIMIT 1", (nickname,))
    return bool(await cursor.fetchone())




async def _set_consent(self, user_id, enabled=True):
    async with self.write_lock:
        await self.conn.execute(
            "UPDATE users SET consent_at=? WHERE user_id=?",
            (now_iso() if enabled else None, user_id),
        )
        await self.conn.commit()

async def _has_consent(self, user_id):
    cursor = await self.conn.execute("SELECT consent_at FROM users WHERE user_id=?", (user_id,))
    row = await cursor.fetchone()
    return bool(row and row["consent_at"])


async def _account_exists(self, user_id):
    cursor = await self.conn.execute("SELECT 1 FROM users WHERE user_id=? AND password_hash IS NOT NULL LIMIT 1", (user_id,))
    return bool(await cursor.fetchone())


async def _get_account_by_nick(self, nickname):
    cursor = await self.conn.execute("""
        SELECT u.user_id, u.nickname, u.password_hash, u.consent_at
        FROM user_aliases a JOIN users u ON u.user_id=a.user_id
        WHERE a.nickname=? LIMIT 1
    """, (str(nickname).strip(),))
    row = await cursor.fetchone()
    return dict(row) if row else None


async def _register_account(self, nickname):
    if not await self.has_consent_by_nick(nickname):
        raise ValueError("consent_required")
    existing = await self.get_account_by_nick(nickname)
    if existing:
        return {"user_id": existing["user_id"], "password": None, "existing": True}

    password = _secrets.token_urlsafe(10)[:14]
    async with self.write_lock:
        for _ in range(20):
            user_id = "DOG-" + _secrets.token_hex(4).upper()
            cur = await self.conn.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,))
            if not await cur.fetchone():
                break
        else:
            raise RuntimeError("cannot_generate_user_id")

        await self.conn.execute("""
            INSERT INTO users(user_id, nickname, facts_json, last_updated, balance, reputation, password_hash, consent_at)
            VALUES (?, ?, '[]', ?, 0, 0, ?, ?)
        """, (user_id, str(nickname).strip()[:30], now_iso(), _password_hash(password), now_iso()))
        await self.conn.execute("DELETE FROM privacy_consents WHERE nickname=?", (str(nickname).strip(),))
        await self.conn.execute(
            "INSERT OR REPLACE INTO user_aliases(nickname,user_id,created_at) VALUES (?,?,?)",
            (str(nickname).strip(), user_id, now_iso()),
        )
        await self.conn.commit()
    return {"user_id": user_id, "password": password, "existing": False}


async def _verify_password(self, user_id, password):
    cursor = await self.conn.execute("SELECT password_hash FROM users WHERE user_id=?", (user_id,))
    row = await cursor.fetchone()
    return bool(row and row["password_hash"] and _password_verify(str(password), row["password_hash"]))


async def _change_password(self, user_id):
    """Генерирует новый секрет, заменяет им хэш пароля и возвращает
    его вызывающему коду (для сборки нового токена вида ID:секрет).
    Возвращает None, если аккаунта не существует."""
    async with self.write_lock:
        cursor = await self.conn.execute(
            "SELECT 1 FROM users WHERE user_id=?", (user_id,)
        )
        if not await cursor.fetchone():
            return None
        new_secret = _secrets.token_urlsafe(10)[:14]
        await self.conn.execute(
            "UPDATE users SET password_hash=? WHERE user_id=?",
            (_password_hash(new_secret), user_id),
        )
        await self.conn.commit()
        return new_secret


async def _verify_token(self, token):
    """
    Токен доступа — единая строка "<ID>:<секрет>", объединяющая ID
    аккаунта и пароль. Заменяет отдельный ввод ID и пароля.
    Возвращает user_id при успехе, иначе None.
    """
    token = str(token).strip()
    if ":" not in token:
        return None
    user_id, secret = token.split(":", 1)
    user_id = user_id.strip()
    secret = secret.strip()
    if not user_id or not secret:
        return None
    if not await self.account_exists(user_id):
        return None
    if not await self.verify_password(user_id, secret):
        return None
    return user_id


async def _bind_nick(self, user_id, nickname):
    nickname = str(nickname).strip()[:80]
    async with self.write_lock:
        cursor = await self.conn.execute("SELECT 1 FROM users WHERE user_id=? AND consent_at IS NOT NULL", (user_id,))
        if not await cursor.fetchone():
            return False
        cursor = await self.conn.execute("SELECT user_id FROM user_aliases WHERE nickname=?", (nickname,))
        row = await cursor.fetchone()
        if row and row["user_id"] != user_id:
            return False
        await self.conn.execute(
            "INSERT OR REPLACE INTO user_aliases(nickname,user_id,created_at) VALUES (?,?,?)",
            (nickname, user_id, now_iso()),
        )
        await self.conn.execute("UPDATE users SET nickname=?, last_updated=? WHERE user_id=?", (nickname, now_iso(), user_id))
        await self.conn.commit()
        return True


async def _get_ship_position(self, user_id):
    cursor = await self.conn.execute(
        "SELECT ship_position FROM users WHERE user_id=?",
        (str(user_id),),
    )
    row = await cursor.fetchone()
    return row["ship_position"] if row else None


async def _set_ship_position(self, user_id, position):
    position = str(position).strip() if position is not None else None
    async with self.write_lock:
        await self.conn.execute(
            "UPDATE users SET ship_position=?, ship_position_changed_at=?, last_updated=? WHERE user_id=? AND password_hash IS NOT NULL",
            (position or None, now_iso(), now_iso(), str(user_id)),
        )
        await self.conn.commit()
    return True


async def _get_crew_accounts(self):
    cursor = await self.conn.execute(
        "SELECT u.user_id, u.nickname, u.ship_position FROM users u WHERE u.password_hash IS NOT NULL AND u.consent_at IS NOT NULL AND u.nickname IS NOT NULL ORDER BY u.nickname COLLATE NOCASE"
    )
    return [dict(row) for row in await cursor.fetchall()]


async def _get_crew_by_nicks(self, nicks):
    clean = []
    seen = set()
    for nick in nicks or []:
        value = str(nick).strip()
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            clean.append(value)
    if not clean:
        return {}
    placeholders = ",".join("?" for _ in clean)
    cursor = await self.conn.execute(
        f"""
        SELECT a.nickname AS muc_nick, u.user_id, u.nickname, u.ship_position
        FROM user_aliases a
        JOIN users u ON u.user_id=a.user_id
        WHERE u.consent_at IS NOT NULL
          AND u.password_hash IS NOT NULL
          AND a.nickname IN ({placeholders})
        """,
        clean,
    )
    result = {}
    for row in await cursor.fetchall():
        result[str(row["muc_nick"]).casefold()] = dict(row)
    return result


async def _resolve_user_by_nick(self, nickname):
    cursor = await self.conn.execute("""
        SELECT u.user_id, u.nickname
        FROM user_aliases a JOIN users u ON u.user_id=a.user_id
        WHERE a.nickname=? AND u.consent_at IS NOT NULL LIMIT 1
    """, (str(nickname).strip(),))
    row = await cursor.fetchone()
    return dict(row) if row else None


class FuzzyNickStatus:
    FOUND = "found"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"


class FuzzyNickMatch:
    """
    Результат _best_fuzzy_nick_match(): статус ЯВНО отделён от
    самого ника, чтобы AMBIGUOUS/NOT_FOUND нельзя было случайно
    перепутать с однозначно найденным кандидатом.
    """

    __slots__ = ("status", "nick", "score", "runner_up_score")

    def __init__(self, status, nick=None, score=0.0, runner_up_score=0.0):
        self.status = status
        self.nick = nick
        self.score = score
        self.runner_up_score = runner_up_score

    def __bool__(self):
        # Совместимость с прежним "if best_nick:" там, где интересен
        # только факт однозначной находки.
        return self.status == FuzzyNickStatus.FOUND


def _best_fuzzy_nick_match(text, candidates, threshold=None, margin=None):
    """
    Чистая (без I/O) функция сопоставления: ищет среди
    `candidates` (список ников) тот, что лучше всего похож на
    какое-то слово из `text` — нужно, чтобы склонённая форма
    ("с Верой", "у Максима") или опечатка в нике всё равно
    резолвилась в нужный аккаунт при RECALL, а не молча
    возвращала "ничего не нашли".

    Возвращает FuzzyNickMatch с одним из трёх статусов:
    - FOUND: лучший кандидат прошёл threshold И достаточно (>=
      margin) обогнал второго по score — то есть это реально ОН, а
      не "первый попавшийся похожий".
    - AMBIGUOUS: лучший кандидат прошёл threshold, но второй почти
      не отстаёт (например "Вера"/"Вера123"/"Верочка") — раньше это
      молча резолвилось в первого, что могло поднять досье не того
      человека.
    - NOT_FOUND: ни один кандидат не прошёл threshold вообще.

    Вынесена отдельно от resolve_user_by_fuzzy_nick(), чтобы
    smoke-тесты могли проверить сам алгоритм без реального
    aiosqlite (см. dogbot/smoke_test_recall_target.py) — тот же
    подход, что и у fuzzy-резолва автора цитаты
    (core.py:find_quote_author).
    """
    threshold = (
        threshold
        if threshold is not None
        else RECALL_FUZZY_NICK_THRESHOLD
    )
    margin = (
        margin
        if margin is not None
        else RECALL_FUZZY_NICK_MARGIN
    )

    tokens = [
        tok
        for tok in re.findall(
            r"[^\W\d_]+", str(text or "").casefold(), re.UNICODE,
        )
        if len(tok) >= 3
    ]

    if not tokens or not candidates:
        return FuzzyNickMatch(FuzzyNickStatus.NOT_FOUND)

    # Лучший score НА КАНДИДАТА (не на пару токен-ник) — иначе
    # длинный текст с несколькими похожими токенами дал бы одному
    # нику несколько "попыток" и исказил бы сравнение best/second
    # относительно остальных кандидатов.
    best_per_nick = {}

    for nick in candidates:

        nick_lower = str(nick).casefold()
        nick_best = 0.0

        # Быстрый путь: ник целиком встретился как подстрока
        # (например, склонение "Верой" содержит корень "вер",
        # но само точное имя "Вера" уже покрыто литеральным
        # поиском раньше в каскаде — сюда попадают более сложные
        # случаи, поэтому полагаемся на difflib ниже).
        for token in tokens:

            score = difflib.SequenceMatcher(
                None, token, nick_lower,
            ).ratio()

            if score > nick_best:
                nick_best = score

        best_per_nick[nick] = nick_best

    ranked = sorted(
        best_per_nick.items(), key=lambda item: item[1], reverse=True,
    )

    best_nick, best_score = ranked[0]

    if best_score < threshold:
        return FuzzyNickMatch(FuzzyNickStatus.NOT_FOUND, score=best_score)

    second_score = ranked[1][1] if len(ranked) > 1 else 0.0

    if best_score - second_score < margin:
        return FuzzyNickMatch(
            FuzzyNickStatus.AMBIGUOUS,
            nick=best_nick,
            score=best_score,
            runner_up_score=second_score,
        )

    return FuzzyNickMatch(
        FuzzyNickStatus.FOUND,
        nick=best_nick,
        score=best_score,
        runner_up_score=second_score,
    )


async def _resolve_user_by_fuzzy_nick(
    self, text, exclude_nicks=(), limit=None,
):
    """
    Фолбэк для resolve_user_by_nick(), когда в тексте нет точного
    совпадения ника: подтягивает ники согласившихся пользователей
    из БД и нечётко сверяет их со словами текста (см.
    _best_fuzzy_nick_match выше). Нужен для склонённых форм имени
    или опечаток — точный поиск (resolve_user_by_nick/
    find_mentioned_nick) такое не находит вообще, и recall раньше
    просто молча сдавался.

    Возвращает dict {"status", "user_id", "nickname"}, где status —
    один из FuzzyNickStatus.FOUND/AMBIGUOUS/NOT_FOUND (см.
    _best_fuzzy_nick_match). user_id/nickname заполнены только при
    FOUND. Раньше при AMBIGUOUS функция вела себя как при NOT_FOUND
    (возвращала None) — вызывающая сторона (llm.py) не могла их
    различить и в обоих случаях в итоге откатывалась на recall
    "про себя" (target=self), из-за чего похожие ники ("Вера"/
    "Вера123") могли поднять чужое досье. Теперь AMBIGUOUS передаётся
    явно, и recall обязан сообщить, что цель не определена, а не
    угадывать. exclude_nicks обычно = {sender, имя бота} — чтобы не
    "найти" в качестве цели recall самого автора или бота.
    """
    limit = (
        limit if limit is not None else RECALL_FUZZY_CANDIDATE_LIMIT
    )

    exclude_cf = {str(n).casefold() for n in exclude_nicks}

    cursor = await self.conn.execute(
        """
        SELECT nickname
        FROM users
        WHERE consent_at IS NOT NULL AND nickname IS NOT NULL
        ORDER BY last_updated DESC
        LIMIT ?
        """,
        (int(limit),),
    )

    rows = await cursor.fetchall()

    candidates = [
        row["nickname"]
        for row in rows
        if row["nickname"]
        and row["nickname"].casefold() not in exclude_cf
    ]

    match = _best_fuzzy_nick_match(text, candidates)

    if match.status == FuzzyNickStatus.NOT_FOUND:
        return {
            "status": FuzzyNickStatus.NOT_FOUND,
            "user_id": None,
            "nickname": None,
        }

    if match.status == FuzzyNickStatus.AMBIGUOUS:
        logging.info(
            "[RECALL] fuzzy nick AMBIGUOUS: best=%r score=%.3f "
            "runner_up_score=%.3f — цель НЕ резолвится однозначно",
            match.nick,
            match.score,
            match.runner_up_score,
        )
        return {
            "status": FuzzyNickStatus.AMBIGUOUS,
            "user_id": None,
            "nickname": None,
        }

    resolved = await self.resolve_user_by_nick(match.nick)

    if not resolved:
        return {
            "status": FuzzyNickStatus.NOT_FOUND,
            "user_id": None,
            "nickname": None,
        }

    return {
        "status": FuzzyNickStatus.FOUND,
        "user_id": resolved["user_id"],
        "nickname": resolved.get("nickname") or match.nick,
    }


async def _get_alias_for_user(self, user_id):
    """Самый свежий MUC-ник, привязанный к этому account_id (если есть)."""
    cursor = await self.conn.execute(
        "SELECT nickname FROM user_aliases WHERE user_id=? ORDER BY created_at DESC LIMIT 1",
        (user_id,),
    )
    row = await cursor.fetchone()
    return row["nickname"] if row else None


async def _revoke_consent(self, user_id):
    async with self.write_lock:
        await self.conn.execute("UPDATE users SET consent_at=NULL WHERE user_id=?", (user_id,))
        await self.conn.execute("DELETE FROM user_aliases WHERE user_id=?", (user_id,))
        # Неотправленные сообщения этого аккаунта больше не должны попасть в LLM.
        await self.conn.execute(
            "DELETE FROM context_messages WHERE user_id=? AND status IN ('pending','processing')",
            (user_id,),
        )
        await self.conn.commit()


async def _record_chat_archive_message(
    self,
    room_jid,
    sender_nick,
    sender_jid,
    body,
    created_at=None,
    user_id=None,
    stanza_id=None,
    mentions=None,
    quoted_text=None,
    quote_author=None,
    quote_stanza_id=None,
    is_command=False,
):
    """
    Сохраняет одну реплику MUC в короткий архив и одновременно
    индексирует упомянутые ники. Никаких LLM/семантических решений:
    архив — сырой источник истины, а релевантность определяет роутер.
    """
    if not CHAT_ARCHIVE_ENABLED:
        return False

    timestamp = created_at or now_iso()
    mentions = [
        str(n).strip()[:80]
        for n in (mentions or [])
        if str(n).strip()
    ]
    # Дедупликация без потери порядка.
    mentions = list(dict.fromkeys(mentions))

    async with self.write_lock:
        # XEP-0359 stanza-id — основной ключ идемпотентности. Он позволяет
        # безопасно импортировать серверную историю при каждом рестарте:
        # уже присутствующее сообщение не будет записано второй раз.
        if stanza_id:
            existing = await self.conn.execute(
                "SELECT id FROM chat_archive WHERE room_jid = ? AND stanza_id = ? LIMIT 1",
                (str(room_jid), str(stanza_id)),
            )
            if await existing.fetchone():
                return False
        else:
            # Некоторые MUC/MAM архивы не возвращают XEP-0359 stanza-id.
            # Тогда один и тот же message, пришедший сначала из MUC history,
            # а затем из MAM, не должен появиться дважды. Точный fallback
            # по комнате + отправителю + timestamp + телу достаточно строгий
            # и используется только когда stanza-id отсутствует.
            existing = await self.conn.execute(
                """
                SELECT id FROM chat_archive
                WHERE room_jid = ?
                  AND sender_nick = ?
                  AND created_at = ?
                  AND body = ?
                LIMIT 1
                """,
                (str(room_jid), str(sender_nick)[:80], str(timestamp), str(body)[:10000]),
            )
            if await existing.fetchone():
                return False

        cursor = await self.conn.execute(
            """
            INSERT INTO chat_archive (
                room_jid, sender_nick, sender_jid, user_id, body,
                created_at, stanza_id, mentions_json, quoted_text,
                quote_author, quote_stanza_id, is_command
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(room_jid),
                str(sender_nick)[:80],
                str(sender_jid)[:255] if sender_jid else None,
                str(user_id) if user_id else None,
                str(body)[:10000],
                str(timestamp),
                str(stanza_id)[:255] if stanza_id else None,
                json.dumps(mentions, ensure_ascii=False, separators=(",", ":")),
                str(quoted_text)[:10000] if quoted_text else None,
                str(quote_author)[:80] if quote_author else None,
                str(quote_stanza_id)[:255] if quote_stanza_id else None,
                1 if is_command else 0,
            ),
        )
        message_id = cursor.lastrowid

        if mentions:
            await self.conn.executemany(
                "INSERT OR IGNORE INTO chat_archive_mentions(message_id, nick) VALUES (?, ?)",
                [(message_id, nick) for nick in mentions],
            )

        # Retention enforced on every write, so the archive cannot grow
        # indefinitely even if the process never restarts.
        cutoff = (
            datetime.now() - timedelta(days=max(int(CHAT_ARCHIVE_RETENTION_DAYS), 1))
        ).isoformat(timespec="seconds")
        await self.conn.execute(
            "DELETE FROM chat_archive WHERE created_at < ?",
            (cutoff,),
        )
        await self.conn.commit()

    return True


async def _search_chat_archive(
    self,
    room_jid,
    target_date=None,
    target_end_date=None,
    nicks=None,
    terms=None,
    limit=None,
    neighbor_days=None,
):
    """
    Возвращает релевантные архивные сообщения за retention-период.

    Ключевой инвариант исторического поиска: если роутер дал
    target_date/target_end_date, сообщения внутри самого запрошенного
    дня (или диапазона) имеют ПРИОРИТЕТ над соседними днями. Иначе
    ORDER BY created_at ASC + LIMIT мог полностью заполнить выдачу
    предыдущим днём и фактически потерять тот день, который попросил
    пользователь. После целевого диапазона добираются ближайшие соседи.
    """
    limit = min(
        max(int(limit or CHAT_ARCHIVE_MAX_MESSAGES), 1),
        200,
    )
    neighbor_days = max(
        int(
            CHAT_ARCHIVE_NEIGHBOR_DAYS
            if neighbor_days is None
            else neighbor_days
        ),
        0,
    )

    def _date_bounds(start_date, end_date, expand=False):
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date or start_date, "%Y-%m-%d")
        if end < start:
            start, end = end, start
        if expand:
            start -= timedelta(days=neighbor_days)
            end += timedelta(days=neighbor_days)
        return (
            start.isoformat(timespec="seconds"),
            (end + timedelta(days=1)).isoformat(timespec="seconds"),
        )

    retention_cutoff = (
        datetime.now() - timedelta(days=max(int(CHAT_ARCHIVE_RETENTION_DAYS), 1))
    ).isoformat(timespec="seconds")

    nick_values = list(dict.fromkeys(
        str(n).strip() for n in (nicks or []) if str(n).strip()
    ))
    term_values = list(dict.fromkeys(
        str(t).strip().casefold()
        for t in (terms or [])
        if str(t).strip()
    ))

    async def _select(window_start, window_end, apply_relevance=True, exclude_ids=None):
        conditions = [
            "room_jid = ?",
            "created_at >= ?",
            "created_at >= ?",
            "created_at < ?",
            # Consent-граница: архив намеренно хранит сообщения и
            # незарегистрированных пользователей (user_id IS NULL —
            # для них персонализации/dossier нет вовсе, поэтому нет и
            # согласия, которое можно было бы отозвать), но сообщение
            # аккаунта, который явно НЕ дал (или отозвал) consent, не
            # должно попадать в контекст, уходящий в LLM — иначе
            # consent для памяти обходится через archive.
            "(user_id IS NULL OR EXISTS ("
            "SELECT 1 FROM users u "
            "WHERE u.user_id = chat_archive.user_id "
            "AND u.consent_at IS NOT NULL"
            "))",
        ]
        params = [str(room_jid), retention_cutoff, window_start, window_end]

        relevance = []
        if apply_relevance and nick_values:
            nick_cond = []
            for nick in nick_values:
                nick_cond.append("LOWER(sender_nick) = LOWER(?)")
                params.append(nick)
                nick_cond.append(
                    "EXISTS (SELECT 1 FROM chat_archive_mentions cam "
                    "WHERE cam.message_id = chat_archive.id AND LOWER(cam.nick) = LOWER(?))"
                )
                params.append(nick)
            relevance.append("(" + " OR ".join(nick_cond) + ")")

        if apply_relevance and term_values:
            term_cond = []
            for term in term_values[:8]:
                term_cond.append("LOWER(body) LIKE ?")
                params.append("%" + term + "%")
            relevance.append("(" + " OR ".join(term_cond) + ")")

        if relevance:
            conditions.append("(" + " OR ".join(relevance) + ")")

        if exclude_ids:
            placeholders = ",".join("?" for _ in exclude_ids)
            conditions.append(f"id NOT IN ({placeholders})")
            params.extend(exclude_ids)

        sql = f"""
            SELECT id, room_jid, sender_nick, sender_jid, user_id, body,
                   created_at, stanza_id, mentions_json, quoted_text,
                   quote_author, quote_stanza_id, is_command
            FROM chat_archive
            WHERE {' AND '.join(conditions)}
            ORDER BY created_at ASC, id ASC
            LIMIT ?
        """
        params.append(limit)
        cursor = await self.conn.execute(sql, params)
        return [dict(r) for r in await cursor.fetchall()]

    if target_date:
        try:
            # СНАЧАЛА строго целевой диапазон. Это гарантирует, что
            # соседний день не вытеснит искомый день из LIMIT.
            target_start, target_end = _date_bounds(
                str(target_date), str(target_end_date or target_date), expand=False
            )
            rows = await _select(
                target_start,
                target_end,
                apply_relevance=True,
            )

            # Если ники/термы сузили целевой день слишком сильно,
            # добираем остальные сообщения САМОГО целевого диапазона,
            # чтобы дать модели реальный контекст разговора.
            if rows and len(rows) < limit and (nick_values or term_values):
                base_rows = await _select(
                    target_start,
                    target_end,
                    apply_relevance=False,
                    exclude_ids=[int(r["id"]) for r in rows],
                )
                rows.extend(base_rows[: limit - len(rows)])

            # Только после полного/частичного целевого диапазона
            # добавляем соседние дни.
            if len(rows) < limit and neighbor_days > 0:
                expanded_start, expanded_end = _date_bounds(
                    str(target_date),
                    str(target_end_date or target_date),
                    expand=True,
                )
                neighbor_rows = await _select(
                    expanded_start,
                    expanded_end,
                    apply_relevance=False,
                    exclude_ids=[int(r["id"]) for r in rows],
                )
                rows.extend(neighbor_rows[: limit - len(rows)])

            rows.sort(key=lambda r: (str(r.get("created_at", "")), int(r.get("id", 0))))
            return rows[:limit]
        except ValueError:
            # Некорректная дата от роутера: безопасно переходим к
            # обычному ограниченному поиску по retention.
            target_date = None

    window_start = (
        datetime.now() - timedelta(days=max(int(CHAT_ARCHIVE_RETENTION_DAYS), 1))
    ).isoformat(timespec="seconds")
    window_end = (datetime.now() + timedelta(seconds=1)).isoformat(timespec="seconds")

    rows = await _select(
        window_start,
        window_end,
        apply_relevance=True,
    )

    # Для поиска без даты добираем соседние сообщения только вокруг
    # найденных результатов, а не случайные первые строки окна.
    if rows and len(rows) < limit and (nick_values or term_values):
        extra_rows = await _select(
            window_start,
            window_end,
            apply_relevance=False,
            exclude_ids=[int(r["id"]) for r in rows],
        )
        rows.extend(extra_rows[: limit - len(rows)])

    rows.sort(key=lambda r: (str(r.get("created_at", "")), int(r.get("id", 0))))
    return rows[:limit]


async def _search_recent_chat_archive(
    self,
    room_jid,
    since,
    nicks=None,
    limit=None,
):
    """
    Прицельная замена self.history для needs_history: последние
    реплики комнаты за окно [since, сейчас), в хронологическом
    порядке, опционально сузенные до конкретных участников (автор
    ИЛИ упоминание в mentions).

    В отличие от _search_chat_archive (needs_chat_archive — редкий,
    нишевый поиск по явной исторической дате с neighbor_days/terms),
    здесь нет date_range и словарных термов: needs_history — это
    самый частый флаг (почти каждое сообщение с продолжением
    разговора), поэтому запрос сознательно простой и дешёвый —
    один индексный скан по (room_jid, created_at) без OR-веток по
    терминам. См. HISTORY_ARCHIVE_* в config.py и
    core.py:get_recent_history_context.

    `since` — datetime (naive, как и остальные записи created_at в
    этой БД) или уже готовая ISO-строка.
    """
    limit = min(
        max(int(limit or HISTORY_ARCHIVE_MAX_MESSAGES), 1),
        200,
    )

    since_iso = (
        since.isoformat(timespec="seconds")
        if hasattr(since, "isoformat")
        else str(since)
    )

    # Даже явно широкое recency-окно никогда не должно вылезать за
    # retention архива — те строки уже могли быть удалены при записи
    # (см. retention DELETE в record_chat_archive_message выше).
    retention_cutoff = (
        datetime.now() - timedelta(days=max(int(CHAT_ARCHIVE_RETENTION_DAYS), 1))
    ).isoformat(timespec="seconds")
    window_start = max(since_iso, retention_cutoff)

    nick_values = list(dict.fromkeys(
        str(n).strip() for n in (nicks or []) if str(n).strip()
    ))

    conditions = [
        "room_jid = ?",
        "created_at >= ?",
        # См. аналогичное условие и комментарий в _search_chat_archive:
        # consent-граница должна работать одинаково для обоих archive-
        # источников контекста LLM (needs_chat_archive и needs_history).
        "(user_id IS NULL OR EXISTS ("
        "SELECT 1 FROM users u "
        "WHERE u.user_id = chat_archive.user_id "
        "AND u.consent_at IS NOT NULL"
        "))",
    ]
    params = [str(room_jid), window_start]

    if nick_values:
        nick_cond = []
        for nick in nick_values:
            nick_cond.append("LOWER(sender_nick) = LOWER(?)")
            params.append(nick)
            nick_cond.append(
                "EXISTS (SELECT 1 FROM chat_archive_mentions cam "
                "WHERE cam.message_id = chat_archive.id AND LOWER(cam.nick) = LOWER(?))"
            )
            params.append(nick)
        conditions.append("(" + " OR ".join(nick_cond) + ")")

    sql = f"""
        SELECT id, room_jid, sender_nick, sender_jid, user_id, body,
               created_at, stanza_id, mentions_json, quoted_text,
               quote_author, quote_stanza_id, is_command
        FROM chat_archive
        WHERE {' AND '.join(conditions)}
        ORDER BY created_at DESC, id DESC
        LIMIT ?
    """
    params.append(limit)
    cursor = await self.conn.execute(sql, params)
    rows = [dict(r) for r in await cursor.fetchall()]

    # Запрос сортирует DESC, чтобы LIMIT честно брал САМЫЕ свежие
    # строки (а не первые N с начала окна); для чтения диалога нужен
    # обратно хронологический порядок — как у self.history.
    rows.reverse()
    return rows


async def _find_chat_archive_quote_author(
    self,
    room_jid,
    quoted_text,
    limit=200,
):
    """
    Ищет автора текстовой цитаты в реальном архиве БД.

    Это принципиально отдельный фолбэк от in-memory quote_lookback:
    после рестарта lookback пуст, а цитата может относиться к сообщению
    недельной давности. Сначала дешёвый SQL LIKE ограничивает кандидатов,
    затем Python считает нормализованное точное/частичное совпадение и
    SequenceMatcher. Возвращается лучший автор только при уверенном
    совпадении.
    """
    if not quoted_text or len(str(quoted_text).strip()) < 4:
        return None

    norm_quote = re.sub(r"\s+", " ", str(quoted_text)).strip().casefold()
    if len(norm_quote) < 4:
        return None

    cutoff = (
        datetime.now() - timedelta(days=max(int(CHAT_ARCHIVE_RETENTION_DAYS), 1))
    ).isoformat(timespec="seconds")

    # LIKE использует первый достаточно длинный фрагмент как индексный
    # предфильтр. Если цитата содержит переносы/форматирование, берём
    # первый кусок из 12+ символов.
    fragment = norm_quote[:120]
    cursor = await self.conn.execute(
        """
        SELECT sender_nick, body, created_at, stanza_id
        FROM chat_archive
        WHERE room_jid = ?
          AND created_at >= ?
          AND LOWER(body) LIKE ?
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (str(room_jid), cutoff, "%" + fragment + "%", int(limit)),
    )
    rows = await cursor.fetchall()

    # Если длинный фрагмент не нашёлся из-за отличий форматирования,
    # повторяем по короткому устойчивому фрагменту.
    if not rows:
        fragment = norm_quote[:40]
        cursor = await self.conn.execute(
            """
            SELECT sender_nick, body, created_at, stanza_id
            FROM chat_archive
            WHERE room_jid = ?
              AND created_at >= ?
              AND LOWER(body) LIKE ?
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (str(room_jid), cutoff, "%" + fragment + "%", int(limit)),
        )
        rows = await cursor.fetchall()

    best = None
    best_score = 0.0
    for row in rows:
        body = re.sub(r"\s+", " ", str(row["body"] or "")).strip().casefold()
        if not body:
            continue
        if norm_quote == body:
            return {
                "sender": row["sender_nick"],
                "score": 1.0,
                "created_at": row["created_at"],
                "stanza_id": row["stanza_id"],
            }
        if norm_quote in body or body in norm_quote:
            score = min(len(norm_quote), len(body)) / max(len(norm_quote), len(body))
        else:
            score = difflib.SequenceMatcher(None, norm_quote, body).ratio()
        if score > best_score:
            best_score = score
            best = row

    if best is not None and best_score >= 0.78:
        return {
            "sender": best["sender_nick"],
            "score": best_score,
            "created_at": best["created_at"],
            "stanza_id": best["stanza_id"],
        }
    return None


async def _get_room_sender_stats(self, room_jid, sender_nick, sender_jid=None):
    """
    Лёгкая агрегатная статистика по chat_archive для ОДНОГО отправителя
    в ОДНОЙ комнате: когда он впервые здесь появился и сколько сообщений
    от него уже есть в архиве (за retention-окно, см.
    CHAT_ARCHIVE_RETENTION_DAYS). Используется исключительно
    модерацией (moderation.py: _mod_tenure) для того, чтобы отличать
    давнего участника от внезапно появившегося рейдера — п.9-11 в ТЗ
    анти-рейд системы.

    В отличие от search_chat_archive/search_recent_chat_archive
    (персональная память/LLM-контекст) здесь СОЗНАТЕЛЬНО нет фильтра
    по consent_at: защита комнаты от рейда — функция безопасности,
    а не персонализация, и не должна зависеть от того, дал ли
    конкретный участник согласие на использование его данных в LLM.
    Опрос "сколько раз здесь писал этот ник/jid" не подразумевает
    работы с содержимым его сообщений для LLM и не требует consent
    так же, как этого не требует, например, сам факт подсчёта событий
    в mod_events.

    room_jid + sender_nick обязательны; sender_jid опционален и
    расширяет совпадение также по occupant JID (устойчивее к смене
    ника тем же участником).
    """
    if not CHAT_ARCHIVE_ENABLED:
        return {"first_seen": None, "message_count": 0}

    nick = str(sender_nick or "").strip()[:80]
    if not nick:
        return {"first_seen": None, "message_count": 0}

    if sender_jid:
        condition = (
            "(LOWER(sender_nick) = LOWER(?) OR LOWER(sender_jid) = LOWER(?))"
        )
        params = [str(room_jid), nick, str(sender_jid)]
    else:
        condition = "LOWER(sender_nick) = LOWER(?)"
        params = [str(room_jid), nick]

    cursor = await self.conn.execute(
        f"""
        SELECT MIN(created_at) AS first_seen, COUNT(*) AS message_count
        FROM chat_archive
        WHERE room_jid = ? AND {condition}
        """,
        params,
    )
    row = await cursor.fetchone()
    if not row:
        return {"first_seen": None, "message_count": 0}
    return {
        "first_seen": row["first_seen"],
        "message_count": int(row["message_count"] or 0),
    }


async def _record_chat_message(self, user_id, nickname, body):
    if not await self.has_consent(user_id):
        return False
    async with self.write_lock:
        await self.conn.execute(
            "INSERT INTO context_messages(user_id,nickname,body,created_at) VALUES (?,?,?,?)",
            (user_id, str(nickname)[:80], str(body)[:10000], now_iso()),
        )
        await self.conn.commit()
    return True


async def _claim_message_batch(self, size=15):
    async with self.write_lock:
        cursor = await self.conn.execute("SELECT id,user_id,nickname,body,created_at FROM context_messages WHERE status='pending' ORDER BY id LIMIT ?", (int(size),))
        rows = await cursor.fetchall()
        if len(rows) < int(size):
            return []
        batch_id = _secrets.token_hex(12)
        ids = [int(r["id"]) for r in rows]
        placeholders = ",".join("?" for _ in ids)
        await self.conn.execute(
            f"UPDATE context_messages SET status='processing', batch_id=? WHERE id IN ({placeholders})",
            [batch_id] + ids,
        )
        await self.conn.commit()
        return [{**dict(r), "batch_id": batch_id} for r in rows]


async def _mark_message_batch(self, batch_id, success=True):
    async with self.write_lock:
        if success:
            await self.conn.execute("UPDATE context_messages SET status='sent', sent_at=? WHERE batch_id=?", (now_iso(), batch_id))
        else:
            await self.conn.execute("UPDATE context_messages SET status='pending', batch_id=NULL WHERE batch_id=?", (batch_id,))
        await self.conn.commit()


async def _release_message_batch(self, batch_id):
    await _mark_message_batch(self, batch_id, False)



async def _save_presence_snapshot(self, room_jid, users, captured_at=None):
    """Сохраняет снимок MUC и оставляет только последние 100 снимков."""
    if not self.conn:
        return False

    captured_at = captured_at or now_iso()

    # Нормализуем JSON, чтобы историческая запись была самодостаточной.
    clean_users = []
    for item in users or []:
        if isinstance(item, dict):
            clean_users.append({
                "nick": str(item.get("nick", ""))[:80],
                "occupant_jid": str(item.get("occupant_jid", ""))[:255],
                "user_id": item.get("user_id"),
                "registered": bool(item.get("registered", False)),
                "ship_position": item.get("ship_position"),
            })
        else:
            clean_users.append({
                "nick": str(item)[:80],
                "occupant_jid": "",
                "user_id": None,
                "registered": False,
            })

    payload = json.dumps(
        clean_users,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    async with self.write_lock:
        await self.conn.execute(
            """
            INSERT INTO room_presence_snapshots
                (room_jid, captured_at, users_json)
            VALUES (?, ?, ?)
            """,
            (str(room_jid), str(captured_at), payload),
        )
        # Проект рассчитан на одну комнату, но ограничение делаем
        # корректным и для нескольких комнат.
        await self.conn.execute(
            """
            DELETE FROM room_presence_snapshots
            WHERE room_jid = ?
              AND id NOT IN (
                  SELECT id
                  FROM room_presence_snapshots
                  WHERE room_jid = ?
                  ORDER BY id DESC
                  LIMIT 100
              )
            """,
            (str(room_jid), str(room_jid)),
        )
        await self.conn.commit()

    logging.info(
        "[PRESENCE] snapshot room=%s users=%d at=%s",
        room_jid, len(clean_users), captured_at,
    )
    return True


async def _get_latest_presence_snapshot(self, room_jid):
    cursor = await self.conn.execute(
        """
        SELECT id, room_jid, captured_at, users_json
        FROM room_presence_snapshots
        WHERE room_jid = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (str(room_jid),),
    )
    row = await cursor.fetchone()
    if not row:
        return None
    try:
        users = json.loads(row["users_json"])
        if not isinstance(users, list):
            users = []
    except Exception:
        users = []
    return {
        "id": int(row["id"]),
        "room_jid": row["room_jid"],
        "captured_at": row["captured_at"],
        "users": users,
    }


async def _get_presence_history(self, room_jid, limit=100):
    cursor = await self.conn.execute(
        """
        SELECT id, room_jid, captured_at, users_json
        FROM room_presence_snapshots
        WHERE room_jid = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (str(room_jid), min(max(int(limit), 1), 100)),
    )
    rows = await cursor.fetchall()
    result = []
    for row in rows:
        try:
            users = json.loads(row["users_json"])
            if not isinstance(users, list):
                users = []
        except Exception:
            users = []
        result.append({
            "id": int(row["id"]),
            "room_jid": row["room_jid"],
            "captured_at": row["captured_at"],
            "users": users,
        })
    return result


UserFactsDB.save_presence_snapshot = _save_presence_snapshot
UserFactsDB.get_latest_presence_snapshot = _get_latest_presence_snapshot
UserFactsDB.get_presence_history = _get_presence_history

async def _get_nearest_presence_snapshots(self, room_jid, target_time, limit=6):
    """Возвращает ближайшие к заданному моменту снимки presence, не весь JSON-архив."""
    cursor = await self.conn.execute(
        """
        SELECT id, room_jid, captured_at, users_json
        FROM room_presence_snapshots
        WHERE room_jid = ?
        ORDER BY ABS(strftime('%s', captured_at) - strftime('%s', ?)) ASC
        LIMIT ?
        """,
        (str(room_jid), str(target_time), min(max(int(limit), 1), 20)),
    )
    rows = await cursor.fetchall()
    result = []
    for row in rows:
        try:
            users = json.loads(row["users_json"])
            if not isinstance(users, list):
                users = []
        except Exception:
            users = []
        result.append({"id": int(row["id"]), "room_jid": row["room_jid"],
                       "captured_at": row["captured_at"], "users": users})
    return result

UserFactsDB.get_nearest_presence_snapshots = _get_nearest_presence_snapshots

UserFactsDB.init_privacy = _init_privacy
UserFactsDB.create_consent_record = _create_consent_record
UserFactsDB.has_consent_by_nick = _has_consent_by_nick
UserFactsDB.has_consent = _has_consent
UserFactsDB.set_consent = _set_consent
UserFactsDB.account_exists = _account_exists
UserFactsDB.get_account_by_nick = _get_account_by_nick
UserFactsDB.register_account = _register_account
UserFactsDB.verify_password = _verify_password
UserFactsDB.change_password = _change_password
UserFactsDB.verify_token = _verify_token
UserFactsDB.bind_nick = _bind_nick
UserFactsDB.get_ship_position = _get_ship_position
UserFactsDB.set_ship_position = _set_ship_position
UserFactsDB.get_crew_accounts = _get_crew_accounts
UserFactsDB.get_crew_by_nicks = _get_crew_by_nicks
UserFactsDB.resolve_user_by_nick = _resolve_user_by_nick
UserFactsDB.resolve_user_by_fuzzy_nick = _resolve_user_by_fuzzy_nick
UserFactsDB.get_alias_for_user = _get_alias_for_user
UserFactsDB.revoke_consent = _revoke_consent
UserFactsDB.record_chat_archive_message = _record_chat_archive_message
UserFactsDB.search_chat_archive = _search_chat_archive
UserFactsDB.search_recent_chat_archive = _search_recent_chat_archive
UserFactsDB.find_chat_archive_quote_author = _find_chat_archive_quote_author
UserFactsDB.get_room_sender_stats = _get_room_sender_stats
UserFactsDB.record_chat_message = _record_chat_message
UserFactsDB.claim_message_batch = _claim_message_batch
UserFactsDB.mark_message_batch = _mark_message_batch
UserFactsDB.release_message_batch = _release_message_batch


# ============================================================
# SEA HUNT 3.0 DB API
# ============================================================

async def _hunt_get_ships(self, hunt_id):
    cur = await self.conn.execute(
        "SELECT * FROM hunt_ships WHERE hunt_id=? ORDER BY id", (hunt_id,)
    )
    return [dict(r) for r in await cur.fetchall()]


async def _hunt_get_enemy_ships(self, hunt_id):
    cur = await self.conn.execute(
        "SELECT * FROM hunt_ships WHERE hunt_id=? AND side='enemy' AND status NOT IN ('sunk','captured') ORDER BY id",
        (hunt_id,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def _hunt_get_ship(self, ship_id):
    cur = await self.conn.execute("SELECT * FROM hunt_ships WHERE id=?", (ship_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def _hunt_clear_encounter(self, hunt_id):
    """Удаляет только незапущенный/подготовительный encounter для безопасного retry."""
    async with self.write_lock:
        await self.conn.execute("DELETE FROM hunt_boardings WHERE hunt_id=?", (hunt_id,))
        await self.conn.execute("DELETE FROM hunt_weapons WHERE hunt_id=?", (hunt_id,))
        await self.conn.execute("DELETE FROM hunt_npcs WHERE hunt_id=?", (hunt_id,))
        await self.conn.execute("DELETE FROM hunt_ships WHERE hunt_id=?", (hunt_id,))
        await self.conn.commit()


async def _hunt_create_ship(self, hunt_id, side, data):
    now = now_iso()
    async with self.write_lock:
        cur = await self.conn.execute("""
            INSERT INTO hunt_ships (
                hunt_id, side, name, class_name, hp, max_hp, sails,
                crew_morale, crew_count, distance, speed, heading, status,
                personality, tactics, ai_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            hunt_id, side, data["name"], data["class_name"], data["hp"],
            data["max_hp"], data["sails"], data["crew_morale"],
            0, data["distance"], data["speed"], data["heading"],
            data.get("status", "afloat"), data.get("personality", ""),
            data.get("tactics", "aggressive"),
            json.dumps(data.get("ai", {}), ensure_ascii=False), now,
        ))
        await self.conn.commit()
        return cur.lastrowid


async def _hunt_update_ship(self, ship_id, **fields):
    allowed = {
        "name", "class_name", "hp", "max_hp", "sails", "crew_morale",
        "crew_count", "distance", "speed", "heading", "status",
        "personality", "tactics", "ai_json", "fire_level",
        "crew_hp", "crew_max_hp", "registered_crew_count",
        "auxiliary_crew_count", "aux_total", "aux_healthy",
        "aux_wounded", "aux_unconscious", "aux_dead",
    }
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    clause = ", ".join(f"{k}=?" for k in fields)
    async with self.write_lock:
        await self.conn.execute(
            f"UPDATE hunt_ships SET {clause} WHERE id=?",
            [*fields.values(), ship_id],
        )
        await self.conn.commit()



async def _hunt_apply_ship_delta(self, ship_id, *, hp_delta=0, sails_delta=0,
                                 morale_delta=0, distance_delta=0,
                                 heading_delta=0, fire_delta=0):
    """Атомарный read-modify-write: SQL сам вычисляет новое состояние."""
    async with self.write_lock:
        await self.conn.execute("""
            UPDATE hunt_ships
            SET hp = MAX(0, MIN(max_hp, hp + ?)),
                sails = MAX(0, MIN(100, sails + ?)),
                crew_morale = MAX(0, MIN(100, crew_morale + ?)),
                distance = MAX(5, MIN(100, distance + ?)),
                heading = ((heading + ?) % 360 + 360) % 360,
                fire_level = MAX(0, MIN(100, fire_level + ?))
            WHERE id=?
        """, (int(hp_delta), int(sails_delta), int(morale_delta),
              int(distance_delta), int(heading_delta), int(fire_delta), ship_id))
        cur = await self.conn.execute("SELECT * FROM hunt_ships WHERE id=?", (ship_id,))
        row = await cur.fetchone()
        await self.conn.commit()
        return dict(row) if row else None


async def _hunt_claim_weapon_shot(self, weapon_id, now_text, reload_seconds):
    """Атомарно расходует боезапас и резервирует перезарядку."""
    ready_at = (datetime.fromisoformat(now_text) + timedelta(seconds=int(reload_seconds))).isoformat(timespec="seconds")
    async with self.write_lock:
        cur = await self.conn.execute("""
            UPDATE hunt_weapons
            SET ammo=ammo-1, ready_at=?, status='reloading'
            WHERE id=? AND ammo>0
              AND (ready_at IS NULL OR ready_at <= ?)
        """, (ready_at, weapon_id, now_text))
        if cur.rowcount != 1:
            await self.conn.commit()
            return False
        await self.conn.commit()
        return True


async def _hunt_get_ship_crew_summary(self, hunt_id, ship_id):
    cur = await self.conn.execute("""
        SELECT * FROM hunt_ships WHERE hunt_id=? AND id=?
    """, (hunt_id, ship_id))
    ship = await cur.fetchone()
    if not ship:
        return None
    ship = dict(ship)
    if ship["side"] == "player":
        cur = await self.conn.execute("""
            SELECT COUNT(*) AS n, COALESCE(SUM(hp),0) AS hp,\n                   COALESCE(SUM(CASE WHEN hp<=0 THEN 1 ELSE 0 END),0) AS dead\n            FROM hunt_player_state WHERE hunt_id=? AND status!='eliminated'
        """, (hunt_id,))
    else:
        cur = await self.conn.execute("""
            SELECT COUNT(*) AS n, COALESCE(SUM(hp),0) AS hp,\n                   COALESCE(SUM(CASE WHEN hp<=0 THEN 1 ELSE 0 END),0) AS dead\n            FROM hunt_npcs WHERE hunt_id=? AND ship_id=? AND status!='dead'
        """, (hunt_id, ship_id))
    crew = dict(await cur.fetchone())
    return {"ship": ship, "crew_count": int(crew["n"] or 0),
            "crew_hp": int(crew["hp"] or 0), "crew_dead": int(crew["dead"] or 0)}


async def _hunt_apply_crew_damage(self, hunt_id, ship_id, damage):
    """Распределяет урон по живому экипажу и синхронизирует агрегат корабля."""
    damage = max(0, int(damage))
    async with self.write_lock:
        cur = await self.conn.execute("SELECT side FROM hunt_ships WHERE id=? AND hunt_id=?", (ship_id, hunt_id))
        row = await cur.fetchone()
        if not row:
            return None
        if row["side"] == "player":
            cur = await self.conn.execute("""
                SELECT rowid, user_id, hp FROM hunt_player_state
                WHERE hunt_id=? AND status!='eliminated' AND hp>0 ORDER BY hp DESC, rowid
            """, (hunt_id,))
            members = [dict(r) for r in await cur.fetchall()]
            for idx, m in enumerate(members):
                if damage <= 0:
                    break
                # Равномерно распределяем ВЕСЬ оставшийся урон по
                # оставшимся живым членам. Старый вариант делил
                # исходный damage на полную длину списка на каждом
                # шаге, из-за чего 10 урона на 5 NPC превращались
                # в 6 фактического урона (2+1+1+1+1).
                members_left = len(members) - idx
                loss = min(
                    int(m["hp"]),
                    max(1, (damage + members_left - 1) // members_left),
                )
                damage -= loss
                new_hp = int(m["hp"]) - loss
                await self.conn.execute("UPDATE hunt_player_state SET hp=?, status=? WHERE rowid=?",
                                         (new_hp, 'eliminated' if new_hp <= 0 else 'free', m['rowid']))
        else:
            cur = await self.conn.execute("""
                SELECT id, hp FROM hunt_npcs WHERE hunt_id=? AND ship_id=? AND status='alive' ORDER BY hp DESC, id
            """, (hunt_id, ship_id))
            members = [dict(r) for r in await cur.fetchall()]
            for idx, m in enumerate(members):
                if damage <= 0:
                    break
                # Равномерно распределяем ВЕСЬ оставшийся урон по
                # оставшимся живым членам. Старый вариант делил
                # исходный damage на полную длину списка на каждом
                # шаге, из-за чего 10 урона на 5 NPC превращались
                # в 6 фактического урона (2+1+1+1+1).
                members_left = len(members) - idx
                loss = min(
                    int(m["hp"]),
                    max(1, (damage + members_left - 1) // members_left),
                )
                damage -= loss
                new_hp = int(m["hp"]) - loss
                await self.conn.execute("UPDATE hunt_npcs SET hp=?, status=? WHERE id=?",
                                         (new_hp, 'dead' if new_hp <= 0 else 'alive', m['id']))
        cur = await self.conn.execute("""
            SELECT COALESCE(SUM(hp),0) AS hp, COUNT(*) AS n
            FROM hunt_npcs WHERE hunt_id=? AND ship_id=? AND status='alive'
        """, (hunt_id, ship_id))
        enemy = dict(await cur.fetchone())
        if row["side"] == "player":
            cur = await self.conn.execute("""
                SELECT COALESCE(SUM(hp),0) AS hp, COUNT(*) AS n
                FROM hunt_player_state WHERE hunt_id=? AND status!='eliminated'
            """, (hunt_id,))
            player = dict(await cur.fetchone())
            # К агрегату зарегистрированного экипажа добавляем живой резервный состав.
            cur2 = await self.conn.execute(
                "SELECT aux_healthy FROM hunt_ships WHERE id=?", (ship_id,)
            )
            aux_row = await cur2.fetchone()
            aux_hp = int(aux_row["aux_healthy"] or 0) * 80 if aux_row else 0
            aux_healthy = int(aux_row["aux_healthy"] or 0) if aux_row else 0
            crew_hp, crew_count = int(player["hp"] or 0) + aux_healthy * 80, int(player["n"] or 0) + aux_healthy
            crew_max_hp = (int((await (await self.conn.execute("SELECT registered_crew_count FROM hunt_ships WHERE id=?", (ship_id,))).fetchone())["registered_crew_count"] or 0) * 100) + aux_healthy * 80
        else:
            crew_hp, crew_count = int(enemy["hp"] or 0), int(enemy["n"] or 0)
            crew_max_hp = crew_hp
        await self.conn.execute("UPDATE hunt_ships SET crew_hp=?, crew_max_hp=?, crew_count=? WHERE id=?",
                                (crew_hp, crew_max_hp, crew_count, ship_id))
        await self.conn.commit()
    return await self.hunt_get_ship(ship_id)


async def _hunt_assign_crew(self, hunt_id, user_id, role, assigned_by=None):
    async with self.write_lock:
        await self.conn.execute("""
            INSERT INTO hunt_crew_assignments(hunt_id,user_id,role,assigned_by,updated_at)
            VALUES(?,?,?,?,?) ON CONFLICT(hunt_id,user_id) DO UPDATE SET
              role=excluded.role, assigned_by=excluded.assigned_by, updated_at=excluded.updated_at
        """, (hunt_id, user_id, role, assigned_by, now_iso()))
        await self.conn.execute("UPDATE hunt_player_state SET hunt_role=? WHERE hunt_id=? AND user_id=?",
                                (role, hunt_id, user_id))
        await self.conn.commit()


async def _hunt_get_crew_role(self, hunt_id, user_id):
    cur = await self.conn.execute("SELECT role FROM hunt_crew_assignments WHERE hunt_id=? AND user_id=?",
                                  (hunt_id, user_id))
    row = await cur.fetchone()
    return row["role"] if row else None


async def _hunt_create_npc(self, hunt_id, ship_id, name, role, hp, personality="", traits=None):
    traits = traits or []
    async with self.write_lock:
        cur = await self.conn.execute("""
            INSERT INTO hunt_npcs (
                hunt_id, ship_id, name, role, hp, status,
                personality, traits_json, ai_json, created_at
            ) VALUES (?, ?, ?, ?, ?, 'alive', ?, ?, '{}', ?)
        """, (
            hunt_id, ship_id, name, role, hp, personality,
            json.dumps(traits[:8], ensure_ascii=False), now_iso(),
        ))
        await self.conn.execute(
            "UPDATE hunt_ships SET crew_count=crew_count+1 WHERE id=?",
            (ship_id,),
        )
        await self.conn.commit()
        return cur.lastrowid


async def _hunt_count_live_npcs(self, hunt_id, ship_id):
    cur = await self.conn.execute(
        "SELECT COUNT(*) AS n FROM hunt_npcs WHERE hunt_id=? AND ship_id=? AND status='alive'",
        (hunt_id, ship_id),
    )
    row = await cur.fetchone()
    return int(row["n"] or 0)


async def _hunt_create_weapon(self, hunt_id, ship_id, name, side, damage, range_, ammo, reload, ammo_type="cannonball"):
    async with self.write_lock:
        cur = await self.conn.execute("""
            INSERT INTO hunt_weapons (
                hunt_id, ship_id, name, side, damage, range,
                ammo, ammo_type, reload, ready_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'ready')
        """, (hunt_id, ship_id, name, side, damage, range_, ammo, str(ammo_type), reload))
        await self.conn.commit()
        return cur.lastrowid


async def _hunt_get_weapons(self, ship_id):
    cur = await self.conn.execute(
        "SELECT * FROM hunt_weapons WHERE ship_id=? ORDER BY id", (ship_id,)
    )
    return [dict(r) for r in await cur.fetchall()]


async def _hunt_update_weapon(self, weapon_id, **fields):
    allowed = {"ammo", "ammo_type", "reload", "ready_at", "status", "damage", "range"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    clause = ", ".join(f"{k}=?" for k in fields)
    async with self.write_lock:
        await self.conn.execute(
            f"UPDATE hunt_weapons SET {clause} WHERE id=?",
            [*fields.values(), weapon_id],
        )
        await self.conn.commit()


async def _hunt_try_claim_boarding(self, hunt_id, attacker_ship_id, defender_ship_id):
    """Один активный абордаж на конкретную цель, независимо от атакующего."""
    async with self.write_lock:
        cur = await self.conn.execute(
            "SELECT status FROM hunt_ships WHERE id=? AND hunt_id=?", (defender_ship_id, hunt_id))
        row = await cur.fetchone()
        if not row:
            return False, None, "no_target"
        if row["status"] in ("captured", "sunk"):
            return False, None, "already_taken"
        cur = await self.conn.execute("""
            SELECT id FROM hunt_boardings
            WHERE hunt_id=? AND defender_ship_id=? AND status='in_progress' LIMIT 1
        """, (hunt_id, defender_ship_id))
        if await cur.fetchone():
            return False, None, "boarding_in_progress"
        try:
            cur = await self.conn.execute("""
                INSERT INTO hunt_boardings(
                    hunt_id,attacker_ship_id,defender_ship_id,status,progress,
                    attacker_crew,defender_crew,started_at,resolved_at,result_json)
                VALUES(?,?,?,'in_progress',0,0,0,?,NULL,'{}')
            """, (hunt_id, attacker_ship_id, defender_ship_id, now_iso()))
            await self.conn.commit()
            return True, cur.lastrowid, None
        except Exception as exc:
            await self.conn.rollback()
            if "UNIQUE" in str(exc).upper():
                return False, None, "boarding_in_progress"
            raise



async def _hunt_try_claim_global_action(self, hunt_id, user_id, cooldown_seconds):
    """Единая очередь действий экипажа: одна физическая операция за cooldown."""
    now = hunt_now()
    now_text = now.isoformat(timespec='seconds')
    async with self.write_lock:
        cur = await self.conn.execute("SELECT last_action_at FROM hunt_action_gate WHERE hunt_id=?", (hunt_id,))
        row = await cur.fetchone()
        if row and row["last_action_at"]:
            try:
                last = datetime.fromisoformat(row["last_action_at"])
                if last.tzinfo is None: last = last.replace(tzinfo=timezone.utc)
                else: last = last.astimezone(timezone.utc)
                remaining = float(cooldown_seconds) - (now-last).total_seconds()
                if remaining > 0:
                    return False, int(remaining + .999)
            except (TypeError, ValueError):
                pass
        await self.conn.execute("""
            INSERT INTO hunt_action_gate(hunt_id,last_action_at,actor_id) VALUES(?,?,?)
            ON CONFLICT(hunt_id) DO UPDATE SET last_action_at=excluded.last_action_at, actor_id=excluded.actor_id
        """, (hunt_id, now_text, str(user_id)))
        await self.conn.commit()
        return True, 0

async def _hunt_resolve_boarding(
    self, boarding_id, status, progress, attacker_crew, defender_crew,
):
    async with self.write_lock:
        await self.conn.execute("""
            UPDATE hunt_boardings
            SET status=?, progress=?, attacker_crew=?, defender_crew=?, resolved_at=?
            WHERE id=?
        """, (status, progress, attacker_crew, defender_crew, now_iso(), boarding_id))
        await self.conn.commit()


async def _hunt_log_action(
    self, hunt_id, actor_type, actor_id, action, payload=None, result=None,
    entity_type=None,
):
    async with self.write_lock:
        await self.conn.execute("""
            INSERT INTO hunt_action_log (
                hunt_id, actor_type, actor_id, entity_type, action,
                payload_json, result_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            hunt_id, actor_type, str(actor_id) if actor_id is not None else None,
            entity_type, action,
            json.dumps(payload or {}, ensure_ascii=False),
            json.dumps(result or {}, ensure_ascii=False),
            now_iso(),
        ))
        await self.conn.commit()


async def _hunt_ship_inventory_get(self, hunt_id):
    cur = await self.conn.execute(
        "SELECT * FROM hunt_ship_inventory WHERE hunt_id=? ORDER BY id",
        (hunt_id,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def _hunt_ship_inventory_ensure(self, hunt_id, items):
    async with self.write_lock:
        for item in items:
            await self.conn.execute("""
                INSERT OR IGNORE INTO hunt_ship_inventory (
                    hunt_id, item_type, item_name, quantity,
                    effects_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                hunt_id,
                str(item["item_type"]),
                str(item["item_name"]),
                int(item.get("quantity", 0)),
                json.dumps(item.get("effects", {}), ensure_ascii=False),
                now_iso(),
                now_iso(),
            ))
        await self.conn.commit()
    return True



async def _hunt_ship_inventory_consume_many(self, hunt_id, requirements):
    """Атомарно проверяет и списывает несколько ресурсов одной операцией."""
    requirements = [(str(k), max(1, int(v))) for k, v in requirements if int(v) > 0]
    async with self.write_lock:
        for item_type, amount in requirements:
            cur = await self.conn.execute(
                "SELECT quantity FROM hunt_ship_inventory WHERE hunt_id=? AND item_type=?",
                (hunt_id, item_type),
            )
            row = await cur.fetchone()
            if not row or int(row["quantity"] or 0) < amount:
                await self.conn.rollback()
                return False
        stamp = now_iso()
        for item_type, amount in requirements:
            await self.conn.execute(
                "UPDATE hunt_ship_inventory SET quantity=quantity-?, updated_at=? WHERE hunt_id=? AND item_type=?",
                (amount, stamp, hunt_id, item_type),
            )
        await self.conn.commit()
        return True


async def _hunt_ship_inventory_consume(self, hunt_id, item_type, quantity=1):
    amount = max(1, int(quantity))
    async with self.write_lock:
        cur = await self.conn.execute("""
            UPDATE hunt_ship_inventory
            SET quantity = quantity - ?, updated_at = ?
            WHERE hunt_id = ? AND item_type = ? AND quantity >= ?
        """, (amount, now_iso(), hunt_id, str(item_type), amount))
        await self.conn.commit()
        return cur.rowcount == 1


UserFactsDB.hunt_clear_encounter = _hunt_clear_encounter
UserFactsDB.hunt_get_ships = _hunt_get_ships
UserFactsDB.hunt_get_enemy_ships = _hunt_get_enemy_ships
UserFactsDB.hunt_get_ship = _hunt_get_ship
UserFactsDB.hunt_create_ship = _hunt_create_ship
UserFactsDB.hunt_update_ship = _hunt_update_ship
UserFactsDB.hunt_apply_ship_delta = _hunt_apply_ship_delta
UserFactsDB.hunt_try_claim_global_action = _hunt_try_claim_global_action
UserFactsDB.hunt_claim_weapon_shot = _hunt_claim_weapon_shot
UserFactsDB.hunt_apply_crew_damage = _hunt_apply_crew_damage
UserFactsDB.hunt_assign_crew = _hunt_assign_crew
UserFactsDB.hunt_get_crew_role = _hunt_get_crew_role
UserFactsDB.hunt_get_ship_crew_summary = _hunt_get_ship_crew_summary
UserFactsDB.hunt_create_npc = _hunt_create_npc
UserFactsDB.hunt_count_live_npcs = _hunt_count_live_npcs
UserFactsDB.hunt_create_weapon = _hunt_create_weapon
UserFactsDB.hunt_get_weapons = _hunt_get_weapons
UserFactsDB.hunt_update_weapon = _hunt_update_weapon
UserFactsDB.hunt_try_claim_boarding = _hunt_try_claim_boarding
UserFactsDB.hunt_resolve_boarding = _hunt_resolve_boarding
UserFactsDB.hunt_log_action = _hunt_log_action
UserFactsDB.hunt_ship_inventory_get = _hunt_ship_inventory_get
UserFactsDB.hunt_ship_inventory_ensure = _hunt_ship_inventory_ensure
UserFactsDB.hunt_ship_inventory_consume = _hunt_ship_inventory_consume
UserFactsDB.hunt_ship_inventory_consume_many = _hunt_ship_inventory_consume_many
