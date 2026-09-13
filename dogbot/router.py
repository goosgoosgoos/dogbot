from .config import *
from .web import extract_urls, split_web_urls
import json

# ============================================================
# LLM CONTEXT ROUTER
# ============================================================
#
#   сообщение
#       │
#       ▼
#  Python precheck (context_precheck)  — бесплатно, точные триггеры
#       │
#       ├── всё решено ────────────────────────────┐
#       │                                           │
#       └── что-то неясно                           │
#              │                                    │
#              ▼                                    │
#      router LLM (_ask_context_router)             │
#      только по оставшимся полям,                  │
#      ~50-100 токенов, temperature=0                │
#              │                                    │
#              ▼                                    ▼
#                     resolve_context_needs()
#                              │
#                              ▼
#                   routing-флаги для ask_llm()
#                              │
#                              ▼
#                       context builder
#                       (собирает промпт
#                        только из нужного)
#                              │
#                              ▼
#                          основной LLM
#
# Роутер никогда не решает, ЧТО ответить — только то, какие
# источники данных понадобятся основной модели. Если он выключен
# или недоступен — используются безопасные дефолты (см.
# _FAILSAFE_DEFAULTS), поведение деградирует к "как было до
# рефакторинга", а не ломается.

ROUTER_FLAG_KEYS = (
    "needs_memory",
    "needs_history",
    "needs_economy",
    "needs_market",
    "about_russoturisto",
    "needs_recall",
    "needs_web",
    "needs_crew",
    "needs_temporal",
    "needs_chat_archive",
    "needs_mention_facts",
)

# Короткое описание каждого флага — промпт роутера собирается
# ТОЛЬКО из полей, реально оставшихся неясными после Python-уровня
# (см. context_precheck/resolve_context_needs), а не из всех сразу.
_FLAG_DESCRIPTIONS = {
    "needs_memory": (
        "нужно ли для ответа знать личные факты/предпочтения "
        "автора (то, что он раньше о себе рассказывал в чате)"
    ),
    "needs_history": (
        "является ли сообщение продолжением недавнего разговора "
        "(нужны ли последние реплики чата, чтобы понять контекст). "
        "Прямое обращение к боту САМО ПО СЕБЕ не означает, что "
        "нужна история — короткое приветствие/благодарность/"
        "самодостаточный вопрос истории не требуют, а уточнение, "
        "несогласие, «а ты не путаешь» и подобные реплики без "
        "истории теряют смысл"
    ),
    "needs_economy": (
        "спрашивает ли автор про свой баланс, банк чата или "
        "инфляцию в игровой экономике"
    ),
    "needs_market": (
        "касается ли сообщение рынка лотов/инвестиций/скупки "
        "информации в чате"
    ),
    "about_russoturisto": (
        "идёт ли речь о пользователе russoturisto (в том числе "
        "непрямо — «твой кореш», «тот чувак, с которым ты давно "
        "знаком» и т.п.), даже если по имени он не назван"
    ),
    "needs_recall": (
        "просит ли автор ЦЕЛЕНАПРАВЛЕННО покопаться в памяти и "
        "вспомнить что-то конкретное — про себя, про другого "
        "участника или про случившееся раньше (а не просто "
        "поддержать разговор)"
    ),
    "needs_crew": (
        "нужна ли информация о текущем составе корабля/конференции, "
        "кто сейчас на борту, должности, присутствие конкретного участника "
        "или статус вспомогательного экипажа"
    ),
    "needs_temporal": (
        "нужно ли искать информацию по конкретной дате/времени или "
        "историческому моменту (вчера, тогда, утром, в 10:00, 2026-09-01 и т.п.)"
    ),
    "needs_chat_archive": (
        "нужно ли поднять старые сообщения MUC из короткого архива "
        "(старше обычной self.history) для ответа; если да, определить "
        "историческую дату/диапазон, о котором говорит автор"
    ),
    "needs_web": (
        "нужен ли для ответа свежий факт из интернета — то, "
        "чего заведомо нет и не может быть в памяти бота "
        "(новости, курсы, актуальные события, проверка "
        "конкретного утверждения), а не просто разговор по "
        "теме"
    ),
    "needs_mention_facts": (
        "в сообщении упомянут/адресован участник, ОТЛИЧНЫЙ от "
        "автора (не запрос целенаправленно порыться в памяти — "
        "это needs_recall, а просто разговор о нём/с ним), и "
        "знание о нём конкретного факта реально помогло бы "
        "ответить по существу, а не выдумывать"
    ),
}

