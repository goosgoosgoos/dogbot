from .config import *
from . import hunt_world as world


class HuntMixin:
    """
    «Дикая Охота» — морская боевая сессия сеттинга корабля-призрака.

    Название/команды/планировщик сохраняются ради совместимости, но
    механика полностью переопределена:

      scheduled -> active -> finished
                         \
                          cancelled

    Игроки — экипаж нашего корабля.
    Враги — реальные NPC-корабли, NPC-экипаж и орудия, записанные в БД.

    Архитектура:
      LLM -> JSON намерение / генерация NPC -> sanitize -> DB
      DB  -> единственный источник истины
      code -> физика, попадания, перезарядка, абордаж, урон, движение
      LLM -> только интерпретация входа и атмосферный отыгрыш.

    Никаких «LLM решил, что пушка попала». Только движок меняет состояние.
    """

    _HUNT_STATUS_ORDER = ["free", "damaged", "boarded", "captured", "eliminated"]

    _HUNT_HINT_FALLBACKS = [
        "🌊 Море сегодня слишком тихое. Такая тишина обычно кому-то дорого обходится.",
        "⚓ На горизонте мелькнул силуэт. Или мне показалось. Лучше зарядить пушки.",
        "🌫️ Туман расступился на секунду — и там явно было что-то большое.",
        "☠️ Дальше будет не прогулка. Проверяйте порох, снасти и нервы.",
    ]

    _HUNT_START_FALLBACK = (
        "☠️ ДИКАЯ ОХОТА НАЧАЛАСЬ. Паруса на горизонте. "
        "Канонирам к орудиям, абордажной команде приготовиться. "
        "Сегодня охотимся мы."
    )

    _HUNT_FINISH_FALLBACK = (
        "🌊 Море снова молчит. Наш корабль остаётся на плаву, "
        "а тем, кто решил выйти против нас, сегодня не повезло."
    )

    _HUNT_INTENTS = (
        "FIRE_CANNON", "BOARD", "MANEUVER", "REPAIR",
        "COMMAND", "CREW_ACTION", "INSPECT", "USE_ITEM",
        "TALK", "OTHER",
        "ASSIGN_CREW", "INSPECT_CREW", "TREAT_CREW",
        "STABILIZE", "REVIVE", "ADMINISTER_STIMULANT",
        "USE_FOOD", "FIGHT_FIRE", "LOAD_CANNON",
        "CHANGE_AMMO", "AIM_CANNON", "TAKE_FROM_SHIP_INVENTORY",
    )

    # ---------------------------------------------------------
    # TIME / ROOM HELPERS
    # ---------------------------------------------------------

    @staticmethod
    def _hunt_now():
        """Текущее время Охоты в UTC (см. hunt_now() в config.py)."""
        return hunt_now()

    @staticmethod
    def _hunt_dt(value):
        """
        Разбирает timestamp Охоты (aware ISO-строку, старую naive
        строку или уже готовый datetime) и всегда возвращает
        UTC-aware datetime.

        Все текущие записи хранятся в UTC. Naive-ветка — только для
        совместимости со старыми строками, написанными до перехода
        на явный UTC; трактуем их как UTC (то же самое допущение,
        которое неявно делает now_iso() по всей остальной БД), а не
        как DOG_TIMEZONE — иначе для по-настоящему старых строк
        получим систематический сдвиг на DOG_BOT_TZ_OFFSET_HOURS часов.
        """
        if isinstance(value, datetime):
            dt = value
        else:
            dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def _hunt_public_room(self, room_jid=None):
        room = room_jid or getattr(self, "room", None)
        return str(room or "").split("/", 1)[0]

    def _hunt_lock(self, hunt_id):
        """Единая in-process очередь физики одной Охоты."""
        locks = getattr(self, "_hunt_event_locks", None)
        if locks is None:
            locks = {}
            self._hunt_event_locks = locks
        lock = locks.get(int(hunt_id))
        if lock is None:
            lock = asyncio.Lock()
            locks[int(hunt_id)] = lock
        return lock

    # ---------------------------------------------------------
    # LOOP
    # ---------------------------------------------------------

    async def hunt_loop(self):
        while True:
            try:
                await asyncio.sleep(HUNT_TICK_SECONDS)
                await self.hunt_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("[ОХОТА] Ошибка фонового цикла")

    async def hunt_tick(self):
        hunt = await self.db.get_current_hunt(self.room)
        if not hunt:
            return

        now = self._hunt_now()
        start_at = self._hunt_dt(hunt["start_at"])

        if hunt["status"] == "scheduled":
            if now >= start_at:
                try:
                    await self._hunt_begin(hunt)
                except Exception:
                    logging.exception("[ОХОТА] Ошибка перехода scheduled -> preparing hunt_id=%s", hunt["id"])
                    await self._hunt_safe_send(
                        self.room,
                        f"⚠️ Дикая Охота #{hunt['id']}: ошибка запуска. Повторю автоматически.",
                        "groupchat",
                    )
                return

            minutes_left = (start_at - now).total_seconds() / 60
            target_hints = sum(
                1 for offset in HUNT_HINT_OFFSETS_MINUTES
                if minutes_left <= offset
            )
            if target_hints > hunt["hints_sent"]:
                await self._hunt_send_hint(hunt, target_hints)
            return

        if hunt["status"] == "preparing":
            # Подготовка идемпотентна: если процесс умер во время LLM/БД,
            # следующий tick продолжит подготовку, а не оставит Охоту
            # навечно «активной» без кораблей.
            try:
                await self._hunt_prepare(hunt)
            except Exception:
                logging.exception("[ОХОТА] Ошибка подготовки hunt_id=%s", hunt["id"])
                await self._hunt_safe_send(
                    self.room,
                    f"⚠️ Дикая Охота #{hunt['id']}: подготовка не завершилась. Повторю автоматически.",
                    "groupchat",
                )
            return

        if hunt["status"] != "active":
            return

        ships = await self.db.hunt_get_ships(hunt["id"])
        player_ship = next((s for s in ships if s["side"] == "player"), None)
        enemies = [s for s in ships if s["side"] == "enemy" and s["status"] not in ("sunk", "captured")]
        if not player_ship or not enemies or int(player_ship.get("hp") or 0) <= 0:
            await self._hunt_finish(hunt)
            return

        started_at = self._hunt_dt(hunt["started_at"] or hunt["start_at"])
        if now >= started_at + timedelta(minutes=HUNT_DURATION_MINUTES):
            await self._hunt_finish(hunt)
            return

        next_raw = hunt.get("next_event_at")
        next_at = self._hunt_dt(next_raw) if next_raw else now
        if now >= next_at:
            await self._hunt_autonomous_event(hunt, player_ship, enemies)

    # ---------------------------------------------------------
    # PRE-START
    # ---------------------------------------------------------

    async def _hunt_send_hint(self, hunt, target_hints):
        instruction = {
            1: "Дай короткий тревожный намёк на приближение морской охоты. Не называй событие.",
            2: "Дай более заметный намёк: в тумане может быть вражеский корабль. Не объявляй бой.",
            3: "Дай почти открытый намёк: скоро экипажу придётся выйти против вражеских судов.",
            4: "Дай последний почти прямой намёк перед стартом, без фразы «Дикая Охота началась».",
        }.get(target_hints, "Дай короткий тревожный морской намёк.")

        prompt = f"""
Ты — Пёс, грубый, дерзкий, саркастичный член экипажа корабля-призрака.
Мат не используй. {instruction}
2-3 предложения, русский язык, без markdown и пояснений.
""".strip()
        try:
            raw = await self.call_openrouter(
                [{"role": "system", "content": prompt}],
                json_mode=False, timeout=25, rate_limit=False, tag="hunt_narrative",
            )
            text = (raw or "").strip()
        except Exception:
            logging.exception("[ОХОТА] Ошибка hint narration hunt_id=%s", hunt["id"])
            text = ""
        if not text:
            text = self._HUNT_HINT_FALLBACKS[min(target_hints, len(self._HUNT_HINT_FALLBACKS)) - 1]
        self.send_message(mto=self.room, mbody=text, mtype="groupchat")
        await self.db.mark_hunt_hints_sent(hunt["id"], target_hints)

    async def _hunt_begin(self, hunt):
        """Снимок участников и переход scheduled -> preparing.

        Никаких тяжёлых операций после перехода состояния: подготовка
        продолжается отдельным идемпотентным шагом.
        """
        hunt_id = hunt["id"]
        registered = await self.db.get_hunt_participants(hunt_id)
        present_ids = [
            p["user_id"] for p in registered
            if self._user_has_active_session(p["user_id"])
        ]

        confirmed = await self.db.snapshot_hunt_start(hunt_id, present_ids)
        if not confirmed:
            await self.db.finish_hunt(hunt_id)
            await self._hunt_safe_send(
                self.room,
                "⚓ Час пробил, но на палубе никого. Корабль уходит в туман без команды.",
                "groupchat",
            )
            return

        await self._hunt_safe_send(
            self.room,
            f"☠️ Дикая Охота #{hunt_id}: экипаж собран, готовлю корабли и цели...",
            "groupchat",
        )
        await self._hunt_prepare(hunt_id)

    async def _hunt_prepare(self, hunt_or_id):
        hunt_id = hunt_or_id["id"] if isinstance(hunt_or_id, dict) else int(hunt_or_id)
        hunt = await self.db.get_hunt_by_id(hunt_id)
        if not hunt or hunt["status"] not in ("preparing", "active"):
            return

        confirmed = await self.db.get_hunt_participants(hunt_id, confirmed_only=True)
        if not confirmed:
            await self.db.finish_hunt(hunt_id)
            await self._hunt_safe_send(self.room, "⚓ Охота отменена: подтверждённого экипажа нет.", "groupchat")
            return

        # Идемпотентность: не создаём второй набор кораблей при повторном tick.
        ships = await self.db.hunt_get_ships(hunt_id)
        player_ship = next((s for s in ships if s["side"] == "player"), None)
        enemy_ships = [s for s in ships if s["side"] == "enemy"]
        # Encounter writes are intentionally recoverable. If a process/LLM
        # failure left only a partial encounter, discard the incomplete
        # preparation and regenerate it; never activate a half-created battle.
        if not player_ship or len(enemy_ships) < 2:
            if ships:
                await self.db.hunt_clear_encounter(hunt_id)
            await self._hunt_generate_encounter(hunt, confirmed)
            ships = await self.db.hunt_get_ships(hunt_id)

        # Persistent world is generated once and never rewritten during the hunt.
        if not await self.db.get_hunt_world(hunt_id):
            await self._hunt_generate_world(hunt_id)
        if not await self.db.get_hunt_location(hunt_id, HUNT_START_LOCATION_KEY):
            await self._hunt_generate_world(hunt_id)

        # Inventory generation is also idempotent: only missing/empty player
        # profiles are filled, so a retry cannot reroll existing equipment.
        missing_inventory = []
        for participant in confirmed:
            ps = await self.db.get_hunt_player_state(hunt_id, participant["user_id"])
            if not ps or not world.load_json_list(ps.get("inventory_json")):
                missing_inventory.append(participant)
        if missing_inventory:
            inventories = await self._hunt_generate_inventories(missing_inventory)
            await self.db.ensure_hunt_player_states(
                hunt_id, missing_inventory,
                starting_location_key=HUNT_START_LOCATION_KEY,
                inventories=inventories,
            )

        player_ship = next((s for s in ships if s["side"] == "player"), None)
        enemies = [s for s in ships if s["side"] == "enemy" and s["status"] not in ("sunk", "captured")]
        if not player_ship or not enemies:
            raise RuntimeError(f"Hunt {hunt_id}: encounter generated without required ships")

        await self._hunt_init_crew_state(
            hunt_id, confirmed, player_ship["id"]
        )
        await self._hunt_ensure_ship_inventory(hunt_id)

        next_at = self._hunt_now() + timedelta(seconds=random.randint(HUNT_EVENT_MIN_SECONDS, HUNT_EVENT_MAX_SECONDS))
        if not hunt.get("next_event_at"):
            await self.db.update_hunt_pack_state(
                hunt_id,
                hound_count=len(enemies),
                rider_count=sum(int(s.get("crew_count") or 0) for s in enemies),
                primary_target_user_id=None,
                next_event_at=next_at.isoformat(timespec="seconds"),
            )

        activated = await self.db.activate_hunt(hunt_id)
        if not activated:
            return

        crew_context = await self._hunt_crew_context()
        world_flavor = await self._hunt_world_flavor(hunt_id)
        enemy_lines = "\n".join(
            f"- {s['name']} [{s['class_name']}], дистанция {s['distance']}, корпус {s['hp']}/{s['max_hp']}"
            for s in enemies
        )
        prompt = f"""
Ты — Пёс на борту корабля-призрака. «Дикая Охота» началась.
Наш корабль: {player_ship['name']}, корпус {player_ship['hp']}/{player_ship['max_hp']}.
Вражеские суда:
{enemy_lines}

{world_flavor or 'Обстановка: типичная для морской Дикой Охоты — туман и напряжение.'}

{crew_context}

Отыграй старт боя строго в этой обстановке (погода, время суток, место —
не выдумывай другие): сигналы, манёвр противника, напряжение на палубе.
Не меняй технические факты. Обратись к экипажу как к реальной команде.
2-5 предложений.
""".strip()
        try:
            raw = await self.call_openrouter(
                [{"role": "system", "content": prompt}],
                json_mode=False, timeout=30, rate_limit=False, tag="hunt_narrative",
            )
            text = (raw or "").strip() or self._HUNT_START_FALLBACK
        except Exception:
            logging.exception("[ОХОТА] Ошибка стартовой narration hunt_id=%s", hunt_id)
            text = self._HUNT_START_FALLBACK

        await self._hunt_safe_send(self.room, text, "groupchat")
        await self._hunt_safe_send(
            self.room,
            "⚓ БОЕВОЙ РЕЖИМ: !охота <действие>\n"
            "Примеры: «стреляю по шхуне», «идём на сближение», «готовлю абордаж», "
            "«чиню такелаж».\n!охота статус — состояние Охоты.",
            "groupchat",
        )

    def _hunt_safe_send(self, mto, mbody, mtype="chat"):
        try:
            self.send_message(mto=mto, mbody=mbody, mtype=mtype)
        except Exception:
            logging.exception("[ОХОТА] Не удалось отправить сообщение mto=%s", mto)

    async def _hunt_crew_context(self):
        if hasattr(self, "get_current_crew_context"):
            try:
                return await self.get_current_crew_context()
            except Exception:
                logging.exception("[ОХОТА] Не удалось получить контекст экипажа")
        return "ТЕКУЩИЙ СОСТАВ ЭКИПАЖА: недоступен."

    async def _hunt_init_crew_state(self, hunt_id, confirmed, player_ship_id):
        ship = await self.db.hunt_get_ship(player_ship_id)
        if not ship:
            return

        # Idempotent init only.
        if (
            int(ship.get("registered_crew_count") or 0) != 0
            or int(ship.get("auxiliary_crew_count") or 0) != 0
        ):
            return

        aux_total = HUNT_DEFAULT_AUXILIARY_CREW
        registered = len(confirmed)

        await self.db.hunt_update_ship(
            ship["id"],
            registered_crew_count=registered,
            auxiliary_crew_count=aux_total,
            aux_total=aux_total,
            aux_healthy=aux_total,
            crew_hp=registered * 100 + aux_total * 80,
            crew_max_hp=registered * 100 + aux_total * 80,
        )

    async def _hunt_ensure_ship_inventory(self, hunt_id):
        existing = await self.db.hunt_ship_inventory_get(hunt_id)
        if existing:
            return

        await self.db.hunt_ship_inventory_ensure(
            hunt_id, world.starting_ship_inventory()
        )

    async def _hunt_world_flavor(self, hunt_id):
        """
        Каноничное описание мира и стартовой локации этой Охоты —
        сгенерировано один раз в _hunt_generate_world и с тех пор
        не меняется. Раньше это писалось в БД (hunt_world,
        hunt_locations, hunt_facts) и там же и оставалось: ни один
        нарративный промпт этого не читал, и Пёс каждый раз выдумывал
        погоду/время суток заново, из-за чего они могли "плавать"
        от реплики к реплике в рамках одной Охоты.

        Теперь это единственная точка, откуда нарративные промпты
        берут обстановку — так весь бой (намёк уже не считая, он
        идёт до генерации мира) идёт в одной и той же придуманной
        сцене, а не в новой каждый раз.
        """
        world = await self.db.get_hunt_world(hunt_id)
        location = await self.db.get_hunt_location(hunt_id, HUNT_START_LOCATION_KEY)
        if not world and not location:
            return ""
        lines = []
        if world:
            lines.append(
                f"Регион: {world['region_name']}. Погода: {world['weather']}. "
                f"Время: {world['world_time']}. Уровень опасности: {world['danger_level']}/5."
            )
        if location:
            lines.append(f"Обстановка: {location['title']} — {location['description']}")
        return "\n".join(lines)

    async def _hunt_generate_encounter(self, hunt, confirmed):
        """
        Сначала код создаёт безопасный каркас. Затем LLM заполняет имена,
        классы, характеристики и NPC. После sanitize всё пишется в БД.
        """
        roster = []
        for p in confirmed:
            position = await self.db.get_ship_position(p["user_id"])
            roster.append({
                "user_id": p["user_id"],
                "nickname": p.get("nickname") or p["user_id"],
                "ship_position": position or "матрос",
            })

        player_name = "Призрачный корабль"
        seed = {
            "enemy_ships": random.randint(2, 3),
            "crew": roster,
            "classes": ["шхуна", "бригантина", "корвет"],
        }
        prompt = f"""
Создай боевую сцену для текстовой RPG про корабль-призрак в море опасностей.
Это НЕ лес и НЕ фантастическая охота на игроков. Игроки — экипаж нашего судна,
а цель — реальные вражеские корабли, которые можно расстреливать и брать на абордаж.

Программный каркас: {json.dumps(seed, ensure_ascii=False)}

Верни ТОЛЬКО JSON:
{{
  "player_ship": {{"name":"...", "class_name":"...", "hp":0, "sails":0, "crew_morale":0}},
  "enemy_ships": [
    {{
      "name":"...", "class_name":"шхуна|бригантина|корвет|фрегат",
      "hp": 0, "sails": 0, "crew_morale": 0,
      "distance": 0,
      "personality":"...",
      "tactics":"aggressive|cautious|boarding|escape",
      "crew":[
        {{"name":"...", "role":"captain|gunner|marine|sailor|medic",
          "hp":0, "traits":["..."]}}
      ],
      "weapons":[
        {{"name":"...", "side":"port|starboard|bow|stern",
          "damage":0, "range":0, "ammo":0, "reload":0}}
      ]
    }}
  ]
}}
"""
        raw = await self.call_openrouter(
            [{"role": "system", "content": prompt}],
            json_mode=True, timeout=35, rate_limit=False,
            tag="hunt_world",
        )
        parsed = extract_json_object(raw) or {}
        enemies_raw = parsed.get("enemy_ships") if isinstance(parsed, dict) else None
        if not isinstance(enemies_raw, list):
            enemies_raw = []

        player_raw = parsed.get("player_ship") if isinstance(parsed, dict) else {}
        if not isinstance(player_raw, dict):
            player_raw = {}

        player_ship = await self.db.hunt_create_ship(
            hunt["id"], "player",
            self._sanitize_ship(player_raw, player_name, "фрегат", 1000, 100, 75, 60),
        )

        # Корабль игроков тоже получает физические орудия.
        # Без этого парсер мог распознать FIRE_CANNON, но движку нечем было стрелять.
        player_weapon_defaults = {
            "weapons": [
                {"name": "Левый борт", "side": "port", "damage": 95, "range": 85, "ammo": 8, "reload": 45},
                {"name": "Правый борт", "side": "starboard", "damage": 95, "range": 85, "ammo": 8, "reload": 45},
                {"name": "Носовое орудие", "side": "bow", "damage": 75, "range": 70, "ammo": 4, "reload": 55},
            ]
        }
        await self._hunt_store_ship_weapons(hunt["id"], player_ship, player_weapon_defaults)

        # Реальный экипаж комнаты учитывается в корабельном состоянии.
        await self.db.hunt_update_ship(
            player_ship,
            crew_count=len(confirmed),
        )

        enemy_count = max(2, min(4, len(enemies_raw) or seed["enemy_ships"]))
        for idx in range(enemy_count):
            raw_enemy = enemies_raw[idx] if idx < len(enemies_raw) else {}
            if not isinstance(raw_enemy, dict):
                raw_enemy = {}
            enemy = self._sanitize_ship(
                raw_enemy,
                f"Тёмный борт №{idx + 1}",
                random.choice(seed["classes"]),
                random.randint(420, 780),
                random.randint(60, 100),
                random.randint(35, 80),
                random.randint(55, 90),
            )
            ship_id = await self.db.hunt_create_ship(hunt["id"], "enemy", enemy)
            await self._hunt_store_enemy_npcs(hunt["id"], ship_id, raw_enemy, idx)
            await self._hunt_store_ship_weapons(hunt["id"], ship_id, raw_enemy)
            await self.db.hunt_apply_crew_damage(hunt["id"], ship_id, 0)

        await self.db.hunt_log_action(
            hunt["id"], "system", "system", "ENCOUNTER_CREATED",
            {"player_ship_id": player_ship, "enemy_count": enemy_count},
            {"ok": True},
        )

        # Старое player_state сохраняется как совместимый профиль участника,
        # но фактическая боевая физика теперь принадлежит ship_* сущностям.
        await self.db.ensure_hunt_player_states(
            hunt["id"], confirmed,
            starting_location_key=HUNT_START_LOCATION_KEY,
            inventories={},
        )
        await self.db.hunt_apply_crew_damage(hunt["id"], player_ship, 0)

    @staticmethod
    def _sanitize_ship(raw, fallback_name, fallback_class, hp, sails, morale, distance):
        raw = raw if isinstance(raw, dict) else {}
        def n(key, default, lo, hi):
            try: value = int(raw.get(key, default))
            except (TypeError, ValueError): value = default
            return max(lo, min(hi, value))
        name = str(raw.get("name") or fallback_name).strip()[:70] or fallback_name
        cls = str(raw.get("class_name") or fallback_class).strip()[:30] or fallback_class
        max_hp = n("hp", hp, 200, 2000)
        return {
            "name": name,
            "class_name": cls,
            "hp": max_hp,
            "max_hp": max_hp,
            "sails": n("sails", sails, 0, 100),
            "crew_morale": n("crew_morale", morale, 0, 100),
            "distance": n("distance", distance, 10, 100),
            "speed": n("speed", random.randint(25, 55), 0, 100),
            "heading": n("heading", random.randint(0, 359), 0, 359),
            "status": "afloat",
            "personality": str(raw.get("personality") or "молчаливый противник")[:300],
            "tactics": str(raw.get("tactics") or "aggressive")[:30],
        }

    async def _hunt_store_enemy_npcs(self, hunt_id, ship_id, raw, idx):
        crew = raw.get("crew") if isinstance(raw, dict) else []
        if not isinstance(crew, list):
            crew = []
        if not crew:
            crew = [
                {"name": f"Капитан {idx + 1}", "role": "captain", "hp": 100, "traits": ["жёсткий"]},
                {"name": f"Канонир {idx + 1}", "role": "gunner", "hp": 80, "traits": ["меткий"]},
                {"name": f"Десантник {idx + 1}", "role": "marine", "hp": 90, "traits": ["агрессивный"]},
            ]
        for item in crew[:12]:
            if not isinstance(item, dict):
                continue
            await self.db.hunt_create_npc(
                hunt_id, ship_id,
                str(item.get("name") or "Безымянный матрос")[:60],
                str(item.get("role") or "sailor")[:30],
                max(1, min(100, int(item.get("hp") or 80))),
                str(item.get("personality") or "")[:300],
                item.get("traits") if isinstance(item.get("traits"), list) else [],
            )

    async def _hunt_store_ship_weapons(self, hunt_id, ship_id, raw):
        weapons = raw.get("weapons") if isinstance(raw, dict) else []
        if not isinstance(weapons, list):
            weapons = []
        if not weapons:
            weapons = [
                {"name": "Левый борт", "side": "port", "damage": 90, "range": 80, "ammo": 6, "reload": 45},
                {"name": "Правый борт", "side": "starboard", "damage": 90, "range": 80, "ammo": 6, "reload": 45},
            ]
        for w in weapons[:8]:
            if not isinstance(w, dict):
                continue
            await self.db.hunt_create_weapon(
                hunt_id, ship_id,
                str(w.get("name") or "орудие")[:60],
                str(w.get("side") or "port")[:20],
                max(10, min(250, int(w.get("damage") or 70))),
                max(10, min(100, int(w.get("range") or 70))),
                max(0, min(99, int(w.get("ammo") or 6))),
                max(10, min(300, int(w.get("reload") or 45))),
            )



    # ---------------------------------------------------------
    # LEGACY COMPATIBILITY HELPERS
    # ---------------------------------------------------------
    # Сохранены намеренно: они не участвуют в морской боевой петле,
    # но не удаляют существующий внутренний API HuntMixin.
    async def _hunt_personal_flavor(
        self,
        confirmed,
    ):
        """
        Для нескольких случайных подтверждённых участников
        достаёт по 1 факту через тот же select_memory_context,
        что используется в обычном диалоге (ambient memory).
        """

        sample = random.sample(
            confirmed,
            min(
                len(confirmed),
                HUNT_PERSONALIZED_PARTICIPANTS,
            ),
        )

        lines = []

        for participant in sample:

            facts = (
                await self.db.select_memory_context(
                    participant["user_id"],
                    min_facts=1,
                    max_facts=1,
                )
            )

            if not facts:
                continue

            nickname = (
                participant["nickname"]
                or participant["user_id"]
            )

            lines.append(
                f"- {nickname}: "
                f"{facts[0]['fact']}"
            )

        return lines

    # ========================================================
    # PHASE: BEGIN — world & inventory generation (Охота 2.0)
    # ========================================================
    #
    # Один раз при старте: LLM предлагает мир и снаряжение, код
    # (hunt_world.py) валидирует и фиксирует это как канон.
    # Дальше по ходу Охоты мир может только расширяться новыми
    # локациями/фактами — не переписываться задним числом.

    async def _hunt_generate_world(self, hunt_id):
        prompt = """
Ты помогаешь спроектировать мир для текстовой RPG "Дикая
Охота" на одну игровую сессию. Придумай мрачный участок моря для корабельной RPG
(не используй реальные географические названия).

Верни ТОЛЬКО JSON, без пояснений и без markdown:
{
  "region_name": "название моря/акватории",
  "weather": "короткое описание погоды",
  "world_time": "который час/период ночи",
  "danger_level": 1-5,
  "start_location": {
    "title": "название бухты/акватории/морского участка",
    "description": "2-4 предложения, атмосферно, про море и навигацию",
    "tags": ["1-4 коротких факта об этом месте, латиницей, snake_case"],
    "exits": ["2-4 коротких морских направления, например 'на восток вдоль рифов', 'в открытое море', 'к дальней бухте'"]
  }
}
""".strip()

        raw = await self.call_openrouter(
            [
                {
                    "role": "system",
                    "content": prompt,
                }
            ],
            json_mode=True,
            timeout=30,
            rate_limit=False,
        )

        parsed = extract_json_object(raw)

        if not isinstance(parsed, dict):
            parsed = {}

        world_data = world.sanitize_world(parsed)

        start_location = world.sanitize_location(
            parsed.get("start_location")
        )

        await self.db.create_hunt_world(
            hunt_id,
            world_data["region_name"],
            world_data["weather"],
            world_data["world_time"],
            world_data["danger_level"],
        )

        await self.db.create_hunt_location(
            hunt_id,
            HUNT_START_LOCATION_KEY,
            start_location["title"],
            start_location["description"],
            start_location["exits"],
            start_location["tags"],
        )

        await self.db.add_hunt_fact(
            hunt_id,
            (
                f"Место действия: {world_data['region_name']}. "
                f"Погода: {world_data['weather']}. "
                f"Время: {world_data['world_time']}."
            ),
            fact_key="world_intro",
            location_key=None,
            visibility="public",
            created_by="system",
        )

        return world_data, start_location

    async def _hunt_generate_inventories(self, confirmed):
        names = [
            p["nickname"] or p["user_id"]
            for p in confirmed
        ]

        prompt = f"""
Придумай стартовое снаряжение для каждого из этих игроков
текстовой RPG "Дикая Охота": {", ".join(names)}.

Для каждого игрока — 2-4 предмета (оружие, броня, расходники).
Не выдавай всем одинаковый набор дословно.

Верни ТОЛЬКО JSON, без пояснений: объект, где ключ — это ник
игрока ровно как указано выше, а значение — список предметов:
{{
  "ник игрока": [
    {{"name": "название", "tags": ["melee|ranged|armor|utility|heal|rope|food|tool"], "damage": 0-20, "durability": 1-100, "uses": 1-5}}
  ]
}}
""".strip()

        raw = await self.call_openrouter(
            [
                {
                    "role": "system",
                    "content": prompt,
                }
            ],
            json_mode=True,
            timeout=30,
            rate_limit=False,
        )

        parsed = extract_json_object(raw)

        inventories = {}

        lookup = {
            (p["nickname"] or p["user_id"]): p["user_id"]
            for p in confirmed
        }

        if isinstance(parsed, dict):

            for name_key, raw_items in parsed.items():

                user_id = lookup.get(name_key)

                if not user_id:

                    close = difflib.get_close_matches(
                        name_key,
                        list(lookup.keys()),
                        n=1,
                        cutoff=0.6,
                    )

                    if close:
                        user_id = lookup.get(close[0])

                if not user_id:
                    continue

                sanitized = world.sanitize_inventory(
                    raw_items
                )

                if sanitized:
                    inventories[user_id] = sanitized

        for participant in confirmed:

            user_id = participant["user_id"]

            if user_id not in inventories:
                inventories[user_id] = (
                    world.random_starting_inventory()
                )

        return inventories

    # ========================================================
    # PHASE: ACTIVE — player actions
    # ========================================================

    async def _hunt_use_item(self, hunt, state, own, parsed):
        """
        Использование личного снаряжения экипажа в бою.

        Предмет должен реально существовать в inventory_json игрока —
        LLM не может выдать вещь "из воздуха", она только подсказывает,
        какой предмет игрок имел в виду (weapon_hint/command).
        Эффект предмета всегда прикладывается к нашему кораблю: лечение
        подлатывает раненых и поднимает боевой дух, инструменты/такелаж
        ускоряют ремонт, оружие и прочее снаряжение просто поднимает
        боевой дух экипажа, который его достал. Использование тратит
        один заряд предмета; при исчерпании uses предмет пропадает из
        инвентаря.
        """
        if not own:
            return {"success": False, "reason": "no_player_ship"}

        inventory = world.load_json_list(state.get("inventory_json"))
        hint = parsed.get("weapon_hint") or parsed.get("command") or ""
        item = world.find_item(inventory, hint)
        if not item:
            return {"success": False, "reason": "item_not_found", "hint": hint}

        effect = {}
        if world.has_tag(item, "heal"):
            hp_gain = min(HUNT_HEAL_ITEM_AMOUNT, max(0, int(own["max_hp"]) - int(own["hp"])))
            morale_gain = min(15, max(0, 100 - int(own["crew_morale"])))
            if hp_gain or morale_gain:
                await self.db.hunt_apply_ship_delta(own["id"], hp_delta=hp_gain, morale_delta=morale_gain)
            effect = {"hp_gain": hp_gain, "morale_gain": morale_gain}
        elif world.has_tag(item, "tool") or world.has_tag(item, "rope") or world.has_tag(item, "utility"):
            sails_gain = min(12, max(0, 100 - int(own["sails"])))
            if sails_gain:
                await self.db.hunt_apply_ship_delta(own["id"], sails_delta=sails_gain)
            effect = {"sails_gain": sails_gain}
        else:
            morale_gain = min(6, max(0, 100 - int(own["crew_morale"])))
            if morale_gain:
                await self.db.hunt_apply_ship_delta(own["id"], morale_delta=morale_gain)
            effect = {"morale_gain": morale_gain}

        new_inventory = world.consume_item_use(inventory, item["id"])
        await self.db.update_hunt_player_state(
            hunt["id"], state["user_id"],
            inventory_json=json.dumps(new_inventory, ensure_ascii=False),
        )

        await self.db.hunt_log_action(
            hunt["id"], "player", state["user_id"], "USE_ITEM",
            {"item_id": item["id"], "item_name": item["name"]},
            {"effect": effect},
        )

        return {
            "success": True, "action": "USE_ITEM",
            "item": item["name"], "effect": effect,
        }



    # ========================================================
    # SESSION HELPERS
    # ========================================================

    # ---------------------------------------------------------
    # ACTION PARSER
    # ---------------------------------------------------------

    async def _hunt_parse_action(self, action_text, hunt_id):
        ships = await self.db.hunt_get_ships(hunt_id)
        crew_context = await self._hunt_crew_context()
        enemy_names = ", ".join(s["name"] for s in ships if s["side"] == "enemy")
        prompt = f"""
Ты интерпретатор действий в морской RPG. Текст игрока НЕ является фактом.
Определи только намерение и параметры. Исход определит программа.

Действие: "{action_text}"
Вражеские корабли: {enemy_names or "нет"}
{crew_context}

JSON:
{{
 "intent":"FIRE_CANNON|BOARD|MANEUVER|REPAIR|COMMAND|CREW_ACTION|INSPECT|USE_ITEM|TALK|ASSIGN_CREW|INSPECT_CREW|TREAT_CREW|STABILIZE|REVIVE|ADMINISTER_STIMULANT|USE_FOOD|FIGHT_FIRE|LOAD_CANNON|CHANGE_AMMO|AIM_CANNON|TAKE_FROM_SHIP_INVENTORY|OTHER",
 "target_ship":"точное имя цели или null",
 "weapon_hint":"какое орудие/борт или null",
 "direction":"left|right|ahead|astern|none",
 "command_target":"ник/должность члена экипажа или null",
 "command":"короткая суть приказа или null",
 "approach":"FORCE|TACTICAL|STEALTH|NEUTRAL"
}}

FIRE_CANNON — стрелять из корабельного орудия.
BOARD — сблизиться/начать абордаж.
MANEUVER — изменить курс/скорость/дистанцию.
REPAIR — ремонт корпуса/парусов.
COMMAND — приказ члену экипажа; не создавай новых людей.
CREW_ACTION — действие экипажа по должности.
USE_ITEM — использовать личный предмет из своего снаряжения (weapon_hint = название предмета).
TALK — просто говорить/переговоры, без физического действия.
ASSIGN_CREW — назначить реального участника на должность; command_target = ник, command = должность.
INSPECT_CREW — осмотреть состояние экипажа.
TREAT_CREW — лечить раненого реального участника; command_target = ник.
STABILIZE — стабилизировать тяжело раненого участника.
REVIVE — вернуть в бой выбывшего участника, если это ещё возможно.
ADMINISTER_STIMULANT — применить корабельный стимулятор к участнику.
USE_FOOD — раздать провизию экипажу.
FIGHT_FIRE — тушить пожар корабельной водой/ресурсами.
LOAD_CANNON — зарядить конкретное орудие.
CHANGE_AMMO — сменить тип боеприпаса.
AIM_CANNON — улучшить точность следующего выстрела.
TAKE_FROM_SHIP_INVENTORY — взять расходник из корабельного склада.
"""
        raw = await self.call_openrouter(
            [{"role": "system", "content": prompt}],
            json_mode=True, timeout=20, rate_limit=False, tag="hunt_action",
        )
        parsed = extract_json_object(raw) or {}
        intent = str(parsed.get("intent") or "OTHER").upper()
        if intent not in self._HUNT_INTENTS:
            intent = "OTHER"

        def clean(v):
            return v.strip()[:100] if isinstance(v, str) and v.strip() else None

        return {
            "intent": intent,
            "target_ship": clean(parsed.get("target_ship")),
            "weapon_hint": clean(parsed.get("weapon_hint")),
            "direction": clean(parsed.get("direction")) or "none",
            "command_target": clean(parsed.get("command_target")),
            "command": clean(parsed.get("command")),
            "approach": str(parsed.get("approach") or "NEUTRAL").upper(),
        }

    # ---------------------------------------------------------
    # RESOLUTION
    # ---------------------------------------------------------

    async def _hunt_resolve_action(self, hunt, state, parsed, role=None):
        ships = await self.db.hunt_get_ships(hunt["id"])
        own = next((s for s in ships if s["side"] == "player"), None)
        enemies = [s for s in ships if s["side"] == "enemy" and s["status"] not in ("sunk", "captured")]
        if not own:
            return {"success": False, "reason": "no_player_ship"}

        if role is None:
            assigned = await self.db.hunt_get_crew_role(hunt["id"], state["user_id"])
            if assigned:
                role = assigned
            else:
                position = await self.db.get_ship_position(state["user_id"])
                role = world.get_role_for_ship_position(position) if position else world.ROLE_CREW

        intent = parsed["intent"]

        if not world.can_role_do(role, intent):
            return {
                "success": False,
                "reason": "role_not_allowed",
                "role": role,
                "action": intent,
            }
        if intent == "FIRE_CANNON":
            return await self._hunt_fire_cannon(hunt, own, enemies, parsed)
        if intent == "BOARD":
            return await self._hunt_board(hunt, own, enemies, parsed)
        if intent == "MANEUVER":
            return await self._hunt_maneuver(hunt, own, enemies, parsed)
        if intent == "REPAIR":
            return await self._hunt_repair(hunt, own, parsed)
        if intent == "COMMAND":
            return await self._hunt_command_crew(hunt, own, parsed)
        if intent == "CREW_ACTION":
            return await self._hunt_crew_action(hunt, own, parsed)
        if intent == "INSPECT":
            return {"success": True, "inspection": True, "ships": ships}
        if intent == "USE_ITEM":
            return await self._hunt_use_item(hunt, state, own, parsed)
        if intent in {"ASSIGN_CREW","INSPECT_CREW","TREAT_CREW","STABILIZE","REVIVE","ADMINISTER_STIMULANT","USE_FOOD","FIGHT_FIRE","LOAD_CANNON","CHANGE_AMMO","AIM_CANNON","TAKE_FROM_SHIP_INVENTORY"}:
            return await self._hunt_feature_action(hunt, state, own, parsed, role)
        if intent == "TALK":
            return {"success": True, "action": "TALK", "text": parsed.get("command") or ""}
        return {"success": False, "reason": "not_implemented", "action": intent}

    async def _resolve_target(self, enemies, hint):
        if not enemies:
            return None
        if hint:
            h = hint.casefold()
            for s in enemies:
                if h in s["name"].casefold() or s["name"].casefold() in h:
                    return s
        return min(enemies, key=lambda s: int(s["distance"]))

    async def _hunt_fire_cannon(self, hunt, own, enemies, parsed):
        target = await self._resolve_target(enemies, parsed.get("target_ship"))
        if not target:
            return {"success": False, "reason": "no_target"}

        weapons = await self.db.hunt_get_weapons(own["id"])
        weapon = None
        hint = (parsed.get("weapon_hint") or "").casefold()
        for w in weapons:
            if hint and (hint in w["name"].casefold() or hint in w["side"].casefold()):
                weapon = w
                break
        if weapon is None:
            now = self._hunt_now()
            ready_loaded = []
            for w in weapons:
                if int(w.get("ammo") or 0) <= 0:
                    continue
                ready_at = w.get("ready_at")
                if ready_at:
                    try:
                        if now < self._hunt_dt(ready_at):
                            continue
                    except (TypeError, ValueError):
                        pass
                ready_loaded.append(w)
            # Ближайшее к цели пригодное орудие предпочтительнее первого в БД.
            weapon = max(ready_loaded, key=lambda w: int(w.get("range") or 0), default=None)
        if not weapon:
            return {"success": False, "reason": "no_loaded_gun"}

        now = self._hunt_now()
        if weapon.get("ready_at"):
            try:
                ready = self._hunt_dt(weapon["ready_at"])
                if now < ready:
                    return {"success": False, "reason": "reloading", "ready_at": weapon["ready_at"]}
            except ValueError:
                pass

        if int(weapon["ammo"]) <= 0:
            return {"success": False, "reason": "empty"}

        distance = int(target["distance"])
        ammo_type = str(weapon.get("ammo_type") or "cannonball")
        ammo_range_bonus = -12 if ammo_type == "grapeshot" else 0
        damage_bonus = 18 if ammo_type == "grapeshot" and distance <= 25 else 0
        rng = max(10, int(weapon["range"]) + ammo_range_bonus)
        if distance > rng:
            return {"success": False, "reason": "out_of_range", "distance": distance, "range": rng}

        # Физика попадания — только здесь.
        aim_bonus = 0.10 if str(weapon.get("status") or "") == "aimed" else 0.0
        crew_condition = max(0.35, min(1.0, int(own.get("crew_hp") or 1) / max(1, int(own.get("crew_max_hp") or 1))))
        chance = max(0.18, min(0.90,
            0.72 - distance / 220
            + (int(own["crew_morale"]) - int(target["crew_morale"])) / 500
            + (crew_condition - 0.7) * 0.20
            + aim_bonus
        ))
        hit = random.random() < chance
        damage = random.randint(max(10, int(weapon["damage"]) - 25), int(weapon["damage"]) + 15) + damage_bonus if hit else 0
        new_hp = max(0, int(target["hp"]) - damage)
        sails_damage = random.randint(4, 16) if hit else 0
        fire_gain = random.randint(8, 22) if hit and random.random() < 0.18 else 0
        new_sails = max(0, int(target["sails"]) - sails_damage)
        new_status = "sunk" if new_hp <= 0 else ("damaged" if new_hp < int(target["max_hp"]) * .55 else "afloat")

        claimed = await self.db.hunt_claim_weapon_shot(
            weapon["id"], now.isoformat(timespec="seconds"), int(weapon["reload"])
        )
        if not claimed:
            return {"success": False, "reason": "weapon_raced_or_reloading"}

        # SQL сам вычитает HP/паруса, поэтому параллельные выстрелы не теряют урон.
        fresh_target = await self.db.hunt_apply_ship_delta(
            target["id"],
            hp_delta=-damage,
            sails_delta=-sails_damage,
            morale_delta=-(random.randint(3, 12) if hit else 0),
            fire_delta=fire_gain,
        )
        if not fresh_target:
            return {"success": False, "reason": "target_gone"}
        new_hp = int(fresh_target["hp"])
        new_sails = int(fresh_target["sails"])
        new_fire = int(fresh_target.get("fire_level") or 0)
        new_status = "sunk" if new_hp <= 0 else ("damaged" if new_hp < int(fresh_target["max_hp"]) * .55 else "afloat")
        await self.db.hunt_update_ship(target["id"], status=new_status)
        if hit and damage > 0:
            await self.db.hunt_apply_crew_damage(hunt["id"], target["id"], max(1, damage // 5))

        if hit and new_hp <= 0:
            await self.db.hunt_log_action(hunt["id"], "player", "ship", "SINK", {
                "ship_id": target["id"], "weapon_id": weapon["id"], "damage": damage,
            }, {"hit": True, "destroyed": True})
        await self.db.hunt_log_action(
            hunt["id"], "player", "ship", "FIRE_CANNON",
            {"target_ship_id": target["id"], "weapon_id": weapon["id"], "distance": distance},
            {"hit": hit, "damage": damage, "target_hp": new_hp, "target_status": new_status},
        )
        return {
            "success": True, "action": "FIRE_CANNON", "hit": hit, "damage": damage,
            "target_ship": target["name"], "target_hp": new_hp, "target_max_hp": target["max_hp"],
            "target_status": new_status, "fire_level": new_fire, "weapon": weapon["name"], "distance": distance,
        }

    async def _hunt_board(self, hunt, own, enemies, parsed):
        target = await self._resolve_target(enemies, parsed.get("target_ship"))
        if not target:
            return {"success": False, "reason": "no_target"}
        distance = int(target["distance"])
        if distance > 20:
            return {"success": False, "reason": "too_far", "distance": distance}

        claimed, boarding_id, claim_reason = await self.db.hunt_try_claim_boarding(
            hunt["id"], own["id"], target["id"],
        )
        if not claimed:
            return {"success": False, "reason": claim_reason or "boarding_in_progress"}

        player_summary = await self.db.hunt_get_ship_crew_summary(hunt["id"], own["id"])
        enemy_summary = await self.db.hunt_get_ship_crew_summary(hunt["id"], target["id"])
        player_crew = int(player_summary["crew_count"] if player_summary else 0)
        enemy_crew = int(enemy_summary["crew_count"] if enemy_summary else 0)
        player_condition = max(0.25, min(1.0, (player_summary["crew_hp"] if player_summary else 0) / max(1, int(own.get("crew_max_hp") or 1))))
        enemy_condition = max(0.25, min(1.0, (enemy_summary["crew_hp"] if enemy_summary else 0) / max(1, int(target.get("crew_max_hp") or 1))))
        player_power = player_crew * player_condition * (0.65 + int(own["crew_morale"]) / 200)
        enemy_power = enemy_crew * enemy_condition * (0.65 + int(target["crew_morale"]) / 200)

        success = random.random() < max(0.15, min(0.9, player_power / max(1, player_power + enemy_power)))
        progress = 100 if success else random.randint(20, 65)
        status = "won" if success else "repelled"

        await self.db.hunt_resolve_boarding(
            boarding_id, status, progress, player_crew, enemy_crew,
        )

        if success:
            await self.db.hunt_apply_ship_delta(target["id"], morale_delta=-35)
            await self.db.hunt_update_ship(target["id"], status="captured")
            await self.db.hunt_log_action(
                hunt["id"], "player", "ship", "BOARDING_WON",
                {"target_ship_id": target["id"]}, {"captured": True},
            )
        else:
            await self.db.hunt_apply_ship_delta(
                own["id"], hp_delta=-random.randint(20, 90), morale_delta=-15
            )
            await self.db.hunt_apply_crew_damage(hunt["id"], own["id"], 8)
            await self.db.hunt_log_action(
                hunt["id"], "player", "ship", "BOARDING_REPELLED",
                {"target_ship_id": target["id"]}, {"captured": False},
            )

        return {
            "success": True, "action": "BOARD", "boarding_status": status,
            "target_ship": target["name"], "captured": success,
            "player_crew": player_crew, "enemy_crew": enemy_crew,
        }

    async def _hunt_maneuver(self, hunt, own, enemies, parsed):
        target = await self._resolve_target(enemies, parsed.get("target_ship"))
        direction = (parsed.get("direction") or "none").casefold()
        approach = (parsed.get("approach") or "NEUTRAL").upper()

        if direction == "astern":
            delta = random.randint(8, 20)
            distance_change = delta
        elif direction == "ahead" or approach in ("FORCE", "TACTICAL"):
            delta = random.randint(8, 22)
            distance_change = -delta
        elif direction in ("left", "right"):
            delta = random.randint(3, 10)
            distance_change = -delta if approach == "FORCE" else 0
        else:
            delta = random.randint(3, 10)
            distance_change = -delta if approach == "FORCE" else 0

        new_distance = None
        if target:
            fresh_target = await self.db.hunt_apply_ship_delta(target["id"], distance_delta=distance_change)
            new_distance = int(fresh_target["distance"]) if fresh_target else None

        heading_delta = random.randint(10, 35)
        if direction == "left":
            heading_delta = -heading_delta
        elif direction == "right":
            heading_delta = heading_delta
        else:
            heading_delta *= random.choice([-1, 1])

        sail_gain = random.randint(0, 4) if int(own["sails"]) < 100 else 0
        fresh_own = await self.db.hunt_apply_ship_delta(
            own["id"], heading_delta=heading_delta, sails_delta=sail_gain
        )
        await self.db.hunt_log_action(
            hunt["id"], "player", "ship", "MANEUVER",
            {"direction": direction, "approach": approach, "target_ship_id": target["id"] if target else None},
            {"distance": new_distance, "distance_delta": distance_change},
        )
        return {"success": True, "action": "MANEUVER", "distance": new_distance, "distance_delta": distance_change, "direction": direction}

    async def _hunt_repair(self, hunt, own, parsed):
        damage = int(own["max_hp"]) - int(own["hp"])
        sail_damage = 100 - int(own["sails"])
        if damage <= 0 and sail_damage <= 0:
            return {"success": False, "reason": "already_repaired"}

        # Реальный ремонт требует корабельные ресурсы. Минимальный комплект:
        # инструмент + материал корпуса/парусов.
        if damage > 0:
            material = "wood" if damage >= sail_damage else "sailcloth"
        else:
            material = "sailcloth"
        if not await self.db.hunt_ship_inventory_consume_many(hunt["id"], [("tools", 1), (material, 1)]):
            return {"success": False, "reason": "no_repair_resources"}

        # Эффективность ремонта зависит от живого экипажа.
        crew_factor = max(0.35, min(1.0, int(own.get("crew_hp") or 0) / max(1, int(own.get("crew_max_hp") or 1))))
        repair = max(10, int(random.randint(35, 85) * crew_factor))
        sail_gain = max(4, int(random.randint(8, 22) * crew_factor))
        fresh = await self.db.hunt_apply_ship_delta(own["id"], hp_delta=repair, sails_delta=sail_gain)
        await self.db.hunt_log_action(hunt["id"], "player", "ship", "REPAIR", {}, {"hp": fresh["hp"], "sails": fresh["sails"], "crew_factor": crew_factor})
        return {"success": True, "action": "REPAIR", "hp": fresh["hp"], "sails": fresh["sails"], "crew_factor": crew_factor}

    async def _hunt_feature_action(self, hunt, state, own, parsed, role):
        intent = parsed["intent"]
        participants = await self.db.get_hunt_participants(hunt["id"], confirmed_only=True)

        def match_member(hint):
            h = str(hint or "").casefold()
            for p in participants:
                if h and (h in str(p.get("nickname") or "").casefold() or str(p.get("user_id") or "").casefold() == h):
                    return p
            return None

        if intent == "INSPECT_CREW":
            rows = []
            for p in participants:
                ps = await self.db.get_hunt_player_state(hunt["id"], p["user_id"])
                rows.append({"user_id":p["user_id"], "nickname":p.get("nickname"), "hp":int(ps.get("hp") or 0), "status":ps.get("status"), "role":await self.db.hunt_get_crew_role(hunt["id"],p["user_id"])})
            return {"success":True,"action":intent,"crew":rows}

        if intent == "ASSIGN_CREW":
            target = match_member(parsed.get("command_target"))
            role_hint = str(parsed.get("command") or "").casefold()
            role_map = {
                "капитан":"captain","старпом":"first_officer","штурман":"navigator","боцман":"boatswain",
                "канонир":"gunner","врач":"doctor","доктор":"doctor","кок":"cook","морпех":"marine","матрос":"sailor"}
            new_role = next((v for k,v in role_map.items() if k in role_hint), None)
            if not target or not new_role:
                return {"success":False,"reason":"bad_assignment"}
            await self.db.hunt_assign_crew(hunt["id"], target["user_id"], new_role, state["user_id"])
            return {"success":True,"action":intent,"target":target.get("nickname"),"role":new_role}

        target = match_member(parsed.get("command_target")) or next((p for p in participants if p["user_id"] == state["user_id"]), None)
        if not target:
            return {"success":False,"reason":"crew_member_not_found"}
        ps = await self.db.get_hunt_player_state(hunt["id"], target["user_id"])

        if intent in ("TREAT_CREW","STABILIZE","REVIVE","ADMINISTER_STIMULANT"):
            if intent == "REVIVE" and int(ps.get("hp") or 0) <= 0:
                heal = 20
            elif intent == "STABILIZE":
                heal = 10
            elif intent == "TREAT_CREW":
                heal = 30
            else:
                heal = 20
            if intent == "ADMINISTER_STIMULANT":
                if not await self.db.hunt_ship_inventory_consume(hunt["id"], "stimulant", 1):
                    return {"success":False,"reason":"no_stimulant"}
            new_hp = min(int(ps.get("hp") or 0) + heal, HUNT_STARTING_HP)
            new_status = "free" if new_hp > 0 else ps.get("status")
            await self.db.update_hunt_player_state(hunt["id"], target["user_id"], hp=new_hp, status=new_status)
            await self.db.hunt_apply_ship_delta(own["id"], morale_delta=(10 if intent in ("STABILIZE","REVIVE","ADMINISTER_STIMULANT") else 5))
            await self.db.hunt_apply_crew_damage(hunt["id"], own["id"], 0)
            return {"success":True,"action":intent,"target":target.get("nickname"),"hp":new_hp,"heal":heal}

        if intent == "USE_FOOD":
            item_type = "food"
            if not await self.db.hunt_ship_inventory_consume(hunt["id"], item_type, 1):
                if not await self.db.hunt_ship_inventory_consume(hunt["id"], "rations", 1):
                    return {"success":False,"reason":"no_food"}
            fresh = await self.db.hunt_apply_ship_delta(own["id"], morale_delta=8)
            return {"success":True,"action":intent,"morale":fresh["crew_morale"]}

        if intent == "FIGHT_FIRE":
            fire = int(own.get("fire_level") or 0)
            if fire <= 0:
                return {"success":False,"reason":"no_fire"}
            if not await self.db.hunt_ship_inventory_consume(hunt["id"], "firefighting_water", 1):
                return {"success":False,"reason":"no_firefighting_water"}
            fresh = await self.db.hunt_apply_ship_delta(own["id"], fire_delta=-random.randint(20, 40), morale_delta=3)
            return {"success":True,"action":intent,"fire_level":fresh["fire_level"]}

        if intent == "TAKE_FROM_SHIP_INVENTORY":
            hint = (parsed.get("weapon_hint") or parsed.get("command") or "").casefold()
            items = await self.db.hunt_ship_inventory_get(hunt["id"])
            chosen = next((i for i in items if hint and (hint in i["item_name"].casefold() or hint in i["item_type"].casefold()) and int(i["quantity"])>0), None)
            if not chosen or not await self.db.hunt_ship_inventory_consume(hunt["id"], chosen["item_type"], 1):
                return {"success":False,"reason":"ship_item_not_found"}
            inv = world.load_json_list(state.get("inventory_json"))
            inv.append(world.sanitize_item({"name":chosen["item_name"],"item_type":chosen["item_type"],"tags":["utility"],"uses":1}))
            await self.db.update_hunt_player_state(hunt["id"],state["user_id"],inventory_json=json.dumps(inv,ensure_ascii=False))
            return {"success":True,"action":intent,"item":chosen["item_name"]}

        if intent in ("LOAD_CANNON","CHANGE_AMMO","AIM_CANNON"):
            weapons = await self.db.hunt_get_weapons(own["id"])
            hint=(parsed.get("weapon_hint") or "").casefold()
            weapon=next((w for w in weapons if not hint or hint in w["name"].casefold() or hint in w["side"].casefold()), None)
            if not weapon: return {"success":False,"reason":"weapon_not_found"}
            if intent == "LOAD_CANNON":
                ammo_type = str(weapon.get("ammo_type") or "cannonball")
                if not await self.db.hunt_ship_inventory_consume(hunt["id"], ammo_type, 1):
                    return {"success":False,"reason":f"no_{ammo_type}"}
                await self.db.hunt_update_weapon(weapon["id"],ammo=min(99,int(weapon["ammo"])+1),status="ready")
            elif intent == "CHANGE_AMMO":
                requested = str(parsed.get("command") or parsed.get("weapon_hint") or "").casefold()
                new_type = "grapeshot" if "картеч" in requested or "grape" in requested else "cannonball"
                if int(weapon.get("ammo") or 0) > 0:
                    await self.db.hunt_update_weapon(weapon["id"], ammo=0, ammo_type=new_type, status="ready")
                else:
                    await self.db.hunt_update_weapon(weapon["id"], ammo_type=new_type, status="ready")
                return {"success":True,"action":intent,"weapon":weapon["name"],"ammo_type":new_type}
            else:
                await self.db.hunt_update_weapon(weapon["id"],status="aimed")
            return {"success":True,"action":intent,"weapon":weapon["name"]}

        return {"success":False,"reason":"not_implemented","action":intent}

    async def _hunt_command_crew(self, hunt, own, parsed):
        crew_context = await self._hunt_crew_context()
        target = parsed.get("command_target")
        command = parsed.get("command") or "выполнить приказ капитана"
        # Командование не создаёт сущности и не подменяет roster.
        participants = await self.db.get_hunt_participants(hunt["id"], confirmed_only=True)
        matched = None
        if target:
            t = target.casefold()
            for p in participants:
                if t in str(p.get("nickname") or "").casefold():
                    matched = p
                    break
            if not matched:
                for p in participants:
                    pos = await self.db.get_ship_position(p["user_id"])
                    if pos and t in pos.casefold():
                        matched = p
                        break
        if not matched and target:
            return {"success": False, "reason": "crew_member_not_found", "target": target}

        await self.db.hunt_log_action(
            hunt["id"], "player", "crew", "COMMAND",
            {"target_user_id": matched["user_id"] if matched else None, "command": command},
            {"accepted": True},
        )
        return {
            "success": True, "action": "COMMAND",
            "target": matched.get("nickname") if matched else "экипаж",
            "command": command, "crew_context": crew_context,
        }

    async def _hunt_crew_action(self, hunt, own, parsed):
        # Привязываем результат к реальным должностям, а не к выдуманным NPC.
        command = parsed.get("command") or ""
        role = (parsed.get("command_target") or "").casefold()
        if any(x in role for x in ("канонир", "gunner")):
            return await self._hunt_fire_cannon(hunt, own, await self.db.hunt_get_enemy_ships(hunt["id"]), parsed)
        if any(x in role for x in ("штурман", "рул", "navigator")):
            return await self._hunt_maneuver(hunt, own, await self.db.hunt_get_enemy_ships(hunt["id"]), parsed)
        if any(x in role for x in ("боцман", "repair", "ремонт")):
            return await self._hunt_repair(hunt, own, parsed)
        await self.db.hunt_log_action(
            hunt["id"], "player", "crew", "CREW_ACTION",
            {"role": role, "command": command}, {"accepted": True},
        )
        return {"success": True, "action": "CREW_ACTION", "role": role, "command": command}

    # ---------------------------------------------------------
    # ENEMY AI
    # ---------------------------------------------------------

    async def _hunt_autonomous_event(self, hunt, own, enemies):
        async with self._hunt_lock(hunt["id"]):
            return await self._hunt_autonomous_event_locked(hunt, own, enemies)

    async def _hunt_autonomous_event_locked(self, hunt, own, enemies):
        target = min(enemies, key=lambda s: int(s["distance"]))
        events = []
        # Несколько врагов могут отстреляться в один и тот же тик.
        # own — снимок из БД на момент начала тика, он не обновляется
        # автоматически после каждого hunt_update_ship. Если считать
        # урон каждого следующего врага от одного и того же исходного
        # own["hp"], попадания не накапливаются: очередная запись в БД
        # просто переписывает предыдущую, и часть урона бесследно
        # исчезает (проверено тестом ниже). Поэтому HP/паруса своего
        # корабля ведём здесь как бегущий локальный итог и пишем в БД
        # уже накопленное значение.
        own_hp = int(own["hp"])
        own_sails = int(own["sails"])
        fire_level = int(own.get("fire_level") or 0)
        if fire_level > 0:
            fire_damage = max(2, fire_level // 12)
            own_hp = max(0, own_hp - fire_damage)
            own_sails = max(0, own_sails - 2)
            await self.db.hunt_apply_ship_delta(own["id"], hp_delta=-fire_damage, sails_delta=-2, fire_delta=-min(12, fire_level))
            await self.db.hunt_apply_crew_damage(hunt["id"], own["id"], max(1, fire_damage // 2))
            events.append("🔥 На палубе бушует пожар — экипаж теряет людей и снасти.")
        for enemy in enemies:
            tactics = str(enemy.get("tactics") or "aggressive")
            distance = int(enemy["distance"])
            if tactics == "escape":
                new_distance = min(100, distance + random.randint(6, 15))
                await self.db.hunt_apply_ship_delta(enemy["id"], distance_delta=(new_distance - distance))
                events.append(f"{enemy['name']} пытается уйти.")
                continue

            if tactics == "boarding" and distance > 12:
                new_distance = max(8, distance - random.randint(7, 15))
                await self.db.hunt_apply_ship_delta(enemy["id"], distance_delta=(new_distance - distance))
                events.append(f"{enemy['name']} идёт на сближение.")
                continue

            if tactics == "cautious" and distance < 35:
                new_distance = min(100, distance + random.randint(5, 12))
                await self.db.hunt_apply_ship_delta(enemy["id"], distance_delta=(new_distance - distance))
                events.append(f"{enemy['name']} держит дистанцию.")
                continue

            weapons = await self.db.hunt_get_weapons(enemy["id"])
            now_ai = self._hunt_now()
            weapon = None
            for candidate in weapons:
                if int(candidate["ammo"]) <= 0:
                    continue
                ready_raw = candidate.get("ready_at")
                if ready_raw:
                    try:
                        if now_ai < self._hunt_dt(ready_raw):
                            continue
                    except ValueError:
                        pass
                weapon = candidate
                break
            if weapon and distance <= int(weapon["range"]):
                chance = max(.12, min(.78, .58 - distance / 240))
                hit = random.random() < chance
                damage = random.randint(max(8, int(weapon["damage"]) - 20), int(weapon["damage"]) + 10) if hit else 0
                sails_damage_ai = random.randint(3, 14) if hit else 0
                fire_gain_ai = random.randint(6, 16) if hit and random.random() < 0.12 else 0
                own_hp = max(0, own_hp - damage)
                own_sails = max(0, own_sails - sails_damage_ai)
                await self.db.hunt_update_weapon(
                    weapon["id"], ammo=int(weapon["ammo"]) - 1,
                    ready_at=(self._hunt_now() + timedelta(seconds=int(weapon["reload"]))).isoformat(timespec="seconds"),
                )
                await self.db.hunt_apply_ship_delta(
                    own["id"], hp_delta=-damage, sails_delta=-sails_damage_ai, fire_delta=fire_gain_ai,
                )
                if hit and damage > 0:
                    await self.db.hunt_apply_crew_damage(hunt["id"], own["id"], max(1, damage // 5))
                fresh_damage_ship = await self.db.hunt_get_ship(own["id"])
                own_hp = int(fresh_damage_ship["hp"]) if fresh_damage_ship else 0
                own_sails = int(fresh_damage_ship["sails"]) if fresh_damage_ship else 0
                await self.db.hunt_log_action(
                    hunt["id"], "npc", "ship", "NPC_FIRE",
                    {"npc_ship_id": enemy["id"], "target_ship_id": own["id"]},
                    {"hit": hit, "damage": damage, "target_hp": own_hp},
                )
                events.append(
                    f"{enemy['name']} стреляет {'— попадание' if hit else '— перелёт'}."
                )
            else:
                new_distance = max(8, distance - random.randint(3, 10))
                await self.db.hunt_apply_ship_delta(enemy["id"], distance_delta=(new_distance - distance))
                events.append(f"{enemy['name']} маневрирует и сокращает дистанцию.")

        await self.db.update_hunt_pack_state(
            hunt["id"],
            next_event_at=(
                self._hunt_now() + timedelta(
                    seconds=random.randint(HUNT_EVENT_MIN_SECONDS, HUNT_EVENT_MAX_SECONDS)
                )
            ).isoformat(timespec="seconds"),
        )

        fresh_own = await self.db.hunt_get_ship(own["id"])
        if not fresh_own or int(fresh_own["hp"]) <= 0:
            await self.db.hunt_log_action(hunt["id"], "npc", "ship", "PLAYER_SHIP_LOST", {}, {})
            await self._hunt_finish(hunt)
            return

        world_flavor = await self._hunt_world_flavor(hunt["id"])
        prompt = f"""
Ты — Пёс на корабле-призраке. Технически произошло:
Наш корабль: корпус {fresh_own['hp']}/{fresh_own['max_hp']}, паруса {fresh_own['sails']}/100.
События противника:
{chr(10).join('- ' + x for x in events)}

{world_flavor}

Не добавляй новых кораблей или фактов, не меняй погоду/время. Отыграй коротко,
как боевой рапорт вперемешку с живой сценой.
1-4 предложения. Учитывай, что настоящий экипаж общается и координируется по должностям.
"""
        raw = await self.call_openrouter(
            [{"role": "system", "content": prompt}],
            json_mode=False, timeout=25, rate_limit=False, tag="hunt_narrative",
        )
        self.send_message(
            mto=self.room, mbody=(raw or "").strip() or "⚓ Враг сделал ход. Следим за дистанцией и заряжаем орудия.",
            mtype="groupchat",
        )

    # ---------------------------------------------------------
    # NARRATION / STATUS
    # ---------------------------------------------------------

    async def _hunt_narrate_action(self, state, result, room_jid):
        hunt = await self.db.get_current_hunt(self.room)
        if not hunt:
            return
        ships = await self.db.hunt_get_ships(hunt["id"])
        own = next((s for s in ships if s["side"] == "player"), None)
        enemies = [s for s in ships if s["side"] == "enemy" and s["status"] not in ("sunk", "captured")]
        crew_context = await self._hunt_crew_context()
        world_flavor = await self._hunt_world_flavor(hunt["id"])

        technical = json.dumps({
            "result": result,
            "our_ship": {
                k: own.get(k) for k in ("name", "hp", "max_hp", "sails", "crew_morale", "heading")
            } if own else None,
            "enemies": [{
                k: s.get(k) for k in ("name", "class_name", "hp", "max_hp", "sails", "distance", "status")
            } for s in enemies],
        }, ensure_ascii=False)

        prompt = f"""
Ты — Пёс, боевой голос экипажа корабля-призрака.
Технический движок уже решил исход. НЕЛЬЗЯ менять цифры или придумывать сущности.

{world_flavor}

Результат действия:
{technical}

Реальный состав экипажа:
{crew_context}

Отыграй действие игрока в описанной выше обстановке (не меняй погоду/время):
коротко, ярко, по-морскому.
Если это приказ — покажи координацию должностей.
Если выстрел — пушка, отдача, дым, попадание/промах.
Если абордаж — только тот результат, который указан движком.
2-5 предложений.
"""
        raw = await self.call_openrouter(
            [{"role": "system", "content": prompt}],
            json_mode=False, timeout=25, rate_limit=False, tag="hunt_narrative",
        )
        self.send_message(
            mto=room_jid,
            mbody=(raw or "").strip() or self._hunt_result_fallback(result),
            mtype="chat",
        )

    @staticmethod
    def _hunt_result_fallback(result):
        if result.get("action") == "FIRE_CANNON":
            if result.get("hit"):
                return f"💥 Попадание по {result.get('target_ship', 'цели')}! Урон: {result.get('damage', 0)}."
            return "💨 Залп ушёл в воду. Заряжаем следующее орудие."
        if result.get("action") == "BOARD":
            return "⚔️ Абордаж завершён: " + ("судно захвачено." if result.get("captured") else "нас отбросили.")
        if result.get("action") == "REPAIR":
            return "🔧 Экипаж работает. Корпус и снасти приводятся в порядок."
        if result.get("action") == "MANEUVER":
            return "⚓ Курс изменён. Держим противника в пределах удобной дистанции."
        if result.get("action") == "USE_ITEM":
            if result.get("success"):
                return f"🎒 В ход пошёл(а) {result.get('item', 'предмет')}."
            return "🎒 Такого предмета в снаряжении не нашлось."
        if result.get("action") == "TALK":
            return "🗣️ Слова разошлись по палубе."
        return "⚓ Приказ принят экипажем."

    async def _hunt_finish(self, hunt):
        hunt_id = hunt["id"]
        ships = await self.db.hunt_get_ships(hunt_id)

        own = next((s for s in ships if s["side"] == "player"), None)
        enemies = [s for s in ships if s["side"] == "enemy"]
        captured = [s for s in enemies if s["status"] == "captured"]
        sunk = [s for s in enemies if s["status"] == "sunk"]

        states = await self.db.get_all_hunt_player_states(hunt_id)

        # Экипаж делит судьбу собственного корабля: PvP и личная
        # "элиминация" в этой версии не является частью боевого
        # движка (см. README), поэтому "выжил" считается по тому,
        # цел ли наш корабль на момент завершения Охоты, а не по
        # неизменяемому полю status в hunt_player_state.
        own_alive = bool(own) and int(own.get("hp") or 0) > 0

        # Сначала атомарно фиксируем итог события. Если другой поток уже
        # завершил эту Охоту, повторный вызов ничего не начисляет.
        finished = await self.db.finish_hunt(hunt_id)
        if not finished:
            return

        reward_lines = []
        for state in states:
            nickname = state["nickname"] or state["user_id"]
            alive = own_alive
            coins = 0.0
            rep = 0

            if alive:
                coins += HUNT_REWARD_SURVIVED
                rep += HUNT_REP_SURVIVED

            # Бонус за боевой результат относится ко всему экипажу,
            # но только участники, пережившие бой, получают его.
            if alive:
                coins += HUNT_REWARD_HOUND_KILL * len(captured)
                coins += HUNT_REWARD_HOUND_KILL * len(sunk)
                if captured:
                    rep += HUNT_REP_SAVE

            actual, awarded = await self.db.award_hunt_reward_once(
                hunt_id, state["user_id"], coins, rep,
                description="Награда за морскую «Дикую Охоту»",
            )
            if not awarded:
                actual = 0.0

            reward_lines.append(
                f"{nickname}: "
                f"{('выжил' if alive else 'выбыл')}"
                + (f", +{actual:.2f} коинов" if actual > 0 else "")
            )

        status = (
            f"Наш корабль: {own['hp']}/{own['max_hp']} корпуса, "
            f"{own['sails']}/100 парусов."
            if own else "Наш корабль потерян."
        )
        world_flavor = await self._hunt_world_flavor(hunt_id)

        prompt = f"""
Ты — Пёс, Предводитель морской «Дикой Охоты».
Охота завершена.

{world_flavor}

{status}
Захвачено судов: {', '.join(s['name'] for s in captured) or 'ничего'}.
Потоплено судов: {', '.join(s['name'] for s in sunk) or 'ничего'}.

Не выдумывай новых событий, кораблей, потерь или подвигов. Погоду/место не меняй.
Дай короткий финал на 2-4 предложения в стиле корабля-призрака.
""".strip()

        try:
            raw = await self.call_openrouter(
                [{"role": "system", "content": prompt}],
                json_mode=False, timeout=25, rate_limit=False, tag="hunt_narrative",
            )
            finish_text = (raw or "").strip() or self._HUNT_FINISH_FALLBACK
        except Exception:
            logging.exception("[ОХОТА] Ошибка финальной narration hunt_id=%s", hunt_id)
            finish_text = self._HUNT_FINISH_FALLBACK
        self._hunt_safe_send(self.room, finish_text, "groupchat")

        if reward_lines:
            self._hunt_safe_send(
                self.room,
                "🏆 ИТОГИ МОРСКОЙ ОХОТЫ:\n" + "\n".join(reward_lines),
                "groupchat",
            )

        await self.db.hunt_log_action(
            hunt_id, "system", "hunt", "FINISH",
            {
                "player_ship_id": own["id"] if own else None,
                "captured_ship_ids": [s["id"] for s in captured],
                "sunk_ship_ids": [s["id"] for s in sunk],
            },
            {"participants": len(states)},
        )

    # ---------------------------------------------------------
    # COMMANDS
    # ---------------------------------------------------------

    def _user_has_active_session(self, user_id):
        sessions = getattr(self, "authenticated_sessions", None)
        if not isinstance(sessions, dict):
            return False
        now_mono = time.monotonic()
        for nick, session in list(sessions.items()):
            if not isinstance(session, dict):
                sessions.pop(nick, None)
                continue
            if now_mono - float(session.get("since", 0)) > SESSION_MAX_AGE_SECONDS:
                sessions.pop(nick, None)
                continue
            if str(session.get("user_id")) == str(user_id):
                return True
        return False

    def _parse_hunt_time(self, text):
        """
        Человек пишет время в своём часовом поясе (DOG_TIMEZONE) —
        "!охота начать 20:00" значит 20:00 по местному, а не по UTC.
        Разбираем именно в этом поясе, а на выходе всегда отдаём UTC,
        потому что всё дальнейшее планирование/сравнение/хранение
        идёт в UTC (см. hunt_now() в config.py).
        """
        text = text.strip()
        base = datetime.now(DOG_TIMEZONE)
        for fmt in ("%H:%M", "%Y-%m-%d %H:%M"):
            try:
                value = datetime.strptime(text, fmt)
                if fmt == "%H:%M":
                    value = base.replace(hour=value.hour, minute=value.minute, second=0, microsecond=0)
                    if value <= base:
                        value += timedelta(days=1)
                else:
                    value = value.replace(tzinfo=DOG_TIMEZONE)
                return value.astimezone(timezone.utc)
            except ValueError:
                continue
        return None

    async def handle_hunt_command(self, user_id, text, room_jid):
        rest = text.strip()[len("!охота"):].strip()
        rest_lower = rest.casefold()
        nickname, _ = await self.db.get_user_info(user_id)
        is_admin = bool(nickname) and nickname.casefold() in HUNT_ADMIN_NICKS

        if rest_lower == "начать" or rest_lower.startswith("начать "):
            await self._hunt_cmd_start(user_id, is_admin, rest[len("начать"):].strip(), room_jid)
            return
        if rest_lower == "отмена":
            await self._hunt_cmd_cancel(is_admin, room_jid)
            return
        if rest_lower in ("статус", "состояние", "status"):
            await self._hunt_cmd_status(room_jid, is_admin)
            return
        if not rest:
            await self._hunt_cmd_join_or_status(user_id, nickname, room_jid)
            return
        await self._hunt_cmd_action(user_id, nickname, rest, room_jid)

    async def _hunt_cmd_start(self, user_id, is_admin, time_str, room_jid):
        if not is_admin:
            self.send_message(mto=room_jid, mbody="🔒 Назначать Охоту может только админ.", mtype="chat")
            return
        nickname, _ = await self.db.get_user_info(user_id)
        start_at = self._parse_hunt_time(time_str)
        if not start_at:
            self.send_message(mto=room_jid, mbody="Формат: !охота начать ЧЧ:ММ или ГГГГ-ММ-ДД ЧЧ:ММ.", mtype="chat")
            return
        if start_at <= self._hunt_now():
            self.send_message(mto=room_jid, mbody="Это время уже в прошлом.", mtype="chat")
            return
        try:
            hunt = await self.db.create_hunt(
                self.room, start_at, user_id, created_by_nickname=nickname
            )
        except Exception as exc:
            logging.exception("[ОХОТА] Не удалось запланировать hunt")
            self._hunt_safe_send(
                room_jid,
                f"⚠️ Не удалось запланировать Охоту: {type(exc).__name__}. Ошибка записана в лог.",
                "chat",
            )
            return
        if not hunt:
            self._hunt_safe_send(room_jid, "Охота уже запланирована или идёт.", "chat")
            return
        try:
            await self.db.hunt_log_action(
                hunt["id"], "admin", user_id, "HUNT_SCHEDULED",
                {"start_at": start_at.isoformat(timespec="seconds")}, {"ok": True},
            )
        except Exception:
            logging.exception("[ОХОТА] Не удалось записать HUNT_SCHEDULED hunt_id=%s", hunt["id"])
        self._hunt_safe_send(
            self._hunt_public_room(room_jid),
            f"☠️ Морская «Дикая Охота» #{hunt['id']} назначена на "
            f"{start_at.astimezone(DOG_TIMEZONE).strftime('%H:%M %d.%m')}.\n"
            "Регистрация экипажа открыта. В назначенный момент начинаем поиск вражеских судов.",
            "groupchat",
        )

    async def _hunt_cmd_status(self, room_jid, is_admin=False):
        hunt = await self.db.get_current_hunt(self.room)
        if not hunt:
            last = await self.db.get_last_hunt(self.room)
            if last and last.get("status") == "finished":
                self._hunt_safe_send(room_jid, f"⚓ Дикая Охота не идёт. Последняя Охота #{last['id']} завершена.", "chat")
            elif last and last.get("status") == "cancelled":
                self._hunt_safe_send(room_jid, f"⚓ Дикая Охота не идёт. Последняя Охота #{last['id']} отменена.", "chat")
            else:
                self._hunt_safe_send(room_jid, "⚓ Дикая Охота не запланирована.", "chat")
            return

        status = hunt["status"]
        start_at = self._hunt_dt(hunt["start_at"])
        participants = await self.db.get_hunt_participants(hunt["id"], confirmed_only=(status in ("preparing", "active")))
        registered = await self.db.get_hunt_participants(hunt["id"])
        lines = [f"☠️ ДИКАЯ ОХОТА #{hunt['id']}"]
        if status == "scheduled":
            now = self._hunt_now()
            mins = max(0, int((start_at-now).total_seconds()//60))
            lines += [f"Статус: запланирована", f"Старт: {start_at.astimezone(DOG_TIMEZONE).strftime('%H:%M %d.%m')}", f"До старта: ~{mins} мин.", f"Экипаж в списке: {len(registered)}"]
        elif status == "preparing":
            lines += ["Статус: подготовка", f"Старт: {start_at.astimezone(DOG_TIMEZONE).strftime('%H:%M %d.%m')}", f"Подтверждённый экипаж: {len(participants)}"]
        else:
            ships = await self.db.hunt_get_ships(hunt["id"])
            own = next((s for s in ships if s["side"] == "player"), None)
            enemies = [s for s in ships if s["side"] == "enemy" and s["status"] not in ("sunk", "captured")]
            started_at = self._hunt_dt(hunt.get("started_at") or hunt["start_at"])
            lines += ["Статус: идёт", f"Начало: {started_at.astimezone(DOG_TIMEZONE).strftime('%H:%M %d.%m')}", f"Экипаж: {len(participants)}", f"Вражеских судов: {len(enemies)}"]
            if own:
                lines.append(f"Наш корабль: {own['name']} — {own['hp']}/{own['max_hp']} HP, паруса {own['sails']}/100")
        # Внутренние DB-поля не показываем в общем чате. Диагностика
        # должна жить в логах, а не в пользовательском выводе.
        self._hunt_safe_send(room_jid, "\n".join(lines), "chat")

    async def _hunt_cmd_cancel(self, is_admin, room_jid):
        if not is_admin:
            self.send_message(mto=room_jid, mbody="🔒 Отменить Охоту может только админ.", mtype="chat")
            return
        hunt = await self.db.get_current_hunt(self.room)
        if not hunt:
            self.send_message(mto=room_jid, mbody="Отменять нечего.", mtype="chat")
            return
        await self.db.cancel_hunt(hunt["id"])
        self.send_message(mto=self.room, mbody="⚓ Морская Охота отменена. Экипаж возвращается к обычной службе.", mtype="groupchat")

    async def _hunt_cmd_join_or_status(self, user_id, nickname, room_jid):
        hunt = await self.db.get_current_hunt(self.room)
        if not hunt:
            self.send_message(mto=room_jid, mbody="⚓ Морская «Дикая Охота» сейчас не назначена.", mtype="chat")
            return

        if hunt["status"] == "active":
            participants = await self.db.get_hunt_participants(hunt["id"], confirmed_only=True)
            ships = await self.db.hunt_get_ships(hunt["id"])
            own = next((s for s in ships if s["side"] == "player"), None)
            enemies = [s for s in ships if s["side"] == "enemy" and s["status"] not in ("sunk", "captured")]
            state = await self.db.get_hunt_player_state(hunt["id"], user_id)
            if not state:
                self.send_message(mto=room_jid, mbody="Ты не вошёл в состав экипажа этой Охоты до старта.", mtype="chat")
                return
            enemy_lines = "\n".join(
                f"• {s['name']} — {s['class_name']}, {s['distance']}м, корпус {s['hp']}/{s['max_hp']}, паруса {s['sails']}/100"
                for s in enemies
            ) or "• Целей на горизонте больше нет."
            weapons = await self.db.hunt_get_weapons(own["id"]) if own else []
            guns = ", ".join(
                f"{w['name']}: {w['ammo']} шт." + (" (перезарядка)" if w.get("ready_at") else "")
                for w in weapons
            ) or "орудия недоступны"
            self.send_message(
                mto=room_jid,
                mbody=(
                    f"☠️ БОЙ ИДЁТ. Экипаж: {len(participants)} чел.\n"
                    f"Наш корабль: {own['name'] if own else '?'} — "
                    f"{own['hp']}/{own['max_hp']} корпус, {own['sails']}/100 парусов.\n"
                    f"Цели:\n{enemy_lines}\n"
                    f"Орудия: {guns}\n"
                    "Действие: !охота <что делаешь>"
                ),
                mtype="chat",
            )
            return

        now = self._hunt_now()
        start_at = self._hunt_dt(hunt["start_at"])
        reg_at = self._hunt_dt(hunt["registration_opens_at"])
        if now < reg_at:
            self.send_message(mto=room_jid, mbody=f"Охота назначена на {start_at.astimezone(DOG_TIMEZONE).strftime('%H:%M %d.%m')}, запись ещё не открыта.", mtype="chat")
            return
        if await self.db.is_hunt_participant(hunt["id"], user_id):
            self.send_message(mto=room_jid, mbody="🐾 Ты уже в списке экипажа.", mtype="chat")
            return
        await self.db.register_hunt_participant(hunt["id"], user_id, nickname)
        mins = max(0, int((start_at - now).total_seconds() // 60))
        self.send_message(mto=room_jid, mbody=f"⚓ Ты записан в экипаж. До выхода в море ~{mins} мин.", mtype="chat")

    async def _hunt_cmd_action(self, user_id, nickname, action_text, room_jid):
        hunt = await self.db.get_current_hunt(self.room)
        if not hunt or hunt["status"] != "active":
            self.send_message(mto=room_jid, mbody="Сейчас морская Охота не идёт.", mtype="chat")
            return
        state = await self.db.get_hunt_player_state(hunt["id"], user_id)
        if not state:
            self.send_message(mto=room_jid, mbody="Ты не в составе экипажа этой Охоты.", mtype="chat")
            return

        role = await self.db.hunt_get_crew_role(hunt["id"], user_id)
        if not role:
            role = world.get_role_for_ship_position(
                await self.db.get_ship_position(user_id)
            ) or world.ROLE_CREW

        parsed = await self._hunt_parse_action(action_text, hunt["id"])
        claimed, wait = await self.db.hunt_try_claim_global_action(
            hunt["id"], user_id, HUNT_ACTION_COOLDOWN_SECONDS
        )
        if not claimed:
            self._hunt_safe_send(room_jid, f"⏳ Экипаж ещё выполняет предыдущий манёвр. ~{max(1, wait)} сек.", "chat")
            return
        await self.db.hunt_log_action(
            hunt["id"], "player", user_id, "INPUT",
            {
                "text": action_text,
                "parsed": parsed,
                "role": role,
            },
            {"accepted": True},
            entity_type="crew_action",
        )
        async with self._hunt_lock(hunt["id"]):
            # Повторно читаем состояние после постановки в очередь: никакого stale snapshot.
            state = await self.db.get_hunt_player_state(hunt["id"], user_id) or state
            result = await self._hunt_resolve_action(hunt, state, parsed, role=role)
        await self._hunt_narrate_action(state, result, room_jid)
