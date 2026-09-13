"""
Smoke-тест анти-рейд/анти-бот модерации без сети.
"""
import asyncio
import os
import sys
from collections import deque

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
_STUBS_DIR = os.path.join(_PROJECT_ROOT, "smoke_stubs")
sys.path.insert(0, _STUBS_DIR)
sys.path.insert(0, _PROJECT_ROOT)

from dogbot.moderation import DogModerationMixin


class FakeXep45:
    def __init__(self, jid="badbot@example.org/resource"):
        self.jid = jid

    def get_jid_property(self, room, nick, prop):
        if nick == "Пёс" and prop == "role":
            return "moderator"
        if nick == "Пёс" and prop == "affiliation":
            return "none"
        if prop == "jid":
            return self.jid
        return None


class FakeXep425:
    def __init__(self):
        self.calls = []

    async def moderate(self, room, stanza_id, reason=""):
        self.calls.append((str(room), stanza_id, reason))


class FakeLlmClassifier:
    """
    Позволяет тесту явно задать, что вернёт classify_possible_bot()
    для конкретного ника — вместо реального обращения к OpenRouter.
    verdicts: {nick: {"bot": bool, "confidence": float, "reason": str}
    | None} — None имитирует недоступность/ошибку LLM (см.
    llm.classify_possible_bot: тоже возвращает None в этом случае).
    """

    def __init__(self, verdicts=None):
        self.verdicts = verdicts or {}
        self.calls = []

    async def classify_possible_bot(
        self, nick, messages, intervals, request_id=None
    ):
        self.calls.append(
            {
                "nick": nick,
                "messages": list(messages),
                "intervals": list(intervals),
            }
        )
        return self.verdicts.get(nick)


class FakeDB:
    """
    Позволяет тесту явно задать, что вернёт get_room_sender_stats()
    для конкретного ника — вместо реального chat_archive в SQLite.
    stats: {nick: {"first_seen": iso str|None, "message_count": int}}
    """

    def __init__(self, stats=None):
        self.stats = stats or {}
        self.calls = []

    async def get_room_sender_stats(self, room_jid, sender_nick, sender_jid=None):
        self.calls.append(sender_nick)
        return self.stats.get(
            sender_nick, {"first_seen": None, "message_count": 0}
        )


class FakeBot(DogModerationMixin):
    def __init__(self, llm_verdicts=None, db=None):
        self.nick = "Пёс"
        self.quote_lookback = deque(maxlen=60)
        self.xep45 = FakeXep45()
        self.xep425 = FakeXep425()
        self.plugin = {
            "xep_0045": self.xep45,
            "xep_0425": self.xep425,
        }
        self.warnings = []
        self.llm = FakeLlmClassifier(llm_verdicts)
        if db is not None:
            self.db = db
        self.moderation_init()

    def extract_stanza_id(self, msg):
        return msg.get("id")

    def send_message(self, **kwargs):
        self.warnings.append(kwargs)

    async def classify_possible_bot(self, *a, **kw):
        return await self.llm.classify_possible_bot(*a, **kw)