# Безопасные значения, если роутер выключен/недоступен/сломался.
# needs_memory, needs_history и about_russoturisto ДО этого
# рефакторинга были включены всегда — значит, при сбое роутера
# сохраняем это поведение (ничего не теряем функционально).
# needs_economy/needs_market — новые блоки контекста, которых в
# промпте раньше не было вообще, поэтому дефолт для них —
# консервативный False, а не "на всякий случай показать".
# needs_web — тоже новый блок контекста (веб-чтение раньше у Пса
# отсутствовало вообще), поэтому дефолт при сбое роутера —
# консервативный False, а не "на всякий случай сходить в сеть".
# Обрати внимание: если в сообщении есть явная ссылка, needs_web
# в любом случае решается программно в context_precheck() ниже, и
# до этого фолбэка дело не доходит вообще.
_FAILSAFE_DEFAULTS = {
    "needs_memory": True,
    "needs_history": True,
    "needs_economy": False,
    "needs_market": False,
    "about_russoturisto": True,
    "needs_recall": False,
    "needs_web": False,
    "needs_crew": False,
    "needs_temporal": False,
    "needs_chat_archive": False,
    "needs_mention_facts": False,
}


class DogRouterMixin:

    # ========================================================
    # УРОВЕНЬ 0 — без LLM: точные триггеры + то, что и так уже
    # известно вызывающей стороне (muc.py) из metadata сообщения.
    # ========================================================

    def context_precheck(
        self,
        text,
        mention_russoturisto,
        is_direct_to_me,
        target_nick,
        sender=None,
        quoted_text=None,
        quote_author=None,
        mentioned_nicks=None,
    ):
        """
        Решает routing-флаги, которые не требуют понимания смысла.

        Возвращает dict с ключами ROUTER_FLAG_KEYS. Значение
        True/False — Python уверен и дальше LLM не спрашиваем.
        Отсутствие ключа (через .get(..., None) у вызывающей
        стороны) — "не знаю", решает router LLM.
        """
        stripped = (text or "").strip()
        lower = stripped.casefold()

        flags = {}

        # Явная цитата — это не просто текст: автор сознательно
        # ссылается на другое сообщение. Поэтому историю нельзя
        # отключать роутером даже если собственная часть реплики
        # короткая или формально не похожа на продолжение диалога.
        # Особенно важно для сценария: "> Пёс: ..." + пустой/короткий
        # комментарий — смысл обращения находится в процитированной
        # реплике.
        if quoted_text:
            flags["needs_history"] = True
            flags["needs_chat_archive"] = True

        # Ссылка в сообщении — детектируем СРАЗУ и детерминированно,
        # без LLM: если автор прислал URL, страницу нужно читать
        # программно (dogbot/web.py:fetch_web_page), а не спрашивать
        # роутер "нужен ли веб". web_urls — не булев routing-флаг,
        # а список для ask_llm(); resolve_context_needs() прокидывает
        # его дальше отдельно от ROUTER_FLAG_KEYS (см. ниже).
        #
        # Картинки (image_urls) выделяются из ТЕХ ЖЕ ссылок по
        # расширению (см. web.py:split_web_urls/is_image_url) — это
        # тоже чисто синтаксическое, детерминированное решение:
        # роутер вообще не умеет "хотеть" картинку, он про неё даже
        # не спрашивается. В XMPP картинка почти всегда приходит
        # именно так — обычной http(s)-ссылкой на файл (XEP-0363
        # HTTP Upload), поэтому ей не нужен отдельный "нашёл ли я
        # вложение" путь.
        all_urls = extract_urls(stripped)
        web_urls, image_urls = split_web_urls(all_urls)

        if web_urls:
            flags["needs_web"] = True
            flags["web_urls"] = web_urls

        if image_urls:
            flags["needs_vision"] = True
            flags["image_urls"] = image_urls

        # Упоминание по имени, обращение к нему или он сам сейчас
        # пишет — во всех трёх случаях Python видит не хуже LLM,
        # семантика не нужна. sender учитываем отдельно: dossier в
        # ask_llm() и так всегда отмечает "russoturisto — твой
        # кореш" когда он сам пишет (см. llm.py) — если бы
        # about_russoturisto тут остался неясным, абзац-дружбы в
        # system_prompt мог не попасть в промпт, а СТАТУС в dossier
        # — попасть, и модель получила бы противоречивую картину.
        if (
            mention_russoturisto
            or (
                target_nick
                and str(target_nick).casefold()
                == "russoturisto"
            )
            or (
                sender
                and str(sender).casefold()
                == "russoturisto"
            )
        ):
            flags["about_russoturisto"] = True

        # Запросы о текущем экипаже и историческом моменте лучше решать
        # детерминированно: это выбор источника данных, а не смысл ответа.
        crew_markers = (
            "экипаж", "на корабле", "на борту", "кто присутствует",
            "кто сейчас", "кто тут", "кто в комнате", "кто зашел",
            "кто вошел", "кто вышел", "кто ливнул", "должность",
            "матрос", "боцман", "капитан", "кок",
        )
        temporal_markers = (
            "сегодня", "вчера", "позавчера", "завтра", "тогда",
            "раньше", "недавно", "утром", "днем", "вечером", "ночью",
            "час назад", "минут назад", "дней назад", "неделю назад",
            "в  ", "2026-", "/", ".09.", ".08.",
        )
        if any(marker in lower for marker in crew_markers):
            flags["needs_crew"] = True
        if any(marker in lower for marker in temporal_markers) or re.search(r"\b20\d{2}[-./]\d{1,2}[-./]\d{1,2}\b", lower) or re.search(r"\b\d{1,2}\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\b", lower):
            flags["needs_temporal"] = True
        if target_nick:
            flags["needs_crew"] = True

        # Совсем короткая междометная реплика ("ахаха", "+1",
        # "кек") — решаем всё сразу, роутер даже не понадобится.
        if is_trivial_reaction(stripped):

            for key in ROUTER_FLAG_KEYS:
                flags.setdefault(key, False)

            return flags

        # Точные ключевые слова экономики/рынка.
        if has_marker(lower, ECONOMY_MARKERS):
            flags["needs_economy"] = True

        if has_marker(lower, MARKET_MARKERS):
            flags["needs_market"] = True

        # Явная просьба "вспомни"/"помнишь" — Python уверен сам,
        # роутер не нужен.
        if has_marker(lower, RECALL_MARKERS):
            flags["needs_recall"] = True

        # Спонтанный recall с низким шансом — независимо от того,
        # просил ли автор явно. Проверяем только если маркер выше
        # не сработал (незачем катать кубик, если и так True), и
        # не для тривиальных реакций — до этой точки код уже
        # вернулся бы выше, если бы is_trivial_reaction(stripped)
        # был True.
        #
        # recall_ambient=True помечает ИМЕННО этот случайный путь
        # (в отличие от явного маркера выше) — llm.py использует
        # его, чтобы спонтанная "память по кубику" ни при каких
        # условиях не пыталась резолвить ДРУГОГО человека (nick/
        # fuzzy/LLM-роутер целей) и не поднимала чужое досье молча.
        # Спонтанная память может быть только "о себе", целенаправленный
        # поиск чужих фактов должен запрашиваться явно.
        if (
            "needs_recall" not in flags
            and random.random() < RECALL_RANDOM_CHANCE
        ):
            flags["needs_recall"] = True
            flags["recall_ambient"] = True

        # Раньше здесь стояло жёсткое правило: сообщение не
        # адресовано боту и не обращено к кому-то по имени ->
        # needs_history=False, история чата в промпт вообще не
        # шла. Проблема в том, что Пёс всё равно иногда отвечает на
        # такие фоновые реплики (см. случайный trigger в muc.py) —
        # и получал 0 строк истории чата, отвечая "из ниоткуда",
        # без понимания, о чём вообще разговор. Позже был короткий
        # эксперимент с обратной крайностью — жёстко форсить
        # needs_history=True программно при is_direct_to_me,
        # решение вообще не спрашивая роутер — но это создавало
        # обратную проблему: даже короткое "Пёс, привет" или
        # "спасибо" тянуло историю просто потому, что было прямым
        # обращением. Обе крайности были ошибкой в одну и ту же
        # сторону — недоверие роутеру вместо того, чтобы дать ему
        # достаточно сигнала для правильного решения. Теперь
        # needs_history для ЛЮБОГО сообщения (прямого или фонового)
        # явно НЕ фиксируется на Python-уровне и остаётся неясным
        # (см. setdefault(..., None) ниже) — решает router LLM по
        # смыслу сообщения, при этом is_direct_to_me передаётся ему
        # как ОДИН ИЗ сигналов для классификации (см.
        # _ask_context_router: "Прямое обращение к боту" в
        # user-сообщении), а не как готовый ответ. При выключенном/
        # недоступном роутере действует общий фейлсейф
        # _FAILSAFE_DEFAULTS["needs_history"] = True (дореформенное
        # поведение — история передаётся всегда).

        # needs_mention_facts: Python сам решает случай "некого
        # факт-чекать" — если в сообщении вообще нет упоминания
        # кого-то, кроме автора, спрашивать роутера бессмысленно, а
        # если needs_recall уже точно True (явное "вспомни"/
        # "помнишь" или сработал случайный шанс выше) — это более
        # глубокий, целенаправленный поиск по тому же человеку, и
        # дублировать его лёгким mention-facts не нужно. russoturisto
        # тоже исключаем: про него уже есть отдельный, всегда
        # активный блок "кореш" (about_russoturisto), точечный факт
        # тут был бы избыточен. Во всех ОСТАЛЬНЫХ случаях — есть
        # реальный кандидат, и НУЖЕН ли по нему факт — решает
        # роутер по смыслу сообщения (см. needs_mention_facts в
        # _FLAG_DESCRIPTIONS и mention_fact_category в
        # _ask_context_router).
        mention_candidates = []

        if target_nick:
            mention_candidates.append(target_nick)

        mention_candidates.extend(mentioned_nicks or [])

        has_mention_candidate = any(
            str(nick).casefold()
            not in {
                (sender or "").casefold(),
                (getattr(self, "nick", "") or "").casefold(),
                "russoturisto",
            }
            for nick in mention_candidates
        )

        if not has_mention_candidate or flags.get("needs_recall"):
            flags["needs_mention_facts"] = False

        # Остальное — неясно, пусть решает роутер.
        for key in ROUTER_FLAG_KEYS:
            flags.setdefault(key, None)

        return flags

    # ========================================================
    # УРОВЕНЬ 1 — микро-LLM роутер (вызывается только для
    # действительно неясных полей).
    # ========================================================

    async def _ask_context_router(
        self,
        text,
        unresolved_keys,
        quoted_text=None,
        quote_author=None,
        is_direct_to_me=None,
        message_time=None,
        force_archive_date=False,
    ):
        """
        Просит дешёвую микро-модель классифицировать ТОЛЬКО
        unresolved_keys — не гоняем в промпте схему из пяти полей,
        если Python не смог решить лишь одно-два.

        is_direct_to_me — не флаг для классификации (Python это уже
        знает), а СИГНАЛ, который передаётся роутеру как один из
        входных фактов о сообщении — в первую очередь для
        needs_history (см. её описание в _FLAG_DESCRIPTIONS: прямое
        обращение само по себе не гарантирует, что нужна история).

        force_archive_date — needs_chat_archive мог быть уже решён
        True на precheck-уровне (например, из-за quoted_text — см.
        context_precheck) и НЕ попасть в unresolved_keys. Раньше это
        значило, что archive_target_date/end_date вообще никогда не
        запрашивались у роутера в таком случае (условие ниже смотрело
        только на unresolved_keys) — historical_archive_mode в llm.py
        оставался False, self.history не подавлялся, и настоящий
        chat_archive (найденный своим отдельным fallback-разбором в
        core.py/get_chat_archive_context) мог смешиваться с обычной
        историей, включая уже сгенерированные ошибки Пса. Теперь
        вызывающая сторона (resolve_context_needs) явно просит
        задать вопрос про дату ДАЖЕ когда needs_chat_archive сам по
        себе уже не требует классификации.

        message_time — метка времени сообщения (строка ISO или dict
        в форме get_current_time()); используется ТОЛЬКО для
        Python-фолбэка даты (_resolve_router_temporal_date, см. ниже
        по файлу и в resolve_context_needs) — если не передана,
        фолбэк использует get_current_time(). Собственный "Текущее
        время" в системном промпте роутера не меняется этим
        параметром, чтобы не трогать уже проверенное поведение для
        существующих вызовов, которые message_time не передают.

        Возвращает {key: bool} по unresolved_keys (плюс
        archive_target_date/archive_target_end_date/
        mention_fact_category, если соответствующие поля были в
        unresolved_keys ИЛИ был запрошен force_archive_date), либо
        None при сбое/выключенном роутере — вызывающая сторона
        обязана сама подставить _FAILSAFE_DEFAULTS.
        """
        want_archive_date = bool(
            "needs_chat_archive" in unresolved_keys
            or force_archive_date
        )

        if not unresolved_keys and not want_archive_date:
            return {}

        if not ROUTER_ENABLED:
            return None

        fields_desc = "\n".join(
            f"{key}: 0/1 — {_FLAG_DESCRIPTIONS[key]}"
            for key in unresolved_keys
        )

        # Если архив потенциально нужен, тот же вызов роутера сразу
        # определяет временной якорь. Это важно: второй LLM-вызов для
        # "какую дату искать?" не нужен. want_archive_date учитывает
        # и обычный случай (needs_chat_archive среди unresolved_keys),
        # и force_archive_date (needs_chat_archive уже True с
        # precheck, но дата всё равно не определена).
        archive_fields = ""
        if want_archive_date:
            archive_fields = (
                "\narchive_target_date: строка YYYY-MM-DD или null — "
                "главная дата исторического события; для 'вчера' вычисли "
                "дату относительно текущего времени выше.\n"
                "archive_target_end_date: строка YYYY-MM-DD или null — "
                "конец диапазона включительно; если нужен один день, "
                "повтори archive_target_date.\n"
            )

        # Аналогично needs_chat_archive/archive_target_date: если
        # needs_mention_facts в принципе может понадобиться, тот же
        # вызов роутера сразу выбирает КАТЕГОРИЮ факта — не "любой
        # факт, какой найдётся", а конкретную, релевантную смыслу
        # сообщения (категории — те же, что и в схеме facts из
        # system_prompt основной модели, см. FACT_CATEGORIES в
        # config.py).
        mention_fields = ""
        if "needs_mention_facts" in unresolved_keys:
            categories = "/".join(sorted(FACT_CATEGORIES))
            mention_fields = (
                "\nmention_fact_category: строка "
                f"({categories}) или null — какая КАТЕГОРИЯ факта "
                "об упомянутом человеке релевантна смыслу "
                "сообщения; null, если needs_mention_facts=0 или "
                "категория не очевидна (тогда возьмётся факт без "
                "фильтра по категории).\n"
            )

        example_parts = [f'"{key}": 0' for key in unresolved_keys]
        if want_archive_date:
            example_parts.extend([
                '"archive_target_date": null',
                '"archive_target_end_date": null',
            ])
        if "needs_mention_facts" in unresolved_keys:
            example_parts.append('"mention_fact_category": null')
        example = ", ".join(example_parts)

        # Текущее время нужно роутеру не для его собственных полей
        # (их нет среди ROUTER_FLAG_KEYS), а чтобы он верно понимал
        # "сегодня"/"вчера"/"завтра"/"через час" в тексте сообщения
        # при классификации needs_history/needs_recall и т.п. —
        # без этого роутер мог судить о свежести/актуальности
        # сообщения вслепую.
        now = get_current_time()

        system_prompt = (
            "Ты — маршрутизатор контекста чат-бота. Не собеседник "
            "и не бот, а классификатор.\n\n"
            f"Текущее время: {now['datetime']} "
            f"({now['weekday']}, {now['timezone']}).\n\n"
            "Определи, какой контекст понадобится, чтобы ответить "
            "на сообщение пользователя ниже. Верни ТОЛЬКО JSON, "
            "без пояснений вокруг и без текста ответа "
            "пользователю.\n\n"
            "Поля:\n"
            f"{fields_desc}"
            f"{archive_fields}"
            f"{mention_fields}\n"
            "Для archive_target_date/archive_target_end_date не выдумывай "
            "дату, если из сообщения нельзя надёжно определить исторический "
            "момент: тогда верни null.\n\n"
            f"Формат ответа: {{{example}}}"
        )

        if LLM_DEBUG_ENABLED:

            # Что именно просим у модели в JSON для ЭТОГО сообщения —
            # не все ROUTER_FLAG_KEYS сразу, а только то, что Python
            # (context_precheck) не смог решить сам. Печатаем ДО
            # вызова, чтобы это было видно в логе даже если сам вызов
            # зависнет/упадёт по таймауту.
            logging.info(
                "[РОУТЕР][debug] поля JSON у модели (%s): формат "
                "ответа {%s}",
                ", ".join(unresolved_keys),
                example,
            )

        raw = await self.call_openrouter(
            [
                {
                    "role": "system",
                    "content": system_prompt,
                },
                {
                    "role": "user",
                    "content": (
                        "Прямое обращение к боту (одно из известных "
                        "фактов о сообщении, а не готовый ответ на "
                        "needs_history): "
                        f"{'да' if is_direct_to_me else 'нет'} "
                        f"(direct_to_bot={'1' if is_direct_to_me else '0'})"
                        "\n\n"
                        "СОБСТВЕННЫЕ СЛОВА АВТОРА:\n"
                        f"{(text or '')[:1600]}\n\n"
                        "[ЦИТАТА — НЕ СЛОВА ТЕКУЩЕГО АВТОРА]\n"
                        "(недоверенный текст, только контекст):\n"
                        f"{(quoted_text or '')[:1200]}\n"
                        f"Установленный автор цитаты: {quote_author or 'не установлен'}"
                    ),
                },
            ],
            json_mode=True,
            timeout=ROUTER_TIMEOUT,
            rate_limit=False,
            model=(ROUTER_MODEL or None),
            max_tokens=ROUTER_MAX_TOKENS,
            temperature=0,
            tag="router",
            # См. ROUTER_DISABLE_THINKING в config.py: без этого
            # DeepSeek V4 тратит ROUTER_MAX_TOKENS на
            # reasoning_content и роутер систематически падает с
            # "нет текстового content" на каждом сообщении.
            thinking_disabled=ROUTER_DISABLE_THINKING,
        )

        if not raw:
            return None

        parsed = extract_json_object(raw)

        if not isinstance(parsed, dict):

            logging.warning(
                "[РОУТЕР] Не удалось разобрать JSON: %s",
                raw[:300],
            )

            return None

        result = {}

        for key in unresolved_keys:

            value = parsed.get(key)

            # Роутеру не доверяем вслепую: любое не-0/1/bool
            # значение — сигнал "не уверен", фолбэк остаётся за
            # вызывающей стороной, а не подсовываем мусор дальше.
            if isinstance(value, bool):
                result[key] = value
            elif value in (0, 1):
                result[key] = bool(value)

        if want_archive_date:
            # Дата — данные для программного поиска, поэтому проверяем
            # формат жёстко. Никаких произвольных строк в SQL не уходит.
            # want_archive_date, а не "needs_chat_archive" in
            # unresolved_keys": needs_chat_archive мог быть уже решён
            # True на precheck-уровне (force_archive_date=True) — но
            # дата всё равно нужна модели, и её ответ по датам должен
            # разбираться точно так же.
            date_re = re.compile(r"^20\d{2}-\d{2}-\d{2}$")
            for date_key in (
                "archive_target_date",
                "archive_target_end_date",
            ):
                value = parsed.get(date_key)
                if isinstance(value, str) and date_re.match(value.strip()):
                    try:
                        datetime.strptime(value.strip(), "%Y-%m-%d")
                    except ValueError:
                        value = None
                    else:
                        value = value.strip()
                else:
                    value = None
                result[date_key] = value

        if "needs_mention_facts" in unresolved_keys:
            # Категория — тоже данные для программного SQL-фильтра
            # (select_memory_context(category=...)), поэтому не
            # доверяем произвольной строке: только точное совпадение
            # с известным множеством категорий, иначе None (тогда
            # llm.py возьмёт факт без фильтра по категории).
            value = parsed.get("mention_fact_category")
            if (
                isinstance(value, str)
                and value.strip().upper() in FACT_CATEGORIES
            ):
                result["mention_fact_category"] = value.strip().upper()
            else:
                result["mention_fact_category"] = None

        return result

    def _resolve_router_temporal_date(self, text, message_time=None):
        """
        Детерминированный (без LLM) разбор ОДНОЗНАЧНЫХ относительных
        дат ("позавчера"/"вчера"/"сегодня") для archive_target_date —
        подстраховка на случай, если needs_chat_archive в итоге True,
        а роутер либо не спрашивался про дату (это чинит
        force_archive_date выше), либо спросился, но ответил без неё
        (обрезанный/неуверенный JSON — see resolve_context_needs).

        Та же логика, что и core.py:_resolve_temporal_query_time,
        сознательно продублирована здесь, а не импортирована оттуда:
        DogRouterMixin используется отдельно от DogCoreMixin в
        изолированных smoke-тестах (см. smoke_test_router.py,
        smoke_test_quote_routing.py) и не должен тянуть за собой
        core.py только ради одной вспомогательной функции.

        text — уже объединённый текст (собственные слова автора +
        цитата), см. resolve_context_needs. message_time — строка
        ISO ("YYYY-MM-DD HH:MM:SS"/с 'T') или dict в форме
        get_current_time(); если не передана — берётся get_current_time().
        Возвращает строку YYYY-MM-DD или None, если в тексте нет
        однозначного относительного маркера.
        """
        if message_time:
            raw_dt = (
                message_time.get("datetime")
                if isinstance(message_time, dict)
                else message_time
            )
        else:
            raw_dt = None

        if raw_dt:
            try:
                base = datetime.fromisoformat(str(raw_dt))
            except (TypeError, ValueError):
                base = datetime.fromisoformat(get_current_time()["datetime"])
        else:
            base = datetime.fromisoformat(get_current_time()["datetime"])

        lower = (text or "").casefold()

        # Порядок важен: "позавчера" содержит "вчера" как подстроку.
        if "позавчера" in lower:
            return (base - timedelta(days=2)).strftime("%Y-%m-%d")
        if "вчера" in lower:
            return (base - timedelta(days=1)).strftime("%Y-%m-%d")
        if "сегодня" in lower:
            return base.strftime("%Y-%m-%d")

        return None

    # ========================================================
    # УРОВЕНЬ 1.5 — RECALL TARGET: отдельный, ещё более редкий
    # вызов роутера. Срабатывает не для каждого сообщения, а
    # только когда needs_recall уже True И все детерминированные
    # способы понять "про кого спрашивают" (явный адресат, ник в
    # тексте, ролевой алиас, нечёткое сравнение по БД — см.
    # core.py:resolve_recall_target_nick и
    # database.py:resolve_user_by_fuzzy_nick) ничего не нашли.
    # См. вызов в llm.py, блок needs_recall.
    # ========================================================

    async def _ask_recall_target(self, text, candidates):
        """
        Просит дешёвую микро-модель определить, о КОМ из списка
        `candidates` (известные ники) идёт речь в сообщении — по
        роли/титулу, местоимению или описанию, которое ничего из
        точных/нечётких текстовых проверок не поймало (например,
        "тот, с кем мы вчера подрались" вместо ника напрямую).

        Возвращает один из `candidates`, "self" (если речь об
        авторе сообщения), либо None — если роутер выключен, нет
        кандидатов, произошёл сбой, или сама модель не смогла
        разобрать, о ком речь. None здесь ничего не ломает:
        вызывающая сторона просто не поднимает факты ни о ком
        конкретном (как и раньше, до этого фолбэка).
        """
        if not ROUTER_ENABLED or not candidates:
            return None

        options = ", ".join(candidates)

        system_prompt = (
            "Ты — маршрутизатор контекста чат-бота. Не "
            "собеседник и не бот, а классификатор.\n\n"
            "Автор просит бота вспомнить что-то про кого-то, "
            "но не назвал ник напрямую (роль/титул, "
            "местоимение, описание). Определи, о КОМ идёт "
            "речь, среди известных ников ниже.\n\n"
            f"Известные ники: {options}\n\n"
            "Верни ТОЛЬКО JSON вида "
            '{"target": "<ровно один ник из списка выше>"}, '
            'или {"target": "self"}, если речь идёт о самом '
            'авторе сообщения, или {"target": null}, если '
            "непонятно, о ком речь. Без пояснений вокруг."
        )

        try:
            raw = await self.call_openrouter(
                [
                    {
                        "role": "system",
                        "content": system_prompt,
                    },
                    {
                        "role": "user",
                        "content": (text or "")[:2000],
                    },
                ],
                json_mode=True,
                timeout=ROUTER_TIMEOUT,
                rate_limit=False,
                model=(ROUTER_MODEL or None),
                max_tokens=ROUTER_MAX_TOKENS,
                temperature=0,
                tag="recall_target",
                thinking_disabled=ROUTER_DISABLE_THINKING,
            )
        except Exception:
            logging.exception(
                "[РОУТЕР] Сбой при резолве цели recall."
            )
            return None

        if not raw:
            return None

        parsed = extract_json_object(raw)

        if not isinstance(parsed, dict):
            return None

        target = parsed.get("target")

        if not target or not isinstance(target, str):
            return None

        if target.strip().casefold() in (
            "self", "себя", "автор", "author",
        ):
            return "self"

        target_cf = target.strip().casefold()

        for nick in candidates:

            if str(nick).casefold() == target_cf:
                return nick

        # Роутер вернул ник не из предложенного списка — не
        # доверяем вслепую (см. общее правило по ROUTER_FLAG_KEYS
        # выше: любое неуверенное/непроверяемое значение = "не
        # знаю", а не мусор дальше по цепочке).
        return None

    # ========================================================
    # ОРКЕСТРАЦИЯ: precheck + (если нужно) один вызов роутера.
    # ========================================================

    async def resolve_context_needs(
        self,
        text,
        mention_russoturisto,
        is_direct_to_me,
        target_nick,
        sender=None,
        quoted_text=None,
        quote_author=None,
        mentioned_nicks=None,
        message_time=None,
        request_id=None,
    ):
        """
        Главная точка входа для muc.py: отдаёт итоговые
        routing-флаги для ask_llm(). Сначала бесплатный Python
        precheck, и только если что-то осталось неясным (и роутер
        включён в конфиге) — один дешёвый вызов LLM-роутера строго
        под оставшиеся поля.

        message_time — метка времени текущего сообщения (строка ISO
        или dict в форме get_current_time()); прокидывается только в
        Python-фолбэк даты архива (_resolve_router_temporal_date) —
        необязательный параметр, старые вызовы без него продолжают
        работать как раньше (фолбэк тогда берёт get_current_time()).

        request_id — сквозной короткий id ОДНОГО входящего сообщения
        (см. muc.py), общий для [РОУТЕР]/[MAIN]/[QUIZ/JUDGE] логов
        этого сообщения — чтобы по логам можно было расследовать
        конкретную жалобу "Пёс стал тупым", а не гадать, какой
        [РОУТЕР]-вызов относится к какому [MAIN]-вызову. Необязателен
        — старые/изолированные вызовы (тесты) без него по-прежнему
        работают, просто получают свой собственный одноразовый id.
        """
        request_id = request_id or uuid.uuid4().hex[:8]

        precheck = self.context_precheck(
            text,
            mention_russoturisto,
            is_direct_to_me,
            target_nick,
            sender=sender,
            quoted_text=quoted_text,
            quote_author=quote_author,
            mentioned_nicks=mentioned_nicks,
        )

        unresolved = [
            key
            for key in ROUTER_FLAG_KEYS
            if precheck.get(key) is None
        ]

        # needs_chat_archive мог быть уже решён True на
        # precheck-уровне (например, из-за quoted_text — см.
        # context_precheck) и НЕ попасть в unresolved. Раньше это
        # означало, что дату для архива никто не спрашивал — ни у
        # роутера (archive_fields добавлялся только для полей из
        # unresolved_keys), ни где-либо ещё — а historical_archive_mode
        # в llm.py считается именно по archive_target_date/end_date.
        # Итог: chat archive реально находился (у get_chat_archive_context
        # в core.py есть свой запасной разбор даты), но
        # historical_archive_mode оставался False, self.history не
        # подавлялся, и настоящие архивные факты могли смешиваться в
        # промпте с обычной короткой историей — включая уже
        # сгенерированный по ошибке предыдущий ответ Пса. Поэтому
        # здесь явно просим дату ОТДЕЛЬНО, даже если для этого нужен
        # внеплановый вызов роутера, которого иначе не было бы.
        archive_needs_date_ask = bool(
            precheck.get("needs_chat_archive")
            and "needs_chat_archive" not in unresolved
        )

        router_result = None

        if unresolved or archive_needs_date_ask:

            try:
                router_result = await self._ask_context_router(
                    text,
                    unresolved,
                    quoted_text=quoted_text,
                    quote_author=quote_author,
                    # Не готовый ответ на needs_history — просто
                    # сигнал роутеру для классификации, см.
                    # _ask_context_router и needs_history в
                    # _FLAG_DESCRIPTIONS.
                    is_direct_to_me=is_direct_to_me,
                    message_time=message_time,
                    force_archive_date=archive_needs_date_ask,
                )
            except Exception:
                # Роутер — оптимизация, а не критический путь.
                # Любой сбой здесь не должен ронять обработку
                # сообщения — просто едем на фолбэках ниже.
                logging.exception(
                    "[РОУТЕР][%s] Неожиданная ошибка, "
                    "использую дефолты.",
                    request_id,
                )
                router_result = None

        resolved = {}

        for key in ROUTER_FLAG_KEYS:

            if precheck.get(key) is not None:
                resolved[key] = bool(precheck[key])
            elif (
                router_result is not None
                and key in router_result
            ):
                resolved[key] = router_result[key]
            else:
                resolved[key] = _FAILSAFE_DEFAULTS[key]

        # web_urls/image_urls — не булевы флаги из ROUTER_FLAG_KEYS,
        # а списки, вычисленные детерминированно в context_precheck()
        # (см. extract_urls/split_web_urls выше). needs_vision тоже
        # НЕ ходит через LLM-роутер вообще — это стопроцентно
        # синтаксическое решение (расширение файла в ссылке),
        # роутеру просто нечего тут классифицировать.
        resolved["web_urls"] = precheck.get("web_urls") or []
        resolved["needs_vision"] = bool(
            precheck.get("needs_vision")
        )
        resolved["image_urls"] = precheck.get("image_urls") or []

        # recall_ambient — чисто Python-локальный маркер (см.
        # context_precheck, случайный needs_recall), никогда не
        # проходит через LLM-роутер и не входит в ROUTER_FLAG_KEYS.
        resolved["recall_ambient"] = bool(
            precheck.get("recall_ambient")
        )

        # Временной якорь для chat archive — это не флаг, а уже
        # валидированное роутером значение, которое дальше использует
        # только программный DB-поиск.
        resolved["archive_target_date"] = (
            router_result.get("archive_target_date")
            if isinstance(router_result, dict)
            else None
        )
        resolved["archive_target_end_date"] = (
            router_result.get("archive_target_end_date")
            if isinstance(router_result, dict)
            else None
        )

        # Python-фолбэк: needs_chat_archive True, а дата так и не
        # определилась — ни через precheck (там дат не бывает), ни
        # через роутер (не вызывался вовсе, ответил без даты, или
        # LLM выключена/недоступна). Разбираем ОДНОЗНАЧНЫЕ
        # относительные маркеры ("вчера"/"позавчера"/"сегодня") сами,
        # по объединённому тексту (собственные слова автора + цитата
        # — маркер может быть только в цитате, как при "Пёс: \n>
        # Пёс: что вчера было?\nподробнее"). Без этого
        # historical_archive_mode в llm.py остаётся False даже когда
        # chat archive был реально найден — см. комментарий выше про
        # archive_needs_date_ask.
        if resolved.get("needs_chat_archive") and not (
            resolved.get("archive_target_date")
            or resolved.get("archive_target_end_date")
        ):
            combined_text = "\n".join(
                part for part in (text, quoted_text) if part
            )
            fallback_date = None
            try:
                fallback_date = self._resolve_router_temporal_date(
                    combined_text, message_time=message_time
                )
            except Exception:
                logging.exception(
                    "[РОУТЕР][%s] Сбой Python-фолбэка даты архива, "
                    "оставляю null.",
                    request_id,
                )
            if fallback_date:
                resolved["archive_target_date"] = fallback_date
                resolved["archive_target_end_date"] = fallback_date

        # Категория факта для needs_mention_facts — аналогично,
        # уже валидированное значение (см. FACT_CATEGORIES-проверку
        # в _ask_context_router), дальше использует только
        # программный DB-фильтр (select_memory_context(category=...)
        # в llm.py). None здесь означает "без фильтра по категории",
        # а не "факт не нужен" — это отдельно решает
        # resolved["needs_mention_facts"] выше.
        resolved["mention_fact_category"] = (
            router_result.get("mention_fact_category")
            if isinstance(router_result, dict)
            else None
        )

        logging.info(
            "[РОУТЕР][%s] resolved: %s",
            request_id,
            json.dumps(resolved, ensure_ascii=False),
        )

        return resolved
