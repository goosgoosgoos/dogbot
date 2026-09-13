from .config import *
from .database import UserFactsDB
from .rate_limiters import (
    SlidingWindowRateLimiter,
    QuizRateLimiter,
    LoadDegradationTracker,
)

class DogCoreMixin:
    def __init__(
        self,
        jid,
        password,
        room,
        nick,
        openrouter_key,
        model,
    ):
        super().__init__(
            jid,
            password,
        )

        self.room = room
        self.nick = nick
        self.api_key = openrouter_key
        self.model = model

        self.engaged = False

        self.history = deque(
            maxlen=HISTORY_SIZE
        )

        # Отдельно от self.history: сюда пишется КАЖДОЕ обычное
        # сообщение комнаты, включая сообщения незарегистрированных
        # и несогласившихся ников (и ответы самого Пса) — только
        # затем, чтобы уметь определить автора цитаты. В LLM как
        # контекст диалога не подмешивается. См. resolve_quote_author.
        self.quote_lookback = deque(
            maxlen=QUOTE_LOOKBACK_SIZE
        )

        self.db = UserFactsDB(
            DB_PATH
        )

        # ----------------------------------------------------
        # QUIZ
        # ----------------------------------------------------

        self.active_quiz = None

        self.quiz_lock = asyncio.Lock()

        self.quiz_rate_limiter = (
            QuizRateLimiter(
                QUIZ_USER_LIMIT,
                QUIZ_USER_WINDOW,
            )
        )

        # Отдельный лимит именно на LLM judge (второй уровень
        # проверки ответа) — не путать с quiz_rate_limiter выше,
        # который ограничивает, как часто вообще ЗАПУСКАЮТСЯ новые
        # викторины. Этот же ограничивает, как часто один участник
        # (и чат в целом) может дёргать judge НА ответы внутри уже
        # идущей викторины — см. resolve_quiz_answer.
        self.quiz_judge_rate_limiter = (
            SlidingWindowRateLimiter(
                QUIZ_JUDGE_RATE_USER_LIMIT,
                QUIZ_JUDGE_RATE_USER_WINDOW,
                QUIZ_JUDGE_RATE_GLOBAL_LIMIT,
                QUIZ_JUDGE_RATE_GLOBAL_WINDOW,
            )
        )

        # ----------------------------------------------------
        # LLM RATE LIMIT
        # ----------------------------------------------------

        self.llm_rate_limiter = (
            SlidingWindowRateLimiter(
                LLM_USER_LIMIT,
                LLM_USER_WINDOW,
                LLM_GLOBAL_LIMIT,
                LLM_GLOBAL_WINDOW,
            )
        )

        self.llm_semaphore = asyncio.Semaphore(
            LLM_CONCURRENCY
        )

        # Если провайдер прислал явный дневной лимит (см.
        # LLM_HOLD_UNTIL_RESET_ENABLED в config.py) — держим запросы на
        # холде до этого unix-времени, чтобы не жечь ретраи впустую.
        self.llm_cooldown_until_epoch = 0.0

        # ----------------------------------------------------
        # LOAD DEGRADATION (защита от "бесплатной техподдержки")
        # ----------------------------------------------------
        # Плавный откат по времени (и вверх, и вниз) — см.
        # LoadDegradationTracker и блок LOAD DEGRADATION в
        # config.py.
        self.load_tracker = (
            LoadDegradationTracker(
                LOAD_HALF_LIFE_SECONDS,
                LOAD_INCREMENT_STEP,
            )
        )

        # ----------------------------------------------------
        # BACKGROUND
        # ----------------------------------------------------

        self.inflation_task = None
        self.context_task = None
        self.hunt_task = None
        self.nickname_task = None
        self.presence_snapshot_task = None
        self.drunk_decay_task = None
        self.privacy_ready = False

        # ----------------------------------------------------
        # MUC ANTI-RAID / ANTI-BOT MODERATION
        # ----------------------------------------------------
        self.moderation_init()

        # ----------------------------------------------------
        # QUIZ QUESTIONS
        # ----------------------------------------------------

        self.quiz_questions = [
            {
                "q": (
                    "Что больше: масса электрона "
                    "или позитрона?"
                ),
                "a": [
                    "одинакова",
                    "равна",
                    "одинаковая",
                ],
            },
            {
                "q": (
                    "Как называется переход из твёрдого "
                    "состояния сразу в газ, "
                    "минуя жидкость?"
                ),
                "a": [
                    "сублимация",
                    "возгонка",
                ],
            },
            {
                "q": (
                    "Какой элемент обозначается буквой W?"
                ),
                "a": [
                    "вольфрам",
                    "tungsten",
                ],
            },
            {
                "q": (
                    "Какая планета Солнечной системы "
                    "вращается почти лёжа на боку?"
                ),
                "a": [
                    "уран",
                    "uranus",
                ],
            },
            {
                "q": (
                    "Что означает приставка «нано» в СИ?"
                ),
                "a": [
                    "-9",
                    "минус девять",
                    "минус девятая",
                ],
            },
            {
                "q": (
                    "Сколько кварков в протоне?"
                ),
                "a": [
                    "3",
                    "три",
                ],
            },
        ]

        # ----------------------------------------------------
        # PLUGINS
        # ----------------------------------------------------

        self.register_plugin(
            "xep_0030"
        )

        self.register_plugin(
            "xep_0045"
        )

        # XEP-0313: MAM — резервный источник восстановления 14-дневного
        # chat_archive после рестарта. Если плагин/сервер недоступен,
        # существующая MUC history и локальная БД продолжают работать.
        if CHAT_ARCHIVE_MAM_ENABLED:
            try:
                self.register_plugin("xep_0313")
            except Exception:
                logging.exception("[АРХИВ/MAM] Не удалось зарегистрировать XEP-0313")

        # XEP-0425: модерируемое удаление чужих сообщений в MUC.
        # Плагин сам подтянет зависимости XEP-0421/XEP-0424.
        self.register_plugin(
            "xep_0425"
        )

        self.register_plugin(
            "xep_0199",
            {
                "keepalive": True,
                "frequency": 60,
            },
        )

        # ----------------------------------------------------
        # EVENTS
        # ----------------------------------------------------

        self.add_event_handler(
            "session_start",
            self.start,
        )

        self.add_event_handler(
            "groupchat_message",
            self.muc_message,
        )

        self.add_event_handler(
            "groupchat_presence",
            self.on_muc_presence,
        )

        self.add_event_handler(
            "message",
            self.on_private_message,
        )

        self.add_event_handler(
            "disconnected",
            self.on_disconnect,
        )

    # ========================================================
    # XMPP LIFECYCLE
    # ========================================================

    async def start(self, event):
        try:
            await self.db.setup()

            # Вайтлист модерации (!вайтлист) переживает перезапуск —
            # подтягиваем сохранённые bare JID из БД в память, чтобы
            # moderate_incoming_muc сразу их учитывал (см.
            # moderation.py::moderation_init/moderate_incoming_muc).
            self.mod_whitelist_jids = {
                row["jid"]
                for row in await self.db.moderation_whitelist_list()
            }

            if not self.privacy_ready:
                await self.privacy_init()
                self.privacy_ready = True

            self.send_presence()

            await self.get_roster()

            muc = self.plugin["xep_0045"]
            # При каждом старте НЕ начинаем архив с нуля: запрашиваем у MUC
            # серверную историю за весь retention-период. Современный
            # Slixmpp возвращает историю прямо из join_muc_wait(), поэтому
            # её можно сохранить в БД до обработки новых сообщений.
            # Старые версии/серверы могут не вернуть историю — тогда
            # продолжаем работу без падения, но логируем проблему.
            join_wait = getattr(muc, "join_muc_wait", None)
            join_result = None
            if callable(join_wait):
                try:
                    join_result = await join_wait(
                        self.room,
                        self.nick,
                        seconds=max(int(CHAT_ARCHIVE_RETENTION_DAYS), 1) * 86400,
                    )
                except TypeError:
                    # Совместимость со старыми реализациями API.
                    join_result = await join_wait(self.room, self.nick)
            else:
                muc.join_muc(
                    self.room,
                    self.nick,
                )

            if join_result and isinstance(join_result, tuple) and len(join_result) >= 4:
                history_messages = join_result[3] or []
                if (
                    CHAT_ARCHIVE_IMPORT_ON_START
                    and CHAT_ARCHIVE_ENABLED
                    and history_messages
                    and hasattr(self, "import_muc_history")
                ):
                    imported = await self.import_muc_history(history_messages)
                    logging.info(
                        "[АРХИВ] При старте загружено из MUC: %s сообщений",
                        imported,
                    )
                else:
                    logging.info("[АРХИВ] Сервер MUC не вернул историю при входе")

            # MUC history может быть ограничен сервером. Добираем пропуски
            # тем же retention-периодом через XEP-0313 MAM.
            if (
                CHAT_ARCHIVE_IMPORT_ON_START
                and CHAT_ARCHIVE_ENABLED
                and CHAT_ARCHIVE_MAM_ENABLED
                and hasattr(self, "import_mam_history")
            ):
                try:
                    imported_mam = await self.import_mam_history()
                    logging.info(
                        "[АРХИВ/MAM] При старте загружено из MAM: %s сообщений",
                        imported_mam,
                    )
                except Exception:
                    logging.exception("[АРХИВ/MAM] Ошибка восстановления через MAM")

            if (
                self.presence_snapshot_task is None
                or self.presence_snapshot_task.done()
            ):
                self.presence_snapshot_task = asyncio.create_task(
                    self.presence_snapshot_loop()
                )

            logging.info(
                "[XMPP] Бот вошёл в %s как %s",
                self.room,
                self.nick,
            )

            await self.db.apply_missed_inflation()

            await self.db.apply_missed_drunk_decay()

            await self.db.cleanup_old_facts(
                days=90
            )

            if (
                self.inflation_task is None
                or self.inflation_task.done()
            ):
                self.inflation_task = (
                    asyncio.create_task(
                        self.inflation_loop()
                    )
                )

            if (
                self.drunk_decay_task is None
                or self.drunk_decay_task.done()
            ):
                self.drunk_decay_task = (
                    asyncio.create_task(
                        self.drunk_decay_loop()
                    )
                )

            if (
                self.hunt_task is None
                or self.hunt_task.done()
            ):
                self.hunt_task = (
                    asyncio.create_task(
                        self.hunt_loop()
                    )
                )

            if (
                self.nickname_task is None
                or self.nickname_task.done()
            ):
                self.nickname_task = (
                    asyncio.create_task(
                        self.nickname_assigner_loop()
                    )
                )

        except Exception:
            logging.exception(
                "[XMPP] Ошибка инициализации"
            )

    async def capture_room_presence_snapshot(self):
        """Снимает текущий roster MUC и фиксирует его в SQLite."""
        try:
            nicks = [
                str(nick)
                for nick in self.get_room_nicks()
                if str(nick).strip() and str(nick).casefold() != self.nick.casefold()
            ]
            accounts = await self.db.get_crew_by_nicks(nicks)
            users = []
            for nick in nicks:
                occupant_jid = f"{self.room}/{nick}"
                account = accounts.get(nick.casefold())
                user_id = account.get("user_id") if account else None
                position = account.get("ship_position") if account else None
                users.append({
                    "nick": nick,
                    "occupant_jid": occupant_jid,
                    "user_id": user_id,
                    "registered": bool(user_id),
                    "ship_position": position,
                })
            await self.db.save_presence_snapshot(self.room, users)
        except Exception:
            logging.exception("[PRESENCE] Ошибка снимка участников")

    async def presence_snapshot_loop(self):
        """Периодически сохраняет состояние комнаты; интервал — 5 минут."""
        # После join_muc roster появляется не мгновенно.
        await asyncio.sleep(PRESENCE_SNAPSHOT_INITIAL_DELAY_SECONDS)
        while True:
            try:
                await self.capture_room_presence_snapshot()
                await asyncio.sleep(PRESENCE_SNAPSHOT_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("[PRESENCE] Ошибка фонового цикла")
                await asyncio.sleep(PRESENCE_SNAPSHOT_RETRY_SECONDS)

    def _resolve_temporal_query_time(self, text):
        """Разбирает только однозначные временные указатели для исторического поиска."""
        now = get_current_time()
        base = datetime.fromisoformat(now["datetime"])
        lower = (text or "").casefold()
        if "позавчера" in lower:
            return base - timedelta(days=2)
        if "вчера" in lower:
            return base - timedelta(days=1)
        if "сегодня" in lower:
            return base
        m = re.search(r"(\d+)\s*(минут|минуты|минуту)\s+назад", lower)
        if m:
            return base - timedelta(minutes=int(m.group(1)))
        m = re.search(r"(\d+)\s*(час|часа|часов)\s+назад", lower)
        if m:
            return base - timedelta(hours=int(m.group(1)))
        m = re.search(r"(\d+)\s*(день|дня|дней)\s+назад", lower)
        if m:
            return base - timedelta(days=int(m.group(1)))
        m = re.search(r"(20\d{2})[-./](\d{1,2})[-./](\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?", lower)
        if not m:
            months = {
                "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
                "мая": 5, "июня": 6, "июля": 7, "августа": 8,
                "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
            }
            rm = re.search(r"(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)(?:\s+(20\d{2}))?", lower)
            if rm:
                year = int(rm.group(3) or base.year)
                try:
                    return base.replace(year=year, month=months[rm.group(2)], day=int(rm.group(1)), hour=0, minute=0, second=0, microsecond=0)
                except ValueError:
                    return None
        if m:
            try:
                return base.replace(year=int(m.group(1)), month=int(m.group(2)), day=int(m.group(3)),
                                    hour=int(m.group(4) or 0), minute=int(m.group(5) or 0), second=0, microsecond=0)
            except ValueError:
                return None
        return None

    def _archive_query_terms(self, text):
        """Дешёвый программный набор слов для точечного поиска в архиве."""
        words = re.findall(r"[^\W\d_]{3,}", str(text or "").casefold(), re.UNICODE)
        stop = set(MEMORY_STOPWORDS or ())
        return [
            word for word in dict.fromkeys(words)
            if word not in stop
        ][:8]

    async def get_chat_archive_context(self, text, context_needs, nicks=None):
        """
        Собирает короткий исторический фрагмент из 14-дневного архива.

        Архитектура: роутер определяет необходимость и дату; Python
        валидирует/расширяет дату на ближайшие дни и ищет упоминания
        по индексам БД. LLM получает уже готовый фрагмент, а не сам
        "ищет" сообщения и не может расширить окно за retention.
        """
        if not CHAT_ARCHIVE_ENABLED or not hasattr(self.db, "search_chat_archive"):
            return ""

        target_date = context_needs.get("archive_target_date")
        target_end_date = context_needs.get("archive_target_end_date")

        # Если роутер недоступен, но запрос явно исторический/recall,
        # используем уже существующий детерминированный разбор даты.
        if not target_date:
            fallback = self._resolve_temporal_query_time(text)
            if fallback:
                target_date = fallback.strftime("%Y-%m-%d")
                target_end_date = target_date

        wanted_nicks = list(dict.fromkeys(
            str(n).strip()
            for n in (nicks or [])
            if str(n).strip()
        ))

        rows = await self.db.search_chat_archive(
            self.room,
            target_date=target_date,
            target_end_date=target_end_date,
            nicks=wanted_nicks,
            # При известной дате сама дата — основной фильтр; не
            # сужаем окно случайными словами вроде "было/вчера".
            # При неизвестной дате термы помогают найти эпизод в 14 днях.
            terms=(
                [] if target_date
                else self._archive_query_terms(text)
            ),
            limit=CHAT_ARCHIVE_MAX_MESSAGES,
            neighbor_days=CHAT_ARCHIVE_NEIGHBOR_DAYS,
        )

        logging.info(
            "[АРХИВ] query=%r target=%s..%s nicks=%s rows=%d",
            text,
            target_date,
            target_end_date,
            wanted_nicks or None,
            len(rows),
        )
        if rows:
            logging.info(
                "[АРХИВ] rows=%s",
                json.dumps(
                    [
                        {
                            "id": r.get("id"),
                            "date": r.get("created_at"),
                            "sender": r.get("sender_nick"),
                            "body": r.get("body"),
                        }
                        for r in rows
                    ],
                    ensure_ascii=False,
                )[:6000],
            )

        if not rows:
            return (
                "\nАРХИВ ЧАТА: по заданному историческому окну "
                "релевантных сообщений не найдено. Не выдумывай "
                "содержание старого разговора.\n"
            )

        lines = [
            "\nИСТОРИЧЕСКИЙ АРХИВ ЧАТА "
            "(выбран программно, retention ограничен "
            f"{CHAT_ARCHIVE_RETENTION_DAYS} днями):"
        ]
        if target_date:
            if target_end_date and target_end_date != target_date:
                lines.append(
                    f"Временной якорь роутера: {target_date} — {target_end_date}; "
                    f"взяты ближайшие ±{CHAT_ARCHIVE_NEIGHBOR_DAYS} дн."
                )
            else:
                lines.append(
                    f"Временной якорь роутера: {target_date}; "
                    f"взяты ближайшие ±{CHAT_ARCHIVE_NEIGHBOR_DAYS} дн."
                )
        else:
            lines.append("Точная дата не определена; поиск ограничен последними 14 днями.")

        for row in rows:
            prefix = f"{row['created_at']} | {row['sender_nick']}"
            if row.get("user_id") is None:
                prefix += " [без зарегистрированного аккаунта]"
            if row.get("is_command"):
                prefix += " [команда]"
            body = str(row.get("body") or "").replace("\r", "").strip()
            line = f"- {prefix}: {body}"

            quote_author = row.get("quote_author")
            quoted = row.get("quoted_text")
            if quoted:
                line += (
                    f" | цитата"
                    f"{' от ' + str(quote_author) if quote_author else ''}: "
                    f"{str(quoted).replace(chr(10), ' ')[:500]}"
                )

            lines.append(line[:1400])

        return "\n".join(lines) + "\n"

    async def get_recent_history_context(
        self,
        context_needs=None,
        target_nick=None,
        mentioned_nicks=None,
        quote_author=None,
    ):
        """
        Отдаёт "последний разговор" для needs_history.

        По умолчанию (и при любом сбое/выключенном флаге) — тот же
        self.history, что и до этого рефакторинга: дешёвый, в
        памяти, без SQL. Под HISTORY_ARCHIVE_ENABLED и только для
        доли трафика (HISTORY_ARCHIVE_ROLLOUT_PERCENT) — прицельный
        поиск по chat_archive за последние
        HISTORY_ARCHIVE_RECENCY_MINUTES, опционально сфокусированный
        на конкретных участниках разговора.

        needs_history — самый частый routing-флаг (почти каждое
        сообщение-продолжение разговора), поэтому это КРИТИЧНЫЙ путь:
        см. комментарий в config.py про HISTORY_ARCHIVE_*. Здесь
        сознательно НЕТ отдельного router-поля "кого искать" (в
        отличие от mention_fact_category для needs_mention_facts) —
        участники берутся из уже посчитанных для этого же сообщения
        target_nick/mentioned_nicks/quote_author (см. вызов в
        llm.py, тот же список, что уходит в needs_chat_archive), а
        не из нового LLM-поля. На самом частом пути это осознанный
        компромисс: не расширять промпт роутера ради каждого
        сообщения и не полагаться на ники, которые LLM могла бы
        придумать/перепутать в SQL-фильтре.
        """
        # self.history уже содержит текущее входящее сообщение (см.
        # muc.py: self.history.append(...) выполняется ДО вызова
        # ask_llm/get_recent_history_context для этого же сообщения).
        # Без исключения последнего элемента текущая реплика попадала
        # бы в "КОНТЕКСТ ПОСЛЕДНИХ СООБЩЕНИЙ" ВТОРОЙ раз — она и так
        # отдельно уходит в промпт как "СЕЙЧАС ПИШЕТ" (см. llm.py).
        fallback = "\n".join(list(self.history)[:-1])

        if not HISTORY_ARCHIVE_ENABLED:
            # Не логируем этот путь — при выключенном флаге (дефолт)
            # это происходит на КАЖДОМ сообщении с needs_history=True,
            # это раздует лог без пользы. Наблюдаемость нужна именно
            # для случаев, когда флаг включён (см. остальные ветки).
            return fallback

        if not CHAT_ARCHIVE_ENABLED or not hasattr(
            self.db, "search_recent_chat_archive"
        ):
            logging.info(
                "[ИСТОРИЯ] history_source=memory reason=archive_unavailable"
            )
            return fallback

        # Постепенный rollout: НЕ replace-in-place. Только доля
        # сообщений реально идёт через archive-поиск, остальные —
        # штатный self.history (см. HISTORY_ARCHIVE_ROLLOUT_PERCENT).
        if random.random() * 100 >= HISTORY_ARCHIVE_ROLLOUT_PERCENT:
            logging.info(
                "[ИСТОРИЯ] history_source=memory reason=rollout_skip "
                "rollout_percent=%s",
                HISTORY_ARCHIVE_ROLLOUT_PERCENT,
            )
            return fallback

        participants = []
        if target_nick:
            participants.append(target_nick)
        participants.extend(mentioned_nicks or [])
        if quote_author:
            participants.append(quote_author)
        participants = list(dict.fromkeys(
            str(n).strip() for n in participants if str(n).strip()
        ))

        since = datetime.now() - timedelta(
            minutes=max(int(HISTORY_ARCHIVE_RECENCY_MINUTES), 1)
        )

        try:
            rows = await self.db.search_recent_chat_archive(
                self.room,
                since=since,
                nicks=participants,
                limit=HISTORY_ARCHIVE_MAX_MESSAGES,
            )

            # ВАЖНО: раньше здесь при 0 результатах по конкретным
            # участникам запрос молча "расширялся" на весь чат комнаты
            # (nicks=None) — если Вася ничего не говорил, LLM вместо
            # этого получала реплики Пети/Кати/Саши и могла
            # реконструировать ответ из чужого, не относящегося к
            # вопросу разговора. Это утечка контекста, а не полезный
            # фолбэк. Если цель была конкретный участник и по нему
            # ничего не нашлось — это NO_RESULT, а не "покажем что
            # есть"; вызывающая сторона в этом случае уходит на
            # fallback (self.history) несколькими строками ниже,
            # который тоже не относится конкретно к participants, но
            # это тот же контекст, что видел бы пользователь и раньше.
            widened = False
        except Exception:
            logging.exception(
                "[ИСТОРИЯ] history_source=memory reason=archive_error — "
                "деградирую на self.history."
            )
            return fallback

        if not rows:
            logging.info(
                "[ИСТОРИЯ] history_source=memory reason=archive_empty "
                "participants=%s widened=%s",
                participants or None,
                widened,
            )
            return fallback

        logging.info(
            "[ИСТОРИЯ] history_source=archive rows=%d participants=%s "
            "widened=%s recency_minutes=%s",
            len(rows),
            participants or None,
            widened,
            HISTORY_ARCHIVE_RECENCY_MINUTES,
        )

        lines = [
            f"{row.get('sender_nick')}: {str(row.get('body') or '').strip()}"
            for row in rows
        ]
        return "\n".join(lines)

    async def get_temporal_context(self, text, nicks=None):
        """Тянет только записи, относящиеся к указанному историческому моменту."""
        target = self._resolve_temporal_query_time(text)
        if not target or not hasattr(self.db, "get_nearest_presence_snapshots"):
            return ""
        snaps = await self.db.get_nearest_presence_snapshots(self.room, target.isoformat(), limit=6)
        wanted = {str(n).casefold() for n in (nicks or []) if str(n).strip()}
        lines = [
            "ИСТОРИЧЕСКИЙ КОНТЕКСТ — ВЫБРАН ПРОГРАММНО ПО ДАТЕ/ВРЕМЕНИ:",
            f"Запрошенный момент: {target.isoformat(timespec='seconds')}.",
        ]
        added = 0
        for snap in snaps:
            matched = []
            for item in snap.get("users", []):
                if not isinstance(item, dict) or not item.get("nick"):
                    continue
                if not wanted or str(item["nick"]).casefold() in wanted:
                    matched.append(item)
            if matched:
                lines.append(f"Снимок {snap['captured_at']}:")
                for item in matched:
                    pos = item.get("ship_position") or SHIP_AUXILIARY_POSITION
                    reg = "зарегистрирован" if item.get("registered") else "не зарегистрирован"
                    lines.append(f"- {item['nick']}: был на борту; {pos}; {reg}.")
                added += len(matched)
        if not added:
            lines.append("В ближайших сохранённых снимках точной информации по указанным участникам нет; не выдумывай её.")
        return "\n".join(lines)

    async def get_current_crew_context(self):
        """Возвращает полный программный состав корабля для каждого LLM-запроса."""
        nicks = [
            str(nick)
            for nick in self.get_room_nicks()
            if str(nick).strip() and str(nick).casefold() != self.nick.casefold()
        ]
        accounts = await self.db.get_crew_by_nicks(nicks)
        live = []
        for nick in nicks:
            account = accounts.get(nick.casefold())
            user_id = account.get("user_id") if account else None
            position = account.get("ship_position") if account else None
            live.append({
                "nick": nick,
                "user_id": user_id,
                "registered": bool(user_id),
                "ship_position": position,
            })

        live.sort(key=lambda x: x["nick"].casefold())
        lines = [
            "ТЕКУЩИЙ СОСТАВ КОРАБЛЯ — ПРОГРАММНО ПРОВЕРЕНО ПО MUC:",
            "Список ниже является источником истины о том, кто сейчас находится на корабле.",
            "Не придумывай отсутствующих членов экипажа и не считай историю сообщений доказательством присутствия.",
            f"Всего на борту: {len(live)}.",
        ]
        for item in live:
            if item["registered"]:
                pos = item["ship_position"] or "должность не назначена"
                lines.append(f"- {item['nick']} — {pos}; зарегистрирован в боте")
            else:
                lines.append(f"- {item['nick']} — {SHIP_AUXILIARY_POSITION}; не зарегистрирован в боте")
        return "\n".join(lines)


    async def get_presence_context(self, nicks=None):
        """Возвращает для LLM проверяемое состояние текущего/исторического присутствия."""
        wanted = [str(n) for n in (nicks or []) if str(n).strip()]
        if not wanted:
            return ""

        # Живой roster — источник истины для «сейчас».
        live = {str(n).casefold(): str(n) for n in self.get_room_nicks()}
        latest = await self.db.get_latest_presence_snapshot(self.room)
        historical = {}
        if latest:
            for item in latest.get("users", []):
                if isinstance(item, dict) and item.get("nick"):
                    historical[str(item["nick"]).casefold()] = item

        lines = [
            "ПРИСУТСТВИЕ В КОМНАТЕ — ПРОГРАММНО ПРОВЕРЕНО:",
            "Не угадывай наличие пользователя по истории сообщений. "
            "Для статуса «сейчас» доверяй текущему MUC roster; "
            "последний сохранённый снимок нужен только как историческое доказательство.",
        ]
        if latest:
            lines.append(f"Последний снимок: {latest['captured_at']}.")
        else:
            lines.append("Снимков ещё нет.")

        seen = set()
        for nick in wanted:
            key = nick.casefold()
            if key in seen:
                continue
            seen.add(key)
            if key == self.nick.casefold():
                lines.append(f"- {nick}: это сам Пёс; не считать обычным участником.")
            elif key in live:
                hist = historical.get(key)
                reg = "зарегистрирован в боте" if hist and hist.get("registered") else "статус регистрации по последнему снимку не подтверждён"
                lines.append(f"- {live[key]}: СЕЙЧАС В КОМНАТЕ; {reg}.")
            elif key in historical:
                item = historical[key]
                reg = "зарегистрирован в боте" if item.get("registered") else "не зарегистрирован в боте"
                lines.append(
                    f"- {nick}: СЕЙЧАС НЕ ВИДЕН в комнате; последний снимок "
                    f"{latest['captured_at']} — присутствовал; {reg}."
                )
            else:
                lines.append(
                    f"- {nick}: не найден ни в текущем roster, ни в последнем снимке; "
                    "факт присутствия не подтверждён."
                )
        return "\n".join(lines) + "\n"

    async def inflation_loop(self):
        while True:
            try:
                await asyncio.sleep(60)

                await (
                    self.db
                    .check_and_apply_inflation_hourly()
                )

            except asyncio.CancelledError:
                raise

            except Exception:
                logging.exception(
                    "[ЭКОНОМИКА] Ошибка фонового цикла"
                )

    async def drunk_decay_loop(self):
        while True:
            try:
                await asyncio.sleep(60)

                await (
                    self.db
                    .check_and_apply_drunk_decay()
                )

            except asyncio.CancelledError:
                raise

            except Exception:
                logging.exception(
                    "[ОПЬЯНЕНИЕ] Ошибка фонового "
                    "цикла отрезвления"
                )

    def on_disconnect(self, event):
        logging.warning(
            "[XMPP] Разрыв соединения. "
            "Slixmpp ожидает переподключение."
        )

        self.engaged = False

    # ========================================================
    # MENTION / PARSING
    # ========================================================

    def get_room_nicks(self):
        """Вернуть текущий roster MUC через официальный API XEP-0045.

        В новых версиях Slixmpp ``xep_0045.rooms`` может быть вложен по
        ``pfrom`` (multi_from), поэтому прямой ``rooms.get(self.room)``
        иногда возвращает пустоту, хотя бот реально находится в комнате.
        Официальный ``get_roster(room)`` учитывает эту структуру.

        Оставляем fallback на старую структуру ``rooms[room]`` для
        совместимости с прежними версиями/тестовыми заглушками.
        """
        try:
            muc = self.plugin["xep_0045"]

            # Основной путь: публичный API XEP-0045.
            get_roster = getattr(muc, "get_roster", None)
            if callable(get_roster):
                try:
                    roster = get_roster(self.room)
                    if roster is not None:
                        return [str(n) for n in roster if str(n).strip()]
                except (KeyError, ValueError, TypeError):
                    # Комната ещё не появилась в локальном roster либо
                    # установлен старый вариант API — пробуем fallback.
                    pass

            rooms = getattr(muc, "rooms", {}) or {}

            # Старый Slixmpp: rooms[room] == {nick: properties}.
            direct = rooms.get(self.room) if hasattr(rooms, "get") else None
            if isinstance(direct, dict):
                return [str(n) for n in direct.keys() if str(n).strip()]

            # Новый multi_from: rooms[pfrom][room] == {nick: properties}.
            if hasattr(rooms, "values"):
                for container in rooms.values():
                    if not isinstance(container, dict):
                        continue
                    room_data = container.get(self.room)
                    if isinstance(room_data, dict):
                        return [
                            str(n) for n in room_data.keys()
                            if str(n).strip()
                        ]

        except Exception as exc:
            logging.error(
                "[XMPP] Ошибка списка участников: %s",
                exc,
            )

        return []

    BOT_MENTION_RE = re.compile(r"\bп[ёе]с\b")

    def is_addressed_to_me(self, text, target_nick):
        """
        Обращаются ли в этом сообщении напрямую к Псу.

        Если есть явный адресат (см. parse_mention — "Ник: ..." /
        "Ник, ...") — решает ТОЛЬКО он: явное обращение к
        конкретному нику важнее случайного упоминания слова
        "пёс"/"пес" где-то в тексте (иначе "Вася, спроси у пса
        совета" засчиталось бы как обращение к боту). Если явного
        адресата нет — ищем слово "пёс"/"пес" по всему тексту.

        Используется и до регистрации/согласия (чтобы решить, надо
        ли предложить !согласие/!регистрация), и после — при
        решении, включаться ли в разговор (см. muc_message).
        """
        if target_nick:
            return (
                str(target_nick).casefold()
                == self.nick.casefold()
            )

        return bool(
            self.BOT_MENTION_RE.search(
                str(text).casefold()
            )
        )

    def parse_mention(self, text):
        text = str(
            text
        ).strip()

        match = re.match(
            r"^([^\s:,]+)[:,\-]\s*(.*)$",
            text,
            re.DOTALL,
        )

        if not match:
            return None, text

        candidate, rest = (
            match.groups()
        )

        if (
            candidate.casefold()
            == self.nick.casefold()
        ):
            return self.nick, rest

        for nick in self.get_room_nicks():

            if (
                candidate.casefold()
                == str(nick).casefold()
            ):
                return nick, rest

        return None, text

    def find_mentioned_nicks(self, text, exclude=()):
        """
        Возвращает ВСЕ известные ники комнаты, упомянутые в
        собственном тексте автора, в порядке их появления.

        Важно: вызывающая сторона должна передавать сюда уже
        отделённый от цитаты текст. Ник внутри quote-block никогда
        не является упоминанием текущего автора.

        Это именно упоминания, а не адресат: ник в середине фразы
        вроде "спроси у russoturisto" не превращает russoturisto
        в автора и не меняет получателя ответа Пса.
        """
        source = str(text or "")
        exclude_cf = {
            str(x).casefold() for x in exclude
        } | {self.nick.casefold()}

        found = []
        seen = set()

        for nick in self.get_room_nicks():
            nick_s = str(nick)
            nick_cf = nick_s.casefold()
            if nick_cf in exclude_cf or nick_cf in seen:
                continue

            pattern = (
                r"(?<!\w)"
                + re.escape(nick_s)
                + r"(?!\w)"
            )
            match = re.search(pattern, source, re.IGNORECASE)
            if match:
                found.append((match.start(), nick))
                seen.add(nick_cf)

        found.sort(key=lambda item: item[0])
        return [nick for _, nick in found]

    def find_mentioned_nick(self, text, exclude=()):
        """Совместимый старый API: вернуть первое упоминание или None."""
        nicks = self.find_mentioned_nicks(text, exclude=exclude)
        return nicks[0] if nicks else None

    def resolve_recall_target_nick(
        self,
        text,
        sender,
        target_nick=None,
    ):
        """
        Определяет, ЧЕЙ ник имеется в виду для целенаправленного
        RECALL (см. llm.py, блок needs_recall) — единая точка
        входа вместо прежней "target_nick or find_mentioned_nick".

        У прежней версии был баг: если автор обращался прямо к
        боту по имени ("Пёс: вспомни, что было с капитаном"),
        parse_mention() отдаёт target_nick = ник самого бота, и
        recall безуспешно пытался искать факты "пользователя
        Пёс" вместо того, чтобы вообще смотреть на текст. Здесь
        target_nick учитывается, только если это не сам бот и не
        автор сообщения.

        Порядок проверки (только детерминированные, без БД и без
        LLM — поэтому синхронно):

        1. Явный адресат сообщения (target_nick), если не бот и
           не сам автор.
        2. Любой известный ник комнаты, встретившийся в тексте
           (find_mentioned_nick).
        3. Ролевой алиас (ROLE_ALIASES из config.py) — "капитана"
           /"кока"/"боцмана" и т.п. вместо ника напрямую.

        Возвращает найденный ник (гарантированно не бот и не сам
        автор) или None — тогда вызывающая сторона решает
        дальнейший фолбэк (нечёткий поиск по БД, затем
        LLM-роутер; см. ask_llm() в llm.py).
        """
        exclude_cf = {
            str(sender).casefold(),
            self.nick.casefold(),
        }

        if (
            target_nick
            and str(target_nick).casefold()
            not in exclude_cf
        ):
            return target_nick

        literal = self.find_mentioned_nick(
            text,
            exclude={sender},
        )

        if literal:
            return literal

        tokens = re.findall(
            r"[^\W\d_]+",
            str(text or "").casefold(),
            re.UNICODE,
        )

        for token in tokens:

            role_nick = ROLE_ALIASES.get(token)

            if (
                role_nick
                and role_nick.casefold()
                not in exclude_cf
            ):
                return role_nick

        return None

    @staticmethod
    def parse_quoted_args(text):
        try:
            return shlex.split(
                text
            )
        except ValueError:
            return text.split()

    # ========================================================
    # ЦИТАТЫ ("> текст")
    # ========================================================

    # Поддерживаем обычные и вложенные цитаты: "> текст", ">> текст",
    # "> > текст" (маркеры через пробел — так тоже бывает, например
    # когда клиент визуально вкладывает цитату в цитату), а также
    # произвольное число уровней. Уровень (число ">") сохраняется в
    # quoted_text как чистый префикс ">>...", чтобы LLM видела
    # структуру цепочки цитирования.
    #
    # ВАЖНО: группа — это ВЕСЬ прогон "> "/">"-токенов подряд
    # (( ?:>\s?)+ ), а не просто consecutive-run символов ">". Старая
    # версия ("^(>+)\s?(.*)\$") корректно считала глубину только для
    # слитных маркеров (">> текст"), а для "> > текст" (маркеры через
    # пробел) вторая ">" не входила в run и оставалась мусором прямо
    # в payload — то есть форматы ">>" и "> >" давали РАЗНЫЙ и местами
    # ломаный результат для одной и той же логической глубины. Теперь
    # оба формата дают идентичный, чистый результат.
    QUOTE_LINE_RE = re.compile(r"^((?:>\s?)+)(.*)$")

    # XEP-0461 (Message Replies) / XEP-0359 (Unique and Stable
    # Stanza IDs) — используются только для программного разбора
    # структурных метаданных ответа, см. extract_reply_target_nick.
    REPLY_NS = "urn:xmpp:reply:0"
    STANZA_ID_NS = "urn:xmpp:sid:0"

    @classmethod
    def parse_quote_block(cls, text):
        """
        Отделяет цитируемую часть сообщения (строки, начинающиеся
        с "> ") от остального текста автора.

        Возвращает (quoted_text | None, remainder_text).
        """
        lines = str(text).split("\n")

        quote_lines = []
        rest_lines = []

        for line in lines:
            stripped = line.strip()
            match = cls.QUOTE_LINE_RE.match(stripped)

            if match:
                # marker — весь блок ">"/"> " подряд (см. коммент к
                # QUOTE_LINE_RE выше). Считаем РЕАЛЬНУЮ глубину по
                # числу ">" внутри него, а не по длине строки — так
                # ">>" и "> >" дают одинаковую глубину=2.
                marker, payload = match.groups()
                depth = marker.count(">")
                payload = payload.strip()
                # Один ">" — обычная цитата (старый формат, без
                # видимого префикса); от двух уровней — показываем
                # чистый ">>"/">>>" без пробелов внутри, независимо
                # от того, как маркеры шли в исходном тексте.
                prefix = ">" * depth if depth > 1 else ""
                if payload:
                    quote_lines.append(f"{prefix} {payload}".strip())
                else:
                    quote_lines.append(prefix)
            else:
                rest_lines.append(line)

        quoted_text = "\n".join(
            line for line in quote_lines if line.strip("> ")
        ).strip()

        remainder = "\n".join(
            rest_lines
        ).strip()

        return (quoted_text or None), remainder

    def extract_stanza_id(self, msg):
        """
        Достаёт "официальный" id сообщения в MUC — XEP-0359
        <stanza-id>, проставляемый сервером комнаты, — а НЕ сырой
        атрибут stanza id="...", который для groupchat-сообщений
        задаётся клиентом-отправителем и не может считаться
        надёжным идентификатором. Цитата из XEP-0461: "For messages
        of type 'groupchat', the stanza's 'id' attribute MUST NOT
        be used for replies. Instead ... the ID assigned to the
        stanza by the group chat itself must be used."

        Возвращает str id или None.
        """
        try:
            el = msg.xml.find(
                f"{{{self.STANZA_ID_NS}}}stanza-id"
            )
        except Exception:
            return None

        if el is None:
            return None

        return el.get("id") or None

    def extract_message_created_at(self, msg):
        """
        Возвращает время сообщения с учётом XEP-0203 delayed delivery.
        Для обычного realtime-сообщения используется текущее время, а для
        истории MUC — реальный timestamp из <delay/>. В БД время хранится
        как naive local datetime, поэтому timezone приводим к DOG_TIMEZONE.
        """
        try:
            delay = msg.get_plugin("delay", check=True)
            stamp = delay.get("stamp") if delay is not None else None
        except Exception:
            stamp = None

        if stamp is None:
            return now_iso()

        try:
            if isinstance(stamp, datetime):
                dt = stamp
            else:
                dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = dt.astimezone(DOG_TIMEZONE).replace(tzinfo=None)
            return dt.isoformat(timespec="seconds")
        except Exception:
            return now_iso()

    def extract_reply_target_nick(self, msg):
        """
        Достаёт ник автора цитируемого сообщения из СТРУКТУРНЫХ
        метаданных ответа (XEP-0461 <reply to=".../Nickname"
        id="..."/>), а не из текста — это и есть надёжный вариант:
        ник берётся из resource-части JID в атрибуте to, который
        клиент, формирующий ответ, копирует из from исходного
        сообщения. В MUC from подделать нельзя — сервер комнаты
        сам подставляет туда реальный ник участника.

        Плагин xep_0461 нарочно НЕ регистрируется (версия slixmpp
        в требованиях не гарантирует его наличие) — вместо этого
        элемент ищется напрямую в сыром XML стенза, что работает
        независимо от того, какие плагины подключены.

        Возвращает (nick | None, reply_id | None). reply_id
        нужен как запасной путь: спека говорит "SHOULD" для to,
        не "MUST" — если to всё-таки нет, но id есть, можно
        попробовать найти сообщение с таким же XEP-0359 id в
        self.quote_lookback.
        """
        try:
            el = msg.xml.find(f"{{{self.REPLY_NS}}}reply")
        except Exception:
            return None, None

        if el is None:
            return None, None

        reply_id = el.get("id") or None
        to_attr = el.get("to") or None

        if not to_attr:
            return None, reply_id

        try:
            resource = slixmpp.JID(to_attr).resource
        except Exception:
            return None, reply_id

        return (resource or None), reply_id

    def resolve_quote_author(self, msg, quoted_text):
        """
        Определяет автора процитированного текста, программно и
        без обращения к LLM, в порядке убывания надёжности:

        1. Структурные метаданные ответа (XEP-0461 <reply to=...
           id=.../>) — если клиент их прислал, ник берётся оттуда
           напрямую и подделать его отправитель не может.
        2. Если to нет, но есть id — ищем в self.quote_lookback
           сообщение с таким же XEP-0359 stanza-id.
        3. Если структурных метаданных нет вообще (клиент их не
           поддерживает и просто прислал текстовый "> "-fallback)
           — откатываемся на find_quote_author: точный или
           нечёткий текстовый поиск по self.quote_lookback.

        self.quote_lookback хранит сообщения ВСЕХ участников
        комнаты, включая незарегистрированных и несогласившихся —
        иначе цитату от такого участника было бы физически
        невозможно атрибутировать (раньше именно так и было: автор
        искался только в self.history, куда неконсентные
        сообщения никогда не попадали).
        """
        nick, reply_id = self.extract_reply_target_nick(msg)

        if nick:
            return nick

        if reply_id:

            for entry in reversed(self.quote_lookback):

                if entry.get("stanza_id") == reply_id:
                    return entry.get("sender")

        return self.find_quote_author(quoted_text)

    async def resolve_quote_author_async(self, room_jid, msg, quoted_text):
        """
        Полный резолвер автора цитаты.

        1) XEP-0461 reply-to -> nick.
        2) XEP-0359 stanza-id -> in-memory lookback.
        3) Текстовый поиск по in-memory lookback.
        4) ТО ЖЕ текстовое сопоставление по 14-дневному DB archive.

        Последний шаг устраняет регрессию после рестарта: реальное сообщение
        и его отправитель теперь находятся в БД, даже если lookback пуст.
        """
        nick, reply_id = self.extract_reply_target_nick(msg)
        if nick:
            return nick

        if reply_id:
            for entry in reversed(self.quote_lookback):
                if entry.get("stanza_id") == reply_id:
                    return entry.get("sender")

            if hasattr(self.db, "find_chat_archive_quote_author"):
                found = await self.db.find_chat_archive_quote_author(
                    room_jid,
                    quoted_text,
                )
                if found and found.get("stanza_id") == reply_id:
                    return found.get("sender")

        author = self.find_quote_author(quoted_text)
        if author:
            return author

        if hasattr(self.db, "find_chat_archive_quote_author"):
            found = await self.db.find_chat_archive_quote_author(
                room_jid,
                quoted_text,
            )
            if found:
                logging.info(
                    "[ЦИТАТА] Автор найден в DB archive: %s score=%.3f date=%s",
                    found.get("sender"),
                    float(found.get("score") or 0),
                    found.get("created_at"),
                )
                return found.get("sender")

        return None

    @staticmethod
    def _quote_match_variants(quoted_text):
        """
        Возвращает варианты текста цитаты для атрибуции.

        Вложенная цитата может содержать префикс ">>" и тогда целиком
        она уже не совпадёт с исходным сообщением в lookback. Проверяем
        одновременно весь блок, каждый уровень вложенности и отдельные
        содержательные строки. Это не меняет текст, который получает LLM.
        """
        text = str(quoted_text or "").replace("\r", "")
        variants = []

        def add(value):
            value = re.sub(r"\s+", " ", value).strip().casefold()
            if len(value) >= 4 and value not in variants:
                variants.append(value)

        add(text)
        for line in text.split("\n"):
            m = re.match(r"^(>+)\s?(.*)$", line.strip())
            if m:
                add(m.group(2))
        for depth in range(1, 8):
            prefix = ">" * depth
            stripped_lines = []
            found = False
            for line in text.split("\n"):
                m = re.match(r"^(>+)\s?(.*)$", line.strip())
                if m and len(m.group(1)) >= depth:
                    stripped_lines.append(m.group(2))
                    found = True
                elif not line.strip().startswith(">"):
                    stripped_lines.append(line)
            if found:
                add("\n".join(stripped_lines))
        return variants

    def find_quote_author(self, quoted_text):
        """
        Резервный, не-структурный способ определить автора цитаты:
        сверяет нормализованный текст цитаты с недавними сырыми
        сообщениями ВСЕХ участников комнаты — self.quote_lookback
        (см. muc_message: туда пишется любое обычное сообщение,
        независимо от регистрации/согласия автора и включая
        реплики самого Пса).

        Возвращает ник автора или None, если совпадение не
        найдено с достаточной уверенностью.
        """
        if not quoted_text or len(quoted_text) < 4:
            return None

        quote_variants = self._quote_match_variants(quoted_text)
        if not quote_variants:
            return None

        best_sender = None
        best_score = 0.0

        for entry in reversed(self.quote_lookback):
            entry_text = re.sub(
                r"\s+", " ", str(entry.get("text", ""))
            ).strip().casefold()
            if not entry_text:
                continue

            entry_best = 0.0
            for norm_quote in quote_variants:
                if norm_quote in entry_text or entry_text in norm_quote:
                    return entry.get("sender")
                score = difflib.SequenceMatcher(
                    None, norm_quote, entry_text
                ).ratio()
                entry_best = max(entry_best, score)

            if entry_best > best_score:
                best_score = entry_best
                best_sender = entry.get("sender")

        return best_sender if best_score >= 0.6 else None

    # ========================================================
    # CAPTCHA
    # ========================================================

    def solve_captcha(self, text):
        lower = text.casefold()

        match = re.search(
            r'напиши:?\s*"([^"]+)"',
            lower,
        )

        if match:
            return match.group(
                1
            ).strip()

        capitals = {
            "итали": "рим",
            "франци": "париж",
            "германи": "берлин",
            "росси": "москва",
            "испан": "мадрид",
            "англи": "лондон",
            "япон": "токио",
            "сша": "вашингтон",
            "украин": "киев",
        }

        for country, capital in capitals.items():

            if country in lower and (
                "столиц" in lower
                or "город" in lower
            ):
                return capital

        alphabet = (
            "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
        )

        ordinals = {
            "первую": 1,
            "вторую": 2,
            "третью": 3,
            "четвертую": 4,
            "пятую": 5,
            "шестую": 6,
        }

        for word, number in ordinals.items():

            if (
                f"{word} букву"
                in lower
            ):
                return alphabet[
                    number - 1
                ]

        num_words = {
            "ноль": "0",
            "один": "1",
            "два": "2",
            "три": "3",
            "четыре": "4",
            "пять": "5",
        }

        math_text = lower

        for word, number in num_words.items():
            math_text = re.sub(
                rf"\b{word}\b",
                number,
                math_text,
            )

        match = re.search(
            r"(\d+)\s*([\+\-\*])\s*(\d+)",
            math_text,
        )

        if match:

            a = int(
                match.group(1)
            )

            op = match.group(2)

            b = int(
                match.group(3)
            )

            if op == "+":
                return str(
                    a + b
                )

            if op == "-":
                return str(
                    a - b
                )

            if op == "*":
                return str(
                    a * b
                )

        return None

    # ========================================================
    # QUIZ (общая логика, используется и в MUC, и в личке)
    # ========================================================

    async def try_win_quiz(self, sender, expected_question_id=None):
        """
        Атомарно пытается закрыть текущую активную викторину на `sender`.

        expected_question_id — если передан, победа засчитывается
        ТОЛЬКО если self.active_quiz всё ещё тот же самый раунд
        (quiz["question_id"] совпадает). Это защищённый critical
        section: он же — точка ПОВТОРНОЙ проверки для ответов,
        подтверждённых LLM judge (см. resolve_quiz_answer ниже) —
        judge мог думать долго, и за это время раунд мог уже
        закрыться/смениться; без этой проверки такой "просроченный"
        judge-ответ мог бы засчитаться за НОВЫЙ вопрос. Для
        обычного синхронного exact-match пути (без ожидания LLM)
        передавать expected_question_id тоже безопасно — раунд
        физически не успевает смениться за это время, проверка
        просто всегда проходит.

        Возвращает:
          None                                — активной викторины уже
                                                 нет, или (при заданном
                                                 expected_question_id)
                                                 это уже ДРУГОЙ раунд —
                                                 гонка с другим ответом
                                                 / просроченный judge.
          {"status": "no_account"}            — ответ верный, но у
                                                 sender нет привязанного
                                                 аккаунта. Викторина
                                                 НЕ закрывается — раунд
                                                 остаётся открытым для
                                                 остальных / для sender
                                                 после регистрации.
          {"status": "won", "reward": float}  — sender выиграл, викторина
                                                 закрыта и награда
                                                 начислена (reward может
                                                 быть 0, если банк пуст).

        Не проверяет сам текст ответа — вызывающий код обязан заранее
        убедиться, что answer_matches(text, ...) вернул True, либо
        что LLM judge вернул 1 (см. resolve_quiz_answer).
        """
        async with self.quiz_lock:
            if not self.active_quiz:
                return None

            if (
                expected_question_id is not None
                and self.active_quiz.get("question_id")
                != expected_question_id
            ):
                return None

            account = await self.db.resolve_user_by_nick(
                sender
            )

            if not account:
                return {
                    "status": "no_account",
                }

            quiz = self.active_quiz
            self.active_quiz = None

        reward = float(quiz["reward"])

        actual = await self.db.transfer_bank_to_user(
            account["user_id"],
            reward,
            transaction_type="QUIZ_REWARD",
            description="Победа в викторине",
        )

        return {
            "status": "won",
            "reward": actual,
        }

    async def resolve_quiz_answer(self, sender, text, request_id=None):
        """
        Единая точка входа для проверки ответа на активную
        викторину — используется и в MUC (muc.py), и в личке
        (on_private_message ниже), чтобы оба пути вели себя
        одинаково (см. ТЗ: атомарный путь + LLM judge как второй
        уровень).

        Порядок:
          1. Быстрый бесплатный Python exact/normalized match
             (answer_matches) — без LLM.
          2. Если не сработал — LLM judge (см.
             llm.py:judge_quiz_answer). Судья ТОЛЬКО классифицирует
             (1/0/None), не начисляет награду и не закрывает
             викторину сам.
          3. Любой "верный" вердикт (exact ИЛИ judge=1) обязан
             пройти через try_win_quiz() — защищённый critical
             section, который атомарно проверяет, что раунд всё
             ещё тот же самый, и только тогда выдаёт награду.

        Возвращает:
          None                     — это вообще не попытка ответить
                                      на викторину (нет активной
                                      викторины, команда, или ни
                                      exact, ни judge не подтвердили
                                      ответ) — вызывающая сторона
                                      должна продолжить обработку
                                      сообщения как обычно.
          {"status": "race_lost"}  — ответ был верным (exact или
                                      judge), но раунд уже закрылся
                                      (гонка с другим участником, или
                                      judge отработал слишком долго и
                                      раунд сменился) — сообщение
                                      нужно молча считать
                                      "обработанным", без выигрышного
                                      сообщения и без дальнейшей
                                      обработки как обычного чата.
          {"status": "no_account"} / {"status": "won", "reward": ...}
                                   — как у try_win_quiz().
        """
        quiz = self.active_quiz

        if not quiz or (text or "").startswith("!"):
            return None

        request_id = request_id or uuid.uuid4().hex[:8]
        question_id = quiz.get("question_id")
        answers = quiz.get("a", [])

        if answer_matches(text, answers):
            method = "exact"
        else:
            logging.info(
                "[QUIZ/JUDGE][%s] sender=%s exact=0",
                request_id,
                sender,
            )

            if not hasattr(self, "judge_quiz_answer"):
                return None

            # Сообщение физически не похоже на попытку ответить
            # (слишком длинное) — не тратим LLM-вызов на оффтоп в
            # чате во время активной викторины.
            if len(text) > QUIZ_JUDGE_MAX_ANSWER_LEN:
                return None

            if hasattr(self, "quiz_judge_rate_limiter"):
                allowed, _reason = (
                    await self.quiz_judge_rate_limiter.allow(
                        sender
                    )
                )

                if not allowed:
                    logging.info(
                        "[QUIZ/JUDGE][%s] rate_limited sender=%s",
                        request_id,
                        sender,
                    )
                    return None

            verdict = await self.judge_quiz_answer(
                quiz.get("q", ""),
                answers,
                text,
                question_id=question_id,
                request_id=request_id,
            )

            if not verdict:
                # False (реально неверно) и None (judge выключен/
                # недоступен/не смог разобрать ответ) трактуются
                # ОДИНАКОВО безопасно — не засчитываем победу.
                return None

            method = "llm_judge"

        result = await self.try_win_quiz(
            sender, expected_question_id=question_id
        )

        if result is None:
            logging.info(
                "[QUIZ] гонка: sender=%s method=%s раунд уже закрыт "
                "(question_id=%s)",
                sender,
                method,
                question_id,
            )
            return {"status": "race_lost"}

        logging.info(
            "[QUIZ] WIN sender=%s method=%s", sender, method
        )

        return result

    async def on_private_message(
        self,
        msg,
    ):
        if msg["type"] not in (
            "chat",
            "normal",
        ):
            return

        body = str(
            msg["body"] or ""
        ).strip()

        if not body:
            return

        # Ответ на токен всегда обрабатывается до CAPTCHA/викторины.
        # Это MUC-private chat: msg["from"] обычно имеет вид room/nick.
        if await self.handle_auth_token(msg):
            return

        # Админ-команда экстренной выдачи клички — тоже только в
        # личку, и тоже раньше остальной логики (см. nickname.py:
        # содержимое личных сообщений в БД не пишется).
        if await self.handle_admin_nickname_command(msg):
            return

        sender = (
            msg["from"].resource
            if msg["from"].resource
            else str(
                msg["from"]
            )
        )

        if (
            sender.casefold()
            == self.nick.casefold()
        ):
            return

        # ----------------------------------------------------
        # ОТВЕТ НА ВИКТОРИНУ В ЛИЧКУ
        #
        # Принимаем ответ только если сообщение пришло как приватный
        # чат внутри игровой комнаты (room/nick), а не с произвольного
        # внешнего JID — так sender гарантированно является MUC-ником,
        # который можно проверить через resolve_user_by_nick.
        # ----------------------------------------------------

        if (
            self.active_quiz
            and msg["from"].bare == self.room
            and not body.startswith("!")
        ):

            # Та же единая точка входа, что и в MUC (muc.py): сначала
            # бесплатный exact match, если не сработал — LLM judge
            # как второй уровень. См. resolve_quiz_answer выше.
            result = await self.resolve_quiz_answer(
                sender, body
            )

            if result is None:
                # Это вообще не попытка ответить на викторину —
                # продолжаем обработку личного сообщения как обычно
                # (ниже, например, CAPTCHA), а не return.
                pass

            elif result["status"] == "race_lost":
                # Кто-то уже успел ответить раньше (гонка), или LLM
                # judge отработал слишком долго и раунд уже сменился.
                return

            elif result["status"] == "no_account":

                self.send_message(
                    mto=msg["from"],
                    mbody=(
                        "🎯 Похоже, ответ верный! Но чтобы "
                        "получить награду, нужен "
                        "зарегистрированный аккаунт.\n"
                        "В общем чате: !согласие, затем "
                        "!регистрация. После этого пришли "
                        "ответ в личку ещё раз — викторина "
                        "остаётся открытой."
                    ),
                    mtype="chat",
                )

                return

            else:
                # result["status"] == "won"

                actual = result["reward"]

                if actual > 0:

                    self.send_message(
                        mto=msg["from"],
                        mbody=(
                            f"🎯 Верно! Ты получил "
                            f"{actual:.2f} коинов из Банка."
                        ),
                        mtype="chat",
                    )

                    self._send_dog_message(
                        self.room,
                        (
                            f"🎯 БИНГО! {sender} "
                            "первым ответил правильно "
                            "(в личке) и получил "
                            f"{actual:.2f} коинов!"
                        ),
                    )

                else:

                    self.send_message(
                        mto=msg["from"],
                        mbody=(
                            "🎯 Верно! Но банк сейчас пуст, "
                            "награда не начислена."
                        ),
                        mtype="chat",
                    )

                    self._send_dog_message(
                        self.room,
                        (
                            f"🎯 {sender} "
                            "ответил правильно (в личке), "
                            "но банк пуст."
                        ),
                    )

                return

        if body.casefold().startswith(
            "привет! это iq проверка"
        ):
            answer = self.solve_captcha(
                body
            )

            if answer:
                self.send_message(
                    mto=msg["from"],
                    mbody=answer,
                    mtype="chat",
                )

    # ========================================================
    # COMMANDS
    # ========================================================