async def main():
    bot = FakeBot()

    # Бот-рейдер: быстрый повторяющийся поток.
    for i in range(8):
        await bot.moderate_incoming_muc(
            {"id": f"bot-{i}"},
            "RaidBot",
            "same spam",
            "room@chat.example",
        )

    assert "badbot@example.org" in bot.mod_fixed_jids
    assert len(bot.xep425.calls) >= 2

    # Человек: тот же flood-паттерн, но без bot identity.
    bot.xep45.jid = "human@example.org/resource"
    bot.mod_events.clear()
    bot.mod_fixed_jids.clear()
    for i in range(6):
        await bot.moderate_incoming_muc(
            {"id": f"human-{i}"},
            "Иван",
            f"spam {i % 2}",
            "room@chat.example",
        )

    assert "human@example.org" not in bot.mod_fixed_jids
    assert bot.warnings

    print("OK moderation smoke test (эвристики)")

    # ========================================================
    # СЕМАНТИЧЕСКИЙ СЛОЙ: медленный бот, который эвристики не видят
    # ========================================================
    # Интервалы ~15с — ни burst_5s/15s/30s, ни duplicate_flood (норм
    # в 15с-окне < 5) не срабатывают ни разу. Ровно тот кейс из
    # рекомендации: "я анатоле" / "яна цист" по кругу. Эвристики
    # должны молчать все 8 сообщений — фиксация возможна только
    # через LLM-классификатор на 8-м сообщении (EVERY_MESSAGES=8).
    #
    # time.monotonic() подменяется управляемыми фейковыми часами —
    # реальные 15с * 8 сообщений никто в smoke-тесте ждать не будет.
    import dogbot.moderation as moderation_module

    class FakeClock:
        def __init__(self, start=1_000.0):
            self.t = start

        def monotonic(self):
            return self.t

        def advance(self, seconds):
            self.t += seconds

    fake_clock = FakeClock()
    real_time_module = moderation_module.time
    moderation_module.time = fake_clock

    try:
        slow_bot = FakeBot(
            llm_verdicts={
                "SlowBot": {
                    "bot": True,
                    "confidence": 0.95,
                    "reason": "повторяющиеся бессвязные фразы",
                }
            }
        )
        slow_bot.xep45.jid = "slowbot@example.org/resource"
        slow_texts = [
            "я анатоле",
            "яна цист",
            "слава зеленскому",
            "я анатоле",
            "админ даун",
            "яна цист",
            "яна цист",
            "я анатоле",
        ]

        handled_flags = []
        for i, body in enumerate(slow_texts):
            if i:
                fake_clock.advance(15.0)
            handled = await slow_bot.moderate_incoming_muc(
                {"id": f"slow-{i}"},
                "SlowBot",
                body,
                "room@chat.example",
            )
            handled_flags.append(handled)
    finally:
        moderation_module.time = real_time_module

    assert "slowbot@example.org" in slow_bot.mod_fixed_jids, (
        "LLM confidence=0.95 должен зафиксировать бота, даже если "
        "финальный численный score от эвристик по-прежнему невелик"
    )
    assert slow_bot.llm.calls, "classify_possible_bot ни разу не вызван"

    # ПРИМЕЧАНИЕ (после разбора реального инцидента, см. коммит):
    # раньше здесь стояло "эвристики первые 7 сообщений не должны
    # трогать вообще" + "ровно 1 вызов LLM на 8-м сообщении" — это
    # было ПРАВИЛЬНОЙ проверкой для СТАРОЙ версии кода, где узкое
    # 15-секундное окно духовно не могло увидеть повтор при паузах
    # 15с между сообщениями, и единственным ловцом такого паттерна
    # была LLM по кадансу. После расширения окна повтора контента до
    # MODERATION_CONTENT_WINDOW_SECONDS (см. config.py и разбор
    # инцидента) эвристики ТЕПЕРЬ ТОЖЕ способны заметить повтор
    # "я анатоле"/"яна цист" в пределах 105с — и могут отправить
    # score в серую зону раньше 8-го сообщения, вызвав LLM раньше.
    # Это желаемое улучшение, а не регресс: проверяем только то, что
    # действительно должно остаться инвариантом — сама LLM-проверка
    # происходит не более пары раз (не на каждое сообщение) и рано
    # или поздно с уверенностью подтверждает бота.
    assert len(slow_bot.llm.calls) <= 2, (
        "проверка не должна происходить чаще, чем пара раз за все 8 "
        f"сообщений (кадансирование/кулдаун), а было {len(slow_bot.llm.calls)}"
    )
    first_call_messages = slow_bot.llm.calls[0]["messages"]
    assert first_call_messages == slow_texts[: len(first_call_messages)], (
        "первый вызов LLM должен получить ХВОСТ реальной истории "
        "этого ника ровно в том порядке, в каком сообщения пришли"
    )
    assert [round(x) for x in slow_bot.llm.calls[0]["intervals"]] == [
        15
    ] * (len(first_call_messages) - 1), (
        "интервалы между сообщениями должны быть ~15с, как и пришли"
    )

    print("OK moderation smoke test (LLM ловит медленного бота)")

    # ========================================================
    # СЕМАНТИЧЕСКИЙ СЛОЙ: watch-порог не фиксирует бота сам по себе
    # ========================================================
    moderation_module.time = fake_clock
    fake_clock.t = 2_000.0
    try:
        watch_bot = FakeBot(
            llm_verdicts={
                "MaybeBot": {
                    "bot": True,
                    "confidence": 0.6,  # выше WATCH(0.55), ниже BOT(0.90)
                    "reason": "немного похоже, но не уверен",
                }
            }
        )
        watch_bot.xep45.jid = "maybebot@example.org/resource"
        for i in range(8):
            if i:
                fake_clock.advance(15.0)
            await watch_bot.moderate_incoming_muc(
                {"id": f"watch-{i}"},
                "MaybeBot",
                f"обычное сообщение номер {i}",
                "room@chat.example",
            )
    finally:
        moderation_module.time = real_time_module

    assert "maybebot@example.org" not in watch_bot.mod_fixed_jids, (
        "confidence=0.6 (watch-уровень) не должен фиксировать бота "
        "в одиночку — это лишь один голос в общем score"
    )
    assert len(watch_bot.llm.calls) == 1

    print("OK moderation smoke test (watch-порог не фиксирует сам по себе)")

    # ========================================================
    # LLM недоступна (None) — деградация без исключений и без
    # ложной фиксации
    # ========================================================
    moderation_module.time = fake_clock
    fake_clock.t = 3_000.0
    try:
        down_bot = FakeBot(llm_verdicts={"DownBot": None})
        down_bot.xep45.jid = "downbot@example.org/resource"
        for i in range(8):
            if i:
                fake_clock.advance(15.0)
            await down_bot.moderate_incoming_muc(
                {"id": f"down-{i}"},
                "DownBot",
                f"сообщение {i}",
                "room@chat.example",
            )
    finally:
        moderation_module.time = real_time_module

    assert "downbot@example.org" not in down_bot.mod_fixed_jids
    assert len(down_bot.llm.calls) == 1, (
        "даже при verdict=None факт проверки должен кэшироваться — "
        "иначе недоступный LLM дёргался бы на каждое сообщение"
    )

    print("OK moderation smoke test (LLM недоступна -> безопасная деградация)")

    # ========================================================
    # ДИАГНОСТИКА: нет прав модератора -> тихий return, но с одним
    # предупреждением в лог на комнату (реальный кейс, из-за которого
    # весь антирейд оказался незаметно неактивен -- см. журнал)
    # ========================================================
    no_rights_bot = FakeBot()
    no_rights_bot.xep45 = FakeXep45(jid="whoever@example.org/resource")
    no_rights_bot.xep45.get_jid_property = lambda room, nick, prop: (
        None  # Пёс тут не moderator/admin/owner
    )
    no_rights_bot.plugin["xep_0045"] = no_rights_bot.xep45

    handled = await no_rights_bot.moderate_incoming_muc(
        {"id": "x-1"},
        "кто-то",
        "флуд флуд флуд",
        "room@no-rights.example",
    )
    assert handled is False
    assert not no_rights_bot.llm.calls, (
        "без прав модератора LLM-проверка не должна вызываться вообще"
    )
    assert (
        "room@no-rights.example",
        "not_privileged",
    ) in no_rights_bot.mod_last_notice, (
        "должно остаться диагностическое предупреждение в логах о "
        "том, что модерация неактивна из-за отсутствия прав"
    )

    print("OK moderation smoke test (диагностика: нет прав модератора)")

    # ========================================================
    # ЭВАЗИЯ: рейдер вставляет одно случайное слово в шаблон на
    # каждое сообщение — exact duplicate_ratio почти всегда 0, но
    # ядро сообщения (большинство токенов) не меняется. Должно
    # ловиться через near_duplicate_flood/low_vocabulary, а не
    # проходить незамеченным.
    # ========================================================
    evasive_bot = FakeBot()
    evasive_bot.xep45.jid = "evasive@example.org/resource"
    templates = [
        "путин вор картошка",
        "путин вор банан",
        "путин вор облако",
        "путин вор ракета",
        "путин вор дерево",
        "путин вор камень",
    ]
    evasive_flags = []
    for i, body in enumerate(templates):
        handled = await evasive_bot.moderate_incoming_muc(
            {"id": f"ev-{i}"},
            "EvasiveRaider",
            body,
            "room@chat.example",
        )
        evasive_flags.append(handled)

    assert any(evasive_flags), (
        "вставка случайного слова в неизменный шаблон должна была "
        "поймать флуд через near_duplicate_flood/low_vocabulary, "
        "несмотря на то, что exact duplicate_ratio почти всегда 0"
    )

    print("OK moderation smoke test (эвазия: вставка случайного слова в шаблон)")

    # ========================================================
    # АРХИВ: давний активный участник получает скидку (trusted_history)
    # и не должен получить настолько же высокий score, как совершенно
    # новый ник с идентичным поведением.
    # ========================================================
    import dogbot.moderation as moderation_module

    class FakeClock:
        def __init__(self, start=1_000.0):
            self.t = start

        def monotonic(self):
            return self.t

        def advance(self, seconds):
            self.t += seconds

    fake_clock = FakeClock(start=5_000.0)
    real_time_module = moderation_module.time
    moderation_module.time = fake_clock

    try:
        veteran_db = FakeDB(
            stats={
                "Ветеран": {
                    "first_seen": "2020-01-01T00:00:00",
                    "message_count": 500,
                }
            }
        )
        veteran_bot = FakeBot(db=veteran_db)
        veteran_bot.xep45.jid = "veteran@example.org/resource"

        newbie_bot = FakeBot()  # без db => tenure неизвестен, без скидки
        newbie_bot.xep45.jid = "newbie@example.org/resource"

        veteran_score = None
        newbie_score = None
        for i in range(6):
            fake_clock.advance(0.3)
            await veteran_bot.moderate_incoming_muc(
                {"id": f"vet-{i}"}, "Ветеран", f"привет всем {i}", "room@chat.example"
            )
            await newbie_bot.moderate_incoming_muc(
                {"id": f"new-{i}"}, "Новичок", f"привет всем {i}", "room@chat.example"
            )

        # Прямая проверка: сам scorer для одинаковых events должен
        # получить trusted_history у ветерана.
        vet_key = veteran_bot._mod_key("veteran@example.org/resource", "Ветеран")
        vet_events = list(veteran_bot.mod_events[vet_key])
        vet_tenure = await veteran_bot._mod_tenure(
            vet_key, "Ветеран", "veteran@example.org/resource", "room@chat.example"
        )
        assert vet_tenure["message_count"] == 500
        assert veteran_db.calls, "get_room_sender_stats ни разу не вызван"
    finally:
        moderation_module.time = real_time_module

    print("OK moderation smoke test (архив: стаж ветерана читается и кэшируется)")

    # ========================================================
    # БЕЗОПАСНОСТЬ: короткий, но очень интенсивный всплеск (мало
    # сообщений) НЕ должен приводить к постоянному бану, даже если
    # score формально перевалил за MODERATION_SCORE_BAN — нужен либо
    # устойчивый поток (>= MODERATION_BAN_SUSTAINED_EVENTS событий),
    # либо явный bot-identity-hint, либо подтверждение LLM. Это
    # главное требование ТЗ: "не начинает банить обычных
    # пользователей".
    # ========================================================
    burst_human = FakeBot()
    burst_human.xep45.jid = "burst-human@example.org/resource"
    burst_key = burst_human._mod_key(
        "burst-human@example.org/resource", "Эмоциональный"
    )
    last_score = None
    for i in range(7):
        await burst_human.moderate_incoming_muc(
            {"id": f"burst-{i}"}, "Эмоциональный", "паника паника", "room@chat.example"
        )
        last_score, _ = burst_human._mod_score(
            list(burst_human.mod_events[burst_key]),
            "паника паника",
            "Эмоциональный",
            "burst-human@example.org/resource",
        )

    # Контроль эксперимента: score ДЕЙСТВИТЕЛЬНО перевалил за
    # MODERATION_SCORE_BAN на этих 7 сообщениях — иначе проверка
    # ниже ничего не доказывает (structural gate просто не был бы
    # даже задействован).
    from dogbot.config import MODERATION_SCORE_BAN, MODERATION_BAN_SUSTAINED_EVENTS

    assert last_score >= MODERATION_SCORE_BAN, (
        f"тест сконструирован неверно: score={last_score} должен был "
        f"превысить MODERATION_SCORE_BAN={MODERATION_SCORE_BAN} — иначе "
        "проверка структурного гейта ничего не проверяет"
    )
    assert len(burst_human.mod_events[burst_key]) < MODERATION_BAN_SUSTAINED_EVENTS

    assert "burst-human@example.org" not in burst_human.mod_fixed_jids, (
        "7 сообщений подряд (меньше MODERATION_BAN_SUSTAINED_EVENTS) без "
        "bot-identity-hint не должны приводить к постоянному бану, даже "
        "если score формально >= MODERATION_SCORE_BAN"
    )

    print(
        "OK moderation smoke test (безопасность: короткий всплеск "
        "человека не банит навсегда)"
    )

    # ========================================================
    # РЕАЛЬНЫЙ ИНЦИДЕНТ (ru@chat.404.city, 2026-09-11 21:01-21:13,
    # ник "Жора баклажанов"): рейдер выдерживал паузы 10-30с (одна
    # пауза почти 6 минут) специально, чтобы не попасть в короткие
    # burst-окна, и обфусцировал текст растягиванием слов пробелами
    # по одной букве / обкладыванием мусорными символами ("л и з н и
    # т е    а н    у с", "тxоi9zзуM73Oк л и з н и т е ... CюRрLM...").
    # На проде это НЕ поймалось вообще: score не поднимался выше 2,
    # а LLM-арбитр после первой проверки (на 8-м сообщении) больше ни
    # разу не вызвался за оставшиеся ~20 сообщений — счётчик каданса
    # был завязан на len(events), которое обнулилось после паузы
    # длиннее ретеншена, и "застрял" в недостижимом состоянии навсегда
    # (см. подробный разбор в INCIDENT.md). Воспроизводим ровно эту
    # переписку и проверяем, что теперь рейдер всё-таки ловится.
    # ========================================================
    _real_incident_msgs = [
        (0.0, "в Ы  Вс Е  пСИНЫ"),
        (5.636, "сллава бандеере"),
        (11.272, "штообб"),
        (14.199, "вЫ"),
        (15.892, "сдохли"),
        (17.717, "лизните анус"),
        (18.218, "блаблабла слава зеленскому"),
        (34.047, "* АДмин ЛОХ"),
        (80.318, "слава зеленскому"),
        (98.203, ")))  я    а  н  а  т  о л е ???"),
        (157.028, "вы все псины"),
        (158.368, "вы всё пидоры"),
        (525.752, "лиззните анусс"),
        (558.223, "тxоi9zзуM73Oк л и з н и т е    а н    у с CюRрLMа9яжsслп"),
        (570.643, "л И З Н и Т е    А н у С"),
        (589.991, "админ"),
        (591.344, "л    о    х"),
        (603.531, "qwerty с л а вв а    б а н д ее р е"),
        (613.656, "*  слав а б а ндере"),
        (630.290, "слава зеленскому"),
        (646.022, "сла ва банд е ре"),
        (658.358, "тупыые хуесоосы"),
        (661.690, "яна"),
        (662.723, "цист"),
        (675.855, "тупые"),
        (679.608, "Х У Е С О с Ы"),
        (693.616, "я аНаТОлЕ"),
    ]

    class RealClock:
        def __init__(self):
            self.t = 0.0

        def monotonic(self):
            return self.t

    real_clock = RealClock()
    real_time_module2 = moderation_module.time
    moderation_module.time = real_clock

    try:
        # Сценарий (1): LLM ничего не подтверждает (имитация "молчания"
        # реальной модели/отсутствия сильного сигнала на первой
        # проверке) — здесь проверяем ИМЕННО каданс: раньше после
        # первой проверки счётчик, завязанный на len(events), "застревал"
        # навсегда после паузы длиннее ретеншена, и вторая проверка
        # не наступала никогда за все оставшиеся ~18 сообщений.
        silent_bot = FakeBot()
        silent_bot.xep45.jid = None
        content_flags_seen = set()
        for t, body in _real_incident_msgs:
            real_clock.t = t
            await silent_bot.moderate_incoming_muc(
                {"id": f"incident-silent-{t}"},
                "Жора баклажанов",
                body,
                "ru@chat.404.city",
            )
            key = silent_bot._mod_key(None, "Жора баклажанов")
            events_now = list(silent_bot.mod_events[key])
            if events_now:
                _, r = silent_bot._mod_score(
                    events_now, body, "Жора баклажанов", None
                )
                content_flags_seen.update(r)

        # Сценарий (2): LLM уверенно подтверждает бота при первом же
        # реальном вызове — проверяем, что пайплайн доводит дело до
        # конца (постоянная фиксация), а не только "видит" подозрение.
        raider_bot = FakeBot(
            llm_verdicts={
                "Жора баклажанов": {
                    "bot": True,
                    "confidence": 0.9,
                    "reason": (
                        "чередует бессвязные оскорбления с явной "
                        "посимвольной обфускацией (буквы через пробел, "
                        "случайные вставки) — не похоже на естественный "
                        "человеческий набор текста"
                    ),
                }
            }
        )
        # В реальном инциденте комната была полу-анонимной для бота
        # (occupant JID недоступен), фиксация шла бы по нику.
        raider_bot.xep45.jid = None
        real_clock.t = 0.0
        for t, body in _real_incident_msgs:
            real_clock.t = t
            await raider_bot.moderate_incoming_muc(
                {"id": f"incident-{t}"},
                "Жора баклажанов",
                body,
                "ru@chat.404.city",
            )
    finally:
        moderation_module.time = real_time_module2

    assert len(silent_bot.llm.calls) >= 2, (
        "LLM-арбитр должен был вызываться более одного раза за 26 "
        "сообщений с паузами и одним 6-минутным разрывом, даже если "
        "ни один из вызовов ничего не подтвердил — раньше после первой "
        "же проверки каданс необратимо застревал (было вызовов: "
        f"{len(silent_bot.llm.calls)})"
    )
    assert content_flags_seen & {
        "char_obfuscation_flood",
        "repeated_phrase_hint",
    }, (
        "детерминированные эвристики должны были заметить хотя бы "
        "часть посимвольной обфускации ('л и з н и т е ... а н у с' "
        f"и т.п.) сами по себе; сработавшие причины: {content_flags_seen}"
    )

    assert "жора баклажанов" in raider_bot.mod_fixed_nicks, (
        "как только LLM-арбитр реально дозвался и уверенно подтвердил "
        "бота, пайплайн должен закончиться постоянной фиксацией — "
        "раньше вся переписка проходила насквозь без единого действия"
    )

    print("OK moderation smoke test (реальный инцидент: медленный обфусцирующий рейдер)")

    # ========================================================
    # ВТОРОЙ РЕАЛЬНЫЙ ИНЦИДЕНТ (ru@conference.jajaba.ru, 2026-09-12,
    # тот же ник "Жора баклажанов", повторная атака после предыдущего
    # патча). Каданс LLM уже не застревал (проверялась несколько раз
    # за диалог), эвристики регулярно доходили до watch/restrict
    # (score 4-7) — но САМА LLM раз за разом отвечала bot=false с
    # confidence~0.3-0.35, объясняя это так: "интервалы нерегулярны и
    # нет явных дословных повторов шаблона". Реальная причина: рейдер
    # СПЕЦИАЛЬНО держит нерегулярные интервалы и обфусцирует текст,
    # чтобы не выглядеть "типичным ботом" — а промпт LLM-арбитра как
    # раз просил её искать регулярные интервалы и дословные повторы
    # как признак бота, то есть буквально учил её пропускать умную
    # эвазию. Score из-за этого никогда не доходил ни до
    # MODERATION_SCORE_DELETE, ни тем более до BAN — рейдер продолжал
    # флудить необнаруженным ещё много минут.
    #
    # Патч: (а) промпт classify_possible_bot переформулирован — прямо
    # называет обфускацию (буквы через пробел, мусорные символы) и
    # намеренно нерегулярные интервалы признаками ИМЕННО умной эвазии,
    # а не индикатором "живого человека"; (б) НЕЗАВИСИМО от мнения
    # LLM — "три предупреждения": повторные попадания одного и того
    # же ключа на уровень restrict в течение получаса эскалируются в
    # ban сами по себе (см. MODERATION_RESTRICT_STRIKES_TO_ESCALATE).
    #
    # Здесь проверяем именно (б) — САМЫЙ жёсткий случай, когда LLM
    # раз за разом реалистично отвечает "не бот" (как в проде), и всё
    # равно всё ловится через накопление независимых restrict-эпизодов.
    # ========================================================
    _second_incident_msgs = [
        (0.0, "я АНАТОлЕ"), (0.001, "вы  в с е пси ны"),
        (16.0, "лолкек вы  вс е  пс и н ы Qйnd1кбд"), (16.817, "штоб выы сдохли"),
        (18.087, "интересно"), (31.847, "штоб"), (34.864, "вы"),
        (37.754, "с д о х л и"), (52.224, "блаблабла с лаВА банД ере"),
        (62.037, "_ слава зеленскому"), (75.601, "asdfgh тупые хуесосы"),
        (76.893, "ШТОб вы СГнИли В канаВЕ"), (88.539, "яна fgшнm7Wяв цист"),
        (128.523, "тупые хуесосы tBдMeгtp"), (129.910, "лизните !!!"),
        (131.985, "а н у с"), (143.895, "штоб вы сгнили в канаве"),
        (149.005, "понятно"), (165.434, "ш т  т о  б     в ы     с  д о х л л и"),
        (166.495, "А Д М и н    л о Х    ) ) )"), (168.074, "((( штоб вы сгнили в канаве"),
        (176.824, "ываплор вы все  пидоры"), (190.6, "што б вы сгни лии в каннаве хзхзхз"),
        (196.633, "Ли зни тЁ а нУ С"), (211.356, "ВЫ"), (213.587, "в в С е"),
        (217.594, "ПпИИдОрыы"), (225.805, "слава бандере"), (245.325, "вы вссё псинны блаблабла"),
        (246.015, "а д м и н    д А У Н"), (246.308, "штоб вы сгнили в канаве"),
        (251.906, "админ даун ээээ"), (263.236, "админ"), (264.615, "даун"),
        (312.954, "лизните анус"), (326.833, "ЛиЗНИТЕ АНуС"), (327.573, "штоб вы сдохли"),
        (329.085, "слава зеленскому"), (329.626, "я ываплор анатоле"),
    ]

    second_clock = FakeClock(start=0.0)
    real_time_module3 = moderation_module.time
    moderation_module.time = second_clock

    try:
        realistic_bot = FakeBot(
            llm_verdicts={
                "Жора баклажанов": {
                    "bot": False,
                    "confidence": 0.35,
                    "reason": "выглядит как эмоциональный троллинг живого человека",
                }
            }
        )
        realistic_bot.xep45.jid = "adminlox@jajaba.ru/xe8srNFN4Aum"
        banned_at_index = None
        for i, (t, body) in enumerate(_second_incident_msgs):
            second_clock.t = t
            await realistic_bot.moderate_incoming_muc(
                {"id": f"incident2-{i}"},
                "Жора баклажанов",
                body,
                "ru@conference.jajaba.ru",
            )
            if realistic_bot.mod_fixed_jids and banned_at_index is None:
                banned_at_index = i
    finally:
        moderation_module.time = real_time_module3

    assert "adminlox@jajaba.ru" in realistic_bot.mod_fixed_jids, (
        "рейдер должен был в итоге зафиксироваться через накопление "
        "restrict-эпизодов ('три предупреждения'), ДАЖЕ когда LLM на "
        "каждый отдельный вызов реалистично отвечает bot=false — "
        "раньше вся переписка проходила насквозь без единого banned"
    )
    assert banned_at_index is not None and banned_at_index < len(
        _second_incident_msgs
    ) - 5, (
        "фиксация должна была случиться заметно раньше последнего "
        f"сообщения (случилась на индексе {banned_at_index} из "
        f"{len(_second_incident_msgs)}) — иначе 'три предупреждения' "
        "срабатывают слишком поздно, чтобы реально защитить комнату"
    )

    print(
        "OK moderation smoke test (второй реальный инцидент: "
        "эскалация через 'три предупреждения' без подтверждения LLM)"
    )


if __name__ == "__main__":
    asyncio.run(main())
