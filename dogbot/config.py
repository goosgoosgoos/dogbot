import asyncio
import difflib
import hmac
import json
import logging
import math
import os
import random
import re
import shlex
import time
import uuid
from collections import deque, Counter
from datetime import datetime, timedelta, timezone

import aiohttp
import aiosqlite
import slixmpp
from aiohttp_socks import ProxyConnector


# ============================================================
# CONFIG / LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-7s] %(message)s",
)

OPENROUTER_URL = "https://api.deepseek.com/chat/completions"

TOR_PROXY = os.getenv(
    "DOG_BOT_PROXY",
    "socks5://127.0.0.1:9050",
)

DB_PATH = os.getenv(
    "DOG_BOT_DB",
    "dog_bot.db",
)

JID = os.getenv(
    "DOG_BOT_JID",
    "dog@jajaba.ru",
)

PASSWORD = os.getenv(
    "DOG_BOT_PASSWORD",
    "",
)

ROOM = os.getenv(
    "DOG_BOT_ROOM",
    "ru@chat.404.city",
)

NICK = os.getenv(
    "DOG_BOT_NICK",
    "Пёс",
)

OPENROUTER_KEY = os.getenv(
    "OPENROUTER_KEY",
    "",
)

OPENROUTER_MODEL = os.getenv(
    "OPENROUTER_MODEL",
    "deepseek-v4-flash",
)

# ------------------------------------------------------------
# MUC PRESENCE HISTORY
# ------------------------------------------------------------
# Снимок участников комнаты каждые 5 минут; в БД остаются последние 100.
PRESENCE_SNAPSHOT_INTERVAL_SECONDS = int(
    os.getenv("DOG_BOT_PRESENCE_SNAPSHOT_INTERVAL", "300")
)
PRESENCE_SNAPSHOT_INITIAL_DELAY_SECONDS = int(
    os.getenv("DOG_BOT_PRESENCE_SNAPSHOT_INITIAL_DELAY", "5")
)
PRESENCE_SNAPSHOT_RETRY_SECONDS = int(
    os.getenv("DOG_BOT_PRESENCE_SNAPSHOT_RETRY", "30")
)


# ============================================================
# SHIP CREW / MARITIME POSITIONS
# ============================================================
# Чем выше число, тем выше должность в командной иерархии.
# Должности можно назначать/снимать зарегистрированным членам экипажа;
# командование нижнего ранга не может менять должность равного/старшего.
SHIP_POSITIONS = {
    "капитан": {"level": 100, "aliases": ["капитан", "master", "шкипер", "кэп"]},
    "старший помощник капитана": {"level": 90, "aliases": ["старший помощник", "старпом", "первый помощник", "chief officer", "chief mate"]},
    "старший механик": {"level": 85, "aliases": ["старший механик", "главный механик", "chief engineer"]},
    "второй помощник капитана": {"level": 80, "aliases": ["второй помощник", "второй офицер", "2-й помощник", "second officer", "second mate"]},
    "навигатор": {"level": 75, "aliases": ["навигатор", "штурман", "navigator"]},
    "третий помощник капитана": {"level": 70, "aliases": ["третий помощник", "третий офицер", "3-й помощник", "third officer", "third mate"]},
    "канонир": {"level": 65, "aliases": ["канонир", "артиллерийский офицер", "gunner"]},
    "боцман": {"level": 60, "aliases": ["боцман", "boatswain", "bosun"]},
    "судовой врач": {"level": 58, "aliases": ["судовой врач", "доктор", "врач", "ship's doctor", "surgeon"]},
    "старший повар": {"level": 55, "aliases": ["старший повар", "шеф-повар", "chief cook"]},
    "кок": {"level": 50, "aliases": ["кок", "повар", "cook"]},
    "морпех": {"level": 40, "aliases": ["морпех", "морской пехотинец", "боец", "marine"]},
    "матрос": {"level": 30, "aliases": ["матрос", "able seaman", "ab"]},
    "юнга": {"level": 10, "aliases": ["юнга", "кадет", "ordinary seaman", "os"]},
}

SHIP_POSITION_ALIASES = {}
for _position_name, _position_meta in SHIP_POSITIONS.items():
    for _alias in _position_meta["aliases"]:
        SHIP_POSITION_ALIASES[_alias.casefold()] = _position_name

ROLE_CAPTAIN = "captain"
ROLE_FIRST_OFFICER = "first_officer"
ROLE_NAVIGATOR = "navigator"
ROLE_BOATSWAIN = "boatswain"
ROLE_GUNNER = "gunner"
ROLE_DOCTOR = "doctor"
ROLE_COOK = "cook"
ROLE_MARINE = "marine"
ROLE_SAILOR = "sailor"
ROLE_CREW = "crew"

SHIP_POSITION_TO_ROLE = {
    "капитан": ROLE_CAPTAIN,
    "старший помощник капитана": ROLE_FIRST_OFFICER,
    "старший механик": ROLE_FIRST_OFFICER,
    "второй помощник капитана": ROLE_FIRST_OFFICER,
    "третий помощник капитана": ROLE_FIRST_OFFICER,
    "навигатор": ROLE_NAVIGATOR,
    "боцман": ROLE_BOATSWAIN,
    "канонир": ROLE_GUNNER,
    "судовой врач": ROLE_DOCTOR,
    "старший повар": ROLE_COOK,
    "кок": ROLE_COOK,
    "морпех": ROLE_MARINE,
    "матрос": ROLE_SAILOR,
    "юнга": ROLE_CREW,
}

# Зарегистрированные пользователи без назначенной должности считаются
# членами основного экипажа без ранга; незарегистрированные участники
# конференции отображаются LLM как вспомогательный экипаж.
SHIP_AUXILIARY_POSITION = "вспомогательный экипаж"



# ============================================================
# OUTPUT TOKEN CAPS
# ============================================================
#
# Раньше ни один вызов (кроме роутера) не ограничивал max_tokens
# вообще — цена одного ответа была не ограничена сверху. Значения
# ниже — не "300 и порезать", как для роутера: основной ответ
# отдаёт JSON, где body (реплика), facts[] (может быть несколько
# фактов) и особенно create_lot.full_info (модель обязана
# процитировать реально раскрытый секрет, а он может быть длинным)
# —  если max_tokens обрежет ответ до валидного конца JSON,
# extract_json_object() не сможет разобрать НИЧЕГО (ни body, ни
# facts) — обрубленный JSON роняет весь ответ целиком, а не только
# "лишний хвост". Поэтому бюджет ниже — это защита от аномального
# разгона (модель зациклилась/отвечает эссе), а не жёсткий лимит
# длины обычного ответа.
MAIN_MAX_TOKENS = int(
    os.getenv("DOG_BOT_MAIN_MAX_TOKENS", "800")
)

# Repair-проход для сломанного JSON основного ответа (см.
# llm.py:_repair_main_json) — вызывается редко (только когда основной
# ответ не распарсился), поэтому бюджет и таймаут отдельные и
# заметно скромнее, чем у самого основного вызова: тут не нужно
# писать что-то новое, только переупаковать уже сказанное в валидный
# JSON.
MAIN_REPAIR_MAX_TOKENS = int(
    os.getenv("DOG_BOT_MAIN_REPAIR_MAX_TOKENS", "500")
)
MAIN_REPAIR_TIMEOUT = float(
    os.getenv("DOG_BOT_MAIN_REPAIR_TIMEOUT", "15")
)

# Кличка + короткая причина (сама кличка и так обрезается в Python
# до NICKNAME_MAX_LEN, но лучше не платить за размышления модели
# до этой обрезки).
NICKNAME_MAX_TOKENS = int(
    os.getenv("DOG_BOT_NICKNAME_MAX_TOKENS", "200")
)

# Один вопрос викторины + короткий массив ответов-синонимов.
QUIZ_MAX_TOKENS = int(
    os.getenv("DOG_BOT_QUIZ_MAX_TOKENS", "200")
)


# ============================================================
# ECONOMY CONFIG
# ============================================================

# Начальный резерв банка.
INITIAL_BANK_BALANCE = 100000.0

# Инфляция никогда не опускается ниже x1.
MIN_INFLATION = 1.0

# Жёсткий потолок инфляции.
MAX_INFLATION = 3.0

# Базовая инфляция за час.
BASE_INFLATION_PER_HOUR = 0.005

# Насколько денежная масса влияет на инфляцию.
# Значение небольшое, чтобы экономика не улетала мгновенно.
MONEY_SUPPLY_INFLATION_FACTOR = 0.0000008

# Целевой объём денег в обращении.
TARGET_MONEY_SUPPLY = 250000.0

# Максимальное снижение инфляции за час.
MAX_DEFLATION_PER_HOUR = 0.002

# Инвестор получает 150% номинала при успешном выкупе.
INVESTOR_RETURN_MULTIPLIER = 1.50

# Комиссия при досрочной продаже инвестиции.
INVESTMENT_EXIT_FEE = 0.05

# Минимальная инвестиция.
MIN_INVESTMENT = 100.0

# Максимальная инвестиция за одну операцию.
MAX_INVESTMENT = 1_000_000.0


# ============================================================
# ОПЬЯНЕНИЕ ПСА (!напоить)
# ============================================================

# Базовая цена одной стадии (умножается на инфляцию, как и
# остальные цены в магазине).
DRUNK_STAGE_COST = 250.0

# Сколько стадий всего — 5-я это "в сопли" (максимум).
MAX_DRUNK_LEVEL = 5

# Пёс трезвеет на 1 стадию каждые столько часов (фоновый
# цикл — см. drunk_decay_loop в core.py).
DRUNK_DECAY_HOURS = 2
DRUNK_DECAY_INTERVAL_SECONDS = DRUNK_DECAY_HOURS * 3600

# Реплика бота при достижении каждой стадии опьянения.
# Ключ — новая стадия (1..MAX_DRUNK_LEVEL) после !напоить.
DRUNK_STAGE_REPLIES = {
    1: (
        "Стопка зашла легко — Пёс явно "
        "повеселел, дурачится чуть "
        "больше обычного."
    ),
    2: (
        "После второй Пёс подшофе: чаще "
        "сбивается с мысли, характерное "
        "«ик» проскакивает в тексте."
    ),
    3: (
        "Третья доза — Пёс изрядно "
        "окосел, язык заплетается, "
        "мысли скачут с темы на тему."
    ),
    4: (
        "Четвёртая — Пёс еле держится "
        "на лапах, порядок слов в "
        "голове окончательно сбился."
    ),
    5: (
        "Пятая — всё, приплыли: Пёс "
        "«в сопли», лыка не вяжет."
    ),
}

# Инструкция для system_prompt LLM по каждой стадии — влияет
# ТОЛЬКО на манеру речи/связность, не на факты (см. дальше).
DRUNK_STAGE_PROMPT = {
    1: (
        "Ты слегка навеселе: чуть более "
        "раскован и болтливее обычного, "
        "изредка путаешь слова, но в "
        "целом собран."
    ),
    2: (
        "Ты подшофе: речь менее "
        "собранная, чаще перескакиваешь "
        "с мысли на мысль, изредка "
        "вставляешь «ик», возможны "
        "мелкие оговорки."
    ),
    3: (
        "Ты прилично пьян: язык "
        "заплетается, есть повторы "
        "слов и оборванные фразы, "
        "тяжелее удерживать нить "
        "разговора."
    ),
    4: (
        "Ты сильно пьян: почти не "
        "можешь сосредоточиться, часто "
        "теряешь мысль на середине "
        "фразы, порядок слов может "
        "сбиваться, тема может "
        "внезапно меняться."
    ),
    5: (
        "Ты пьян «в сопли»: речь почти "
        "бессвязная, заикание, повторы "
        "слов/слогов, можешь обрывать "
        "мысль на полуслове — но "
        "отвечай всё равно по-русски, "
        "и поле body не может быть "
        "пустым."
    ),
}


# ============================================================
# LLM RATE LIMIT CONFIG
# ============================================================

# Обычные LLM-запросы от одного пользователя.
LLM_USER_LIMIT = 12
LLM_USER_WINDOW = 60

# Глобальный лимит LLM.
LLM_GLOBAL_LIMIT = 30
LLM_GLOBAL_WINDOW = 60

# Отдельный лимит генерации викторин.
QUIZ_USER_LIMIT = 2
QUIZ_USER_WINDOW = 300

# Сколько запросов одновременно может идти к OpenRouter.
LLM_CONCURRENCY = 3

# ============================================================
# LOAD DEGRADATION (защита от "бесплатной техподдержки")
# ============================================================
#
# Отдельно от rate-limiter'а (который просто режет частоту
# запросов): здесь копится ПЛАВНЫЙ, непрерывный "уровень нагрузки"
# 0..1 на пользователя — растёт от сообщений, похожих на просьбу
# решить рабочую/техническую задачу (или просто от частого
# дёрганья LLM), и сам по себе экспоненциально спадает обратно к
# нулю, если человек отстал. И рост, и спад — плавные (см.
# LoadDegradationTracker в rate_limiters.py), без резких скачков
# "запрещено/разрешено". Текущий уровень транслируется в системный
# промпт как "терпение" — LLM сама постепенно закручивает гайки
# (короче/суше/без конкретики), а на самом верху бот вообще
# перестаёт звать LLM и отвечает заготовленной фразой.

# За сколько секунд накопленный уровень нагрузки спадает вдвое
# при отсутствии новых релевантных сообщений от пользователя.
LOAD_HALF_LIFE_SECONDS = float(
    os.getenv("DOG_BOT_LOAD_HALF_LIFE", "900")
)

# Базовый шаг роста уровня за одно релевантное сообщение (до
# насыщения — см. LoadDegradationTracker.bump).
LOAD_INCREMENT_STEP = float(
    os.getenv("DOG_BOT_LOAD_INCREMENT_STEP", "0.22")
)

# Вес обычного сообщения, вызвавшего LLM, но НЕ похожего на
# просьбу порешать рабочую/техническую задачу (обычный трёп) —
# заметно меньше 1, чтобы обычное общение почти не грузило.
LOAD_CHAT_WEIGHT = float(
    os.getenv("DOG_BOT_LOAD_CHAT_WEIGHT", "0.25")
)

# Пороги уровня (0..1), на которых меняется поведение:
#   < LOAD_TIER_ANNOYED        — как обычно;
#   >= LOAD_TIER_ANNOYED        — короче, суше, с раздражением,
#                                  но ещё реально помогает;
#   >= LOAD_TIER_COLD           — почти не помогает по делу,
#                                  явно отфутболивает;
#   >= LOAD_TIER_REFUSE         — LLM вообще не вызывается,
#                                  заготовленный отказ.
LOAD_TIER_ANNOYED = float(
    os.getenv("DOG_BOT_LOAD_TIER_ANNOYED", "0.35")
)

LOAD_TIER_COLD = float(
    os.getenv("DOG_BOT_LOAD_TIER_COLD", "0.65")
)

LOAD_TIER_REFUSE = float(
    os.getenv("DOG_BOT_LOAD_TIER_REFUSE", "0.88")
)

# Готовые фразы-отказы, когда уровень нагрузки на пользователе
# достиг LOAD_TIER_REFUSE — выбираются случайно, LLM не вызывается
# вообще (это ещё и экономит реальные запросы к провайдеру).
LOAD_REFUSAL_REPLIES = (
    "Харе. Я тебе не бесплатный саппорт на аутсорсе, "
    "сам своей инфраструктурой занимайся.",
    "Опять рабочие проблемы? Всё, лавочка закрыта. "
    "Гугли, читай доки, разбирайся сам.",
    "Не, сегодня я тебе больше ничего не чиню. "
    "Возвращайся, когда я остыну.",
    "Хватит меня грузить рабочими задачами задаром. "
    "Иди страдай самостоятельно.",
    "Я тебе не консультант по подписке. На сегодня — всё, "
    "дальше сам.",
)

# Грубая эвристика (по подстрокам, без учёта словоформ):
# похоже ли сообщение на просьбу решить рабочую/техническую
# задачу — код, конфиги, сисадминство, отладка и т.п. Не
# претендует на точность NLP-классификатора: ложные срабатывания
# сглаживаются decay/насыщением в LoadDegradationTracker, а не
# режут конкретное сообщение резко.
LOAD_WORK_REQUEST_MARKERS = (
    "ошибк", "не работает", "не запуска", "почему не",
    "как настро", "как сделать", "как исправ", "как поднят",
    "как подключ", "напиши код", "напиши прог", "напиши скрипт",
    "напиши функц", "исправь", "почини", "отладь", "задеплой",
    "разверни", "настрой", "конфиг", "траблш", "переустанов",
    "error", "exception", "traceback", "stack trace",
    "not working", "doesn't work", "permission denied",
    "sudo ", "systemctl", "docker", "iptables", "nginx",
    "apache", "reverse proxy", "deploy", "compile", "debug",
    "ssh ", "bridge", "virsh", "config",
)

# Длинное сообщение с вопросом — тоже слабый сигнал "объясни мне/
# реши мне вот это", даже без явных технических слов.
LOAD_WORK_REQUEST_LENGTH_THRESHOLD = 220


def looks_like_work_request(text):
    """
    True, если сообщение похоже на просьбу порешать рабочую/
    техническую задачу (см. LOAD_WORK_REQUEST_MARKERS выше).
    """
    if not text:
        return False

    if "`" in text:
        return True

    lower = text.casefold()

    if any(
        marker in lower
        for marker in LOAD_WORK_REQUEST_MARKERS
    ):
        return True

    if (
        len(text) >= LOAD_WORK_REQUEST_LENGTH_THRESHOLD
        and "?" in text
    ):
        return True

    return False


# Максимальная длина истории в памяти — сколько последних строк
# "ник: текст" из self.history попадает в system_prompt при
# needs_history=True (см. llm.py). Раньше было жёстко 8: в активном
# многолюдном чате это меньше минуты реального времени, и бот терял
# нить разговора даже когда роутер верно решил, что история вообще
# нужна. Это чистый прирост токенов в НЕкэшируемом хвосте промпта
# (после dossier/context_note), на кэш-префикс из llm.py не влияет.
# Подними DOG_BOT_HISTORY_SIZE ещё выше, если бот по-прежнему "не
# помнит" недавний разговор. Поднято с 24 до 32 (умеренный шаг, как
# и предлагалось: не "засунуть 200 сообщений" вслепую, а сначала
# смотреть на реальный prompt_chars/history_chars из лога [MAIN] —
# см. llm.py — и уже по факту решать, нужно ли больше).
HISTORY_SIZE = int(
    os.getenv("DOG_BOT_HISTORY_SIZE", "32")
)

# Сколько последних сообщений комнаты (от ЛЮБОГО ника, включая
# незарегистрированных/несогласившихся) держим в памяти отдельно
# от HISTORY_SIZE — исключительно для того, чтобы уметь определить
# автора цитаты ("> текст"). В LLM как разговорный контекст не
# идёт (в отличие от self.history) — используется только точечно,
# когда кто-то реально что-то процитировал. См. muc.py/core.py.
QUOTE_LOOKBACK_SIZE = 60

# ------------------------------------------------------------
# SHORT-TERM CHAT ARCHIVE (14 дней)
# ------------------------------------------------------------
# Полный технический журнал MUC для точечного исторического контекста.
# В отличие от self.history, архив не является prompt-дампом: роутер
# сначала решает, нужен ли он, а БД возвращает только релевантный кусок.
CHAT_ARCHIVE_ENABLED = os.getenv(
    "DOG_BOT_CHAT_ARCHIVE_ENABLED", "1"
) == "1"
CHAT_ARCHIVE_RETENTION_DAYS = int(
    os.getenv("DOG_BOT_CHAT_ARCHIVE_RETENTION_DAYS", "14")
)
CHAT_ARCHIVE_NEIGHBOR_DAYS = int(
    os.getenv("DOG_BOT_CHAT_ARCHIVE_NEIGHBOR_DAYS", "1")
)
CHAT_ARCHIVE_MAX_MESSAGES = int(
    os.getenv("DOG_BOT_CHAT_ARCHIVE_MAX_MESSAGES", "40")
)
CHAT_ARCHIVE_MAX_MATCHES = int(
    os.getenv("DOG_BOT_CHAT_ARCHIVE_MAX_MATCHES", "20")
)
# При старте XEP-0045 запрашивает серверную MUC-историю за retention.
# Отдельный флаг позволяет аварийно отключить именно импорт при старте,
# не выключая запись новых сообщений.
CHAT_ARCHIVE_IMPORT_ON_START = os.getenv(
    "DOG_BOT_CHAT_ARCHIVE_IMPORT_ON_START", "1"
).strip().lower() not in ("0", "false", "no", "off")

# XEP-0313 Message Archive Management: второй источник восстановления
# истории после рестарта. MUC history остаётся первым источником.
CHAT_ARCHIVE_MAM_ENABLED = os.getenv(
    "DOG_BOT_CHAT_ARCHIVE_MAM_ENABLED", "1"
).strip().lower() not in ("0", "false", "no", "off")
CHAT_ARCHIVE_MAM_PAGE_SIZE = int(os.getenv(
    "DOG_BOT_CHAT_ARCHIVE_MAM_PAGE_SIZE", "100"
))
CHAT_ARCHIVE_MAM_TIMEOUT = int(os.getenv(
    "DOG_BOT_CHAT_ARCHIVE_MAM_TIMEOUT", "30"
))

# ------------------------------------------------------------
# NEEDS_HISTORY ИЗ CHAT ARCHIVE (постепенный перевод self.history)
# ------------------------------------------------------------
# self.history — плоский deque последних HISTORY_SIZE строк комнаты
# в памяти, без разбора "от кого кому". chat_archive пишется тем же
# вызовом, что и self.history.append (см. muc.py) и уже умеет
# прицельный поиск (search_chat_archive), но заточен под нишевый
# needs_chat_archive с явной исторической датой.
#
# needs_history — САМЫЙ частый флаг (обычное продолжение разговора
# почти на каждом сообщении), поэтому в отличие от needs_chat_archive/
# needs_mention_facts это КРИТИЧНЫЙ путь: ошибка здесь бьёт по
# основному чату, а не по нишевому блоку. Поэтому:
#   1) self.history НЕ убирается — остаётся дешёвым (память, без SQL)
#      фолбэком на любой сбой/выключенный архив (см.
#      core.py:get_recent_history_context);
#   2) переход на архив включается только явным флагом и только для
#      ЧАСТИ сообщений (см. ROLLOUT_PERCENT) — не replace-in-place.
#
# Мастер-выключатель. По умолчанию выключено: needs_history
# продолжает работать ровно как до этого рефакторинга.
HISTORY_ARCHIVE_ENABLED = os.getenv(
    "DOG_BOT_HISTORY_ARCHIVE_ENABLED", "0"
).strip().lower() not in ("0", "false", "no", "off")

# Доля сообщений (0-100), для которых при HISTORY_ARCHIVE_ENABLED=1
# needs_history реально уходит в archive-поиск вместо self.history.
# Остальные — штатный дешёвый путь. Так нагрузку на БД от нового кода
# можно поднимать постепенно (0 -> 5 -> 25 -> 100), проверяя реальный
# пик активности чата, вместо разового переключения на 100% трафика.
HISTORY_ARCHIVE_ROLLOUT_PERCENT = min(max(int(
    os.getenv("DOG_BOT_HISTORY_ARCHIVE_ROLLOUT_PERCENT", "0")
), 0), 100)

# "Недавнее" для archive-поиска needs_history — это окно по РЕАЛЬНОМУ
# времени (минуты), а не фиксированное число последних реплик, как у
# self.history/HISTORY_SIZE: archive ищет по created_at, а не хранит
# кольцевой буфер в памяти. У search_chat_archive (needs_chat_archive)
# гранулярность дневная (target_date) — для "последних N минут" она
# не подходит, поэтому используется отдельная функция БД
# (database.py:search_recent_chat_archive).
HISTORY_ARCHIVE_RECENCY_MINUTES = int(
    os.getenv("DOG_BOT_HISTORY_ARCHIVE_RECENCY_MINUTES", "45")
)

# Верхняя граница строк на один archive-запрос needs_history.
# Независимый от CHAT_ARCHIVE_MAX_MESSAGES лимит (тот — для
# needs_chat_archive, где оправдан более крупный фрагмент); здесь
# запрос — прямая замена self.history, поэтому дефолт равен
# HISTORY_SIZE, чтобы не раздувать промпт при простом включении флага.
HISTORY_ARCHIVE_MAX_MESSAGES = int(
    os.getenv("DOG_BOT_HISTORY_ARCHIVE_MAX_MESSAGES", str(HISTORY_SIZE))
)


# ============================================================
# MUC ANTI-RAID / ANTI-BOT MODERATION
# ============================================================
#
# Работает только когда Пёс сам имеет moderator/admin/owner права
# в комнате. По умолчанию включено.
MODERATION_ENABLED = (
    os.getenv("DOG_BOT_MODERATION_ENABLED", "true")
    .strip()
    .lower()
    not in ("0", "false", "no", "off")
)

# ------------------------------------------------------------
# ЕДИНАЯ 4-УРОВНЕВАЯ ШКАЛА SCORE (заменяет старую пару
# MODERATION_HUMAN_SCORE/MODERATION_BOT_SCORE с двумя параллельными
# if-ветками "человек или бот"). Один и тот же числовой score
# теперь определяет уровень реакции монотонно:
#
#   score <  WATCH                -> ничего
#   WATCH   <= score < RESTRICT   -> подозрение (только лог)
#   RESTRICT<= score < DELETE     -> временное ограничение
#                                     (удаление текущего сообщения +
#                                     более частые LLM-проверки)
#   DELETE  <= score < BAN        -> удаление недавних сообщений
#                                     этого ключа, без постоянной фиксации
#   score >= BAN                  -> удаление + постоянная фиксация
#                                     как рейдера (см. MODERATION_BAN_*
#                                     ниже про дополнительный
#                                     структурный гейт)
#
# Старые переменные MODERATION_HUMAN_SCORE/MODERATION_BOT_SCORE/
# MODERATION_BOT_SIGNALS больше не читаются кодом — оставлены как
# no-op ниже только чтобы существующие DOG_BOT_MODERATION_HUMAN_SCORE/
# DOG_BOT_MODERATION_BOT_SCORE в окружении не падали с ошибкой при
# импорте, если кто-то их всё ещё экспортирует.
os.getenv("DOG_BOT_MODERATION_HUMAN_SCORE", "5")
os.getenv("DOG_BOT_MODERATION_BOT_SCORE", "6")
os.getenv("DOG_BOT_MODERATION_BOT_SIGNALS", "3")

MODERATION_SCORE_WATCH = int(
    os.getenv("DOG_BOT_MODERATION_SCORE_WATCH", "4")
)
MODERATION_SCORE_RESTRICT = int(
    os.getenv("DOG_BOT_MODERATION_SCORE_RESTRICT", "6")
)
MODERATION_SCORE_DELETE = int(
    os.getenv("DOG_BOT_MODERATION_SCORE_DELETE", "8")
)
MODERATION_SCORE_BAN = int(
    os.getenv("DOG_BOT_MODERATION_SCORE_BAN", "10")
)

# ------------------------------------------------------------
# СТРУКТУРНЫЙ ГЕЙТ ДЛЯ ПОСТОЯННОГО БАНА (score>=BAN один сам по
# себе НЕ фиксирует человека навсегда — нужно ЛИБО устойчивое
# число сообщений в окне, ЛИБО явный bot-identity-hint в нике,
# ЛИБО подтверждение LLM). Это сохраняет ключевое свойство исходной
# реализации: короткий, но очень интенсивный всплеск от живого
# человека (score может легко долететь до 10 на 4-5 сообщениях)
# никогда не должен приводить к необратимому бану в одиночку —
# см. подробное обоснование в сопроводительном анализе.
MODERATION_BAN_SUSTAINED_EVENTS = int(
    os.getenv("DOG_BOT_MODERATION_BAN_SUSTAINED_EVENTS", "8")
)
MODERATION_BAN_IDENTITY_SIGNALS = int(
    os.getenv("DOG_BOT_MODERATION_BAN_IDENTITY_SIGNALS", "2")
)

MODERATION_WINDOW_SECONDS = int(
    os.getenv("DOG_BOT_MODERATION_WINDOW", "30")
)

# ------------------------------------------------------------
# ОТДЕЛЬНОЕ (более широкое) окно для сигналов ПОВТОРА КОНТЕНТА
# (duplicate_flood/near_duplicate_flood/low_vocabulary/
# char_obfuscation_flood — см. _mod_score в moderation.py).
#
# recent_5/15/30 (см. MODERATION_WINDOW_SECONDS) — это сигналы
# ЧАСТОТЫ, им нужно короткое окно, чтобы отличать "быстро" от
# "медленно". Но повтор/похожесть СОДЕРЖИМОГО — другая ось: рейдер,
# который намеренно выдерживает паузы 10-30с между сообщениями
# специально для того, чтобы не попасть в burst-окно, всё равно
# явно репостит один и тот же (пусть и обфусцированный) текст — и
# в 15-секундное окно предыдущее его сообщение просто не попадает
# физически, сколько бы сообщений он ни прислал. Более широкое окно
# не увеличивает чувствительность к скорости, оно возвращает
# возможность вообще СРАВНИТЬ сообщения между собой при медленном,
# но упорном темпе.
#
# По умолчанию равно полному горизонту хранения events
# (max(MODERATION_WINDOW_SECONDS, MODERATION_LLM_CONTEXT_WINDOW_SECONDS),
# сейчас это 180с) НЕ СЛУЧАЙНО: раньше здесь стояло 60с, и на реальном
# рейде с паузами по 10-30с и одной паузой почти в 6 минут этого не
# хватило — часть повторов "выпадала" из окна раньше, чем накапливалось
# достаточно сообщений для сравнения (нужно >=3-5 сообщений В ОДНОМ
# окне). Смысла делать это окно короче горизонта хранения events всё
# равно нет: события старше горизонта хранения физически удаляются из
# mod_events и сравнивать их не с чем, так что "экономия" на более
# узком окне только теряет часть уже имеющихся данных, не снижая
# нагрузку.
MODERATION_CONTENT_WINDOW_SECONDS = int(
    os.getenv("DOG_BOT_MODERATION_CONTENT_WINDOW", "180")
)

# ------------------------------------------------------------
# "ВРЕМЕННОЕ ОГРАНИЧЕНИЕ" (RESTRICT-уровень). Явного XMPP-API для
# временного мьюта (role=visitor) в кодовой базе сейчас нет —
# добавлять его без проверки на реальном MUC-сервере рискованно.
# Поэтому RESTRICT реализован через существующие примитивы:
# удаление текущего сообщения + разовое предупреждение (как раньше
# у "human"-ветки) + "чувствительное окно": пока оно активно,
# LLM-арбитр вызывается СРАЗУ в серой зоне, а не по кадансу
# MODERATION_LLM_EVERY_MESSAGES — то есть реальное отличие от
# WATCH в более быстром и внимательном контроле, а не в тихом
# массовом удалении будущих сообщений (это было бы рискованно для
# человека, который уже остановился после предупреждения).
MODERATION_RESTRICT_SECONDS = float(
    os.getenv("DOG_BOT_MODERATION_RESTRICT_SECONDS", "300")
)

# ------------------------------------------------------------
# "ТРИ ПРЕДУПРЕЖДЕНИЯ": если один и тот же участник несколько раз
# подряд попадает на уровень restrict в течение ограниченного окна —
# это НЕЗАВИСИМАЯ от score/LLM улика упорства. Нужна для случая,
# когда участник балансирует прямо на грани серой зоны, а LLM
# (обоснованно осторожно) раз за разом не даёт уверенного bot=true
# ни по одному отдельному сообщению — но сам факт повторных попаданий
# в restrict за получас эвристики просто не могут списать на
# случайность. Считается только для уровня restrict (см.
# _mod_ban_eligible и moderate_incoming_muc).
#
# Окно — скользящее: если между двумя restrict прошло больше него,
# счётчик обнуляется, а не копится бесконечно (иначе редкие вспышки
# раздражения у обычного активного участника когда-нибудь тоже
# наберут 3 штрафа за месяцы использования чата).
MODERATION_RESTRICT_STRIKES_TO_ESCALATE = int(
    os.getenv("DOG_BOT_MODERATION_RESTRICT_STRIKES", "3")
)
MODERATION_RESTRICT_STRIKE_WINDOW_SECONDS = float(
    os.getenv("DOG_BOT_MODERATION_RESTRICT_STRIKE_WINDOW", "1800")
)

# ------------------------------------------------------------
# ИСПОЛЬЗОВАНИЕ CHAT_ARCHIVE ДЛЯ TENURE (стаж участника в комнате).
# Позволяет отличать давнего участника от внезапного рейдера
# (см. п.9-11 ТЗ) и снижать ложные срабатывания на активных
# старожилах.
MODERATION_TENURE_TRUSTED_MESSAGES = int(
    os.getenv("DOG_BOT_MODERATION_TENURE_TRUSTED_MESSAGES", "20")
)
MODERATION_TENURE_TRUSTED_DAYS = float(
    os.getenv("DOG_BOT_MODERATION_TENURE_TRUSTED_DAYS", "3")
)
MODERATION_TENURE_DAMPEN = int(
    os.getenv("DOG_BOT_MODERATION_TENURE_DAMPEN", "2")
)

# ------------------------------------------------------------
# ВАЙТЛИСТ МОДЕРАЦИИ (!вайтлист, см. commands.py/moderation.py):
# кому разрешено добавлять/убирать записи в moderation_whitelist.
#
# Сознательно НЕ переиспользуем BOT_ADMIN_NICKS/HUNT_ADMIN_NICKS:
# те сверяются по нику, а ник в комнате может занять кто угодно
# после выхода настоящего владельца — для команды, которая решает,
# кого модерация больше не трогает, этого недостаточно. Bare JID
# участника Пёс видит только через occupant-метаданные MUC
# (get_jid_property), и видит их только пока сам является
# moderator/admin/owner в комнате (см. moderation.py::
# _mod_occupant_jid/_mod_is_privileged) — рядовой участник его
# подделать не может. Поэтому право на !вайтлист выдаётся отдельно,
# по подтверждённому bare JID.
#
# Заполнить реальным bare JID перед использованием, например:
#   DOG_BOT_MODERATION_WHITELIST_ADMINS=russoturisto@example.com
# Несколько JID — через запятую. Пока переменная не задана, команда
# !вайтлист недоступна никому (пустой allowlist).
MODERATION_WHITELIST_ADMIN_JIDS = {
    jid.strip().split("/", 1)[0].casefold()
    for jid in os.getenv(
        "DOG_BOT_MODERATION_WHITELIST_ADMINS",
        "",
    ).split(",")
    if jid.strip()
}

# ============================================================
# MODERATION LLM CHECK (второй, семантический уровень антибота)
# ============================================================
# Числовые эвристики выше (_mod_score) ловят только БЫСТРЫЙ флуд —
# burst/duplicate/pacing считаются в окнах 5/15/30с. Бот, который
# намеренно держит паузу ~15с между сообщениями (классический
# рейд-паттерн: "я анатоле" / "яна цист" / "слава зеленскому" по
# кругу), под эти окна не попадает вообще: в любом 15-секундном окне
# у него от силы 1-2 сообщения. Эвристики его не увидят никогда —
# нужен смысловой, а не числовой сигнал.
#
# Поэтому раз в MODERATION_LLM_EVERY_MESSAGES сообщений (не чаще
# MODERATION_LLM_COOLDOWN_SECONDS) лёгкая LLM смотрит на последние
# сообщения ЭТОГО конкретного ника — не архив всей комнаты — и
# только КЛАССИФИЦИРУЕТ: {"bot": true/false, "confidence": 0..1}.
# Решение, что с этим делать (фиксировать/удалять/игнорировать),
# по-прежнему принимает код через пороги ниже — LLM не получает
# права модерировать напрямую (см. moderate_incoming_muc).
MODERATION_LLM_ENABLED = (
    os.getenv("DOG_BOT_MODERATION_LLM_ENABLED", "1") == "1"
)

# Пусто = взять ROUTER_MODEL (тот же дешёвый классификатор, что и у
# роутера/quiz judge) — отдельная модель тут не нужна.
MODERATION_LLM_MODEL = os.getenv(
    "DOG_BOT_MODERATION_LLM_MODEL",
    "",
)

# Короткий JSON без рассуждений — {"bot":.., "confidence":.., "reason":
# ".."} укладывается с большим запасом.
MODERATION_LLM_MAX_TOKENS = int(
    os.getenv("DOG_BOT_MODERATION_LLM_MAX_TOKENS", "150")
)

MODERATION_LLM_TIMEOUT = float(
    os.getenv("DOG_BOT_MODERATION_LLM_TIMEOUT", "10")
)

# Как и у роутера/quiz judge (см. их комментарии) — без этого модель
# может потратить весь маленький MAX_TOKENS на reasoning_content и
# вернуть пустой content.
MODERATION_LLM_DISABLE_THINKING = (
    os.getenv("DOG_BOT_MODERATION_LLM_DISABLE_THINKING", "1") == "1"
)

# ------------------------------------------------------------
# КОГДА ВЫЗЫВАТЬ LLM — ДВА НЕЗАВИСИМЫХ УСЛОВИЯ (см. _mod_llm_check):
#
# 1) "Серая зона" по score — ТЗ явно требует, чтобы LLM звала
#    именно как арбитр при неуверенности эвристик, а не как
#    самостоятельный детектор. score < GREYZONE_MIN или
#    score >= MODERATION_SCORE_DELETE — решение и так уже очевидно
#    (ничего не делать или сразу удалять), LLM не нужна.
# 2) Кадансный fallback (EVERY_MESSAGES/COOLDOWN) — ловит НАМЕРЕННО
#    медленного бота (паузы ~15-20с), который эвристики никогда не
#    затолкают в серую зону по score (burst/duplicate-окна просто
#    не видят редкие сообщения), но который заведомо ведёт себя
#    подозрительно на длинной дистанции. Без этого условия
#    "медленный рейдер" был бы невидим навсегда — см. существующий
#    smoke-тест "LLM ловит медленного бота".
#
# Оба условия используют одинаковый MODERATION_LLM_COOLDOWN_SECONDS,
# так что дорогой вызов LLM в любом случае не может происходить
# чаще, чем этот интервал, независимо от того, какое из двух условий
# его вызвало.
MODERATION_LLM_GREYZONE_MIN = int(
    os.getenv("DOG_BOT_MODERATION_LLM_GREYZONE_MIN", "5")
)

# Проверяем не на каждое сообщение (дорого и не нужно — рейдер за
# 8 сообщений никуда не денется), а раз в столько сообщений от
# конкретного ника...
MODERATION_LLM_EVERY_MESSAGES = int(
    os.getenv("DOG_BOT_MODERATION_LLM_EVERY_MESSAGES", "8")
)

# ...но не чаще, чем раз в столько секунд — иначе очень активный
# человек (не бот) гонял бы классификатор постоянно.
MODERATION_LLM_COOLDOWN_SECONDS = float(
    os.getenv("DOG_BOT_MODERATION_LLM_COOLDOWN_SECONDS", "60")
)

# Меньше этого сообщений в истории — смысловой картины ещё нет,
# классификатор не вызываем.
MODERATION_LLM_MIN_MESSAGES = int(
    os.getenv("DOG_BOT_MODERATION_LLM_MIN_MESSAGES", "5")
)

# Сколько последних сообщений этого ника показываем классификатору.
# Именно ник, а не вся комната — запрос должен оставаться дешёвым.
MODERATION_LLM_CONTEXT_MESSAGES = int(
    os.getenv("DOG_BOT_MODERATION_LLM_CONTEXT_MESSAGES", "8")
)

# events должны прожить достаточно долго, чтобы набрать
# MODERATION_LLM_CONTEXT_MESSAGES даже при паузах ~15-20с между
# сообщениями (см. комментарий выше про "паузу 15с") — иначе
# основной MODERATION_WINDOW_SECONDS (30с) вычистит историю до того,
# как накопится смысловая картина. Реальный горизонт хранения events
# — максимум из этой константы и MODERATION_WINDOW_SECONDS, так что
# существующий, уже настроенный MODERATION_WINDOW_SECONDS этим не
# затрагивается.
MODERATION_LLM_CONTEXT_WINDOW_SECONDS = int(
    os.getenv("DOG_BOT_MODERATION_LLM_CONTEXT_WINDOW", "180")
)

# confidence >= этого порога + bot=true -> фиксируем JID/ник как
# бота немедленно (тот же механизм, что и у эвристик, см.
# mod_fixed_jids/mod_fixed_nicks) — без этого дополнительных
# подтверждений не требуется, даже если эвристики молчали.
MODERATION_LLM_BOT_CONFIDENCE = float(
    os.getenv("DOG_BOT_MODERATION_LLM_BOT_CONFIDENCE", "0.75")
)

# Порог пониже: сам по себе не фиксирует бота, а лишь подмешивает
# сигнал "llm_bot_suspect" в общий score/reasons (_mod_score) — то
# есть работает как ещё один голос в существующей многосигнальной
# схеме, а не как отдельный вердикт в обход неё.
MODERATION_LLM_WATCH_CONFIDENCE = float(
    os.getenv("DOG_BOT_MODERATION_LLM_WATCH_CONFIDENCE", "0.55")
)

# ============================================================
# OTHER CONFIG
# ============================================================

IGNORED_SENDERS = {
    "Сигмабой",
    "Вера",
}

FACT_CATEGORIES = {
    "PREFERENCE",
    "EVENT",
    "INFO",
    "HOBBY",
    "WORK",
    "TECH",
    "PERSONAL",
}

FORBIDDEN_FACT_CATEGORIES = {
    "SECRET",
    "CONFIDENTIAL",
    "PRIVATE",
    "LEAK",
    "GOSSIP",
}

SECRET_MARKERS = (
    "пароль",
    "password",
    "passwd",
    "token",
    "токен",
    "api_key",
    "api-key",
    "apikey",
    "secret",
    "секретный ключ",
    "private key",
    "приватный ключ",
    "seed phrase",
    "сид-фраз",
    "seed-фраз",
    "mnemonic",
    "мнемоническая фраза",
)


# ============================================================
# ЭНТРОПИЙНЫЙ ФИЛЬТР ТОКЕНОВ/КЛЮЧЕЙ
# ============================================================
#
# SECRET_MARKERS ловит секрет только если рядом есть слово вроде
# "пароль"/"token". Этот фильтр — вторая, независимая линия
# защиты: ищет в тексте отдельные "слова"-токены, похожие на
# случайный ключ/пароль (длинные, с высокой энтропией Шеннона,
# без явного повторяющегося или монотонного паттерна), даже без
# подписи рядом. Работает на уровне отдельных токенов, а не всей
# фразы целиком — обычные предложения на естественном языке
# энтропию всей строки так высоко не поднимают, а случайный
# токен внутри предложения — поднимает.
#
# Выключить (0/false), если фильтр начнёт резать легитимные
# факты (длинные хэштеги, идентификаторы и т.п.).

SECRET_ENTROPY_FILTER_ENABLED = (
    os.getenv("DOG_BOT_SECRET_ENTROPY_FILTER", "true")
    .strip()
    .lower()
    not in ("0", "false", "no", "off")
)

# Короче токены entropy-фильтром не проверяем — случайность
# короткой строки статистически ненадёжна, а обычные слова легко
# дают ложные срабатывания.
SECRET_TOKEN_MIN_LEN = int(
    os.getenv("DOG_BOT_SECRET_TOKEN_MIN_LEN", "20")
)

# Доля от максимально возможной энтропии алфавита токена, начиная
# с которой токен считается "случайным" (как в примере из
# постановки задачи).
SECRET_TOKEN_ENTROPY_RATIO = float(
    os.getenv("DOG_BOT_SECRET_TOKEN_ENTROPY_RATIO", "0.85")
)

_TOKEN_SPLIT_RE = re.compile(r"[^\w\-\.\+/=]+", re.UNICODE)


def _shannon_entropy(text):
    """Энтропия Шеннона в битах на символ."""
    if not text:
        return 0.0

    freq = Counter(text)
    length = len(text)

    return -sum(
        (count / length) * math.log2(count / length)
        for count in freq.values()
    )


def _repetition_score(text):
    """
    Штрафует строки с длинными сериями одинаковых символов
    (например, "aaaaaaaa"). 0 — очень монотонно, 1 — сильно
    чередуется.
    """
    if len(text) < 2:
        return 1.0

    runs = 1

    for i in range(1, len(text)):
        if text[i] != text[i - 1]:
            runs += 1

    return runs / len(text)


def _looks_like_random_token(
    token,
    min_len=SECRET_TOKEN_MIN_LEN,
    entropy_ratio=SECRET_TOKEN_ENTROPY_RATIO,
    rep_threshold=0.4,
):
    if len(token) < min_len:
        return False

    alphabet_size = len(set(token))

    if alphabet_size < 2:
        return False

    max_ent = math.log2(alphabet_size)
    ent = _shannon_entropy(token)
    rep = _repetition_score(token)

    return (
        ent >= entropy_ratio * max_ent
        and rep >= rep_threshold
    )


def contains_high_entropy_secret(text):
    """
    True, если в тексте нашёлся отдельный токен, похожий на
    случайный ключ/пароль/секрет. Дополняет keyword-фильтр
    SECRET_MARKERS, а не заменяет его — SECRET_MARKERS ловит по
    контексту ("вот мой пароль: ..."), этот фильтр ловит "голый"
    высокоэнтропийный токен без подписи.
    """
    if not SECRET_ENTROPY_FILTER_ENABLED or not text:
        return False

    for token in _TOKEN_SPLIT_RE.split(text):
        token = token.strip("-_./+=")

        if _looks_like_random_token(token):
            return True

    return False


# ============================================================
# MEMORY CONFIG
# ============================================================
#
# Старая схема: все факты пользователя одним куском (facts_json)
# уходили в промпт LLM целиком ("дамп досье"). Новая схема:
# факты живут в таблице user_facts, а в промпт на каждый запрос
# попадает только маленькая взвешенно-случайная выборка —
# см. UserFactsDB.select_memory_context().

# Сколько фактов на пользователя храним всего в БД. Это не то,
# сколько уходит в LLM за раз — туда идёт всего MEMORY_CONTEXT_*.
MAX_FACTS_PER_USER = int(
    os.getenv("DOG_BOT_MAX_FACTS_PER_USER", "500")
)

# Сколько фактов реально подмешиваем в промпт LLM за один запрос.
MEMORY_CONTEXT_MIN_FACTS = int(
    os.getenv("DOG_BOT_MEMORY_MIN_FACTS", "2")
)
MEMORY_CONTEXT_MAX_FACTS = int(
    os.getenv("DOG_BOT_MEMORY_MAX_FACTS", "5")
)

# Сколько кандидатов держим в топе перед взвешенным случайным
# выбором (см. доку: "не тупой random.choice()").
MEMORY_TOP_CANDIDATES = int(
    os.getenv("DOG_BOT_MEMORY_TOP_CANDIDATES", "10")
)

# Факт, использованный недавно, "остывает" это время (часы),
# прежде чем у него снова нормальные шансы быть выбранным.
MEMORY_COOLDOWN_HOURS = float(
    os.getenv("DOG_BOT_MEMORY_COOLDOWN_HOURS", "6")
)

# Веса компонентов формулы score (см. доку):
# score = importance*W_IMPORTANCE + novelty*W_NOVELTY
#         + relevance*W_RELEVANCE - cooldown*W_RECENT_PENALTY
MEMORY_WEIGHT_IMPORTANCE = 3
MEMORY_WEIGHT_NOVELTY = 2
MEMORY_WEIGHT_RELEVANCE = 5
MEMORY_WEIGHT_RECENT_PENALTY = 6

# Отрицательные факты ("не любит X", "не называть Y") получают
# фиксированный бонус к score и приоритет при отсечении по лимиту —
# они не должны теряться в случайной выборке.
MEMORY_NEGATIVE_BONUS = 8

# Сколько фактов показываем при явном запросе ("!досье") —
# режим Explicit Memory: полный (в пределах лимита) список, а не
# взвешенная случайная выборка.
EXPLICIT_MEMORY_LIMIT = int(
    os.getenv("DOG_BOT_EXPLICIT_MEMORY_LIMIT", "15")
)

# Короткие частые слова, которые не считаем ключевыми при поиске
# релевантных фактов по тексту сообщения.
MEMORY_STOPWORDS = {
    "это", "что", "как", "для", "при", "все", "его", "или",
    "она", "они", "мне", "меня", "тебя", "тебе", "себя", "себе",
    "уже", "ещё", "если", "чтобы", "когда", "быть", "есть",
    "был", "была", "было", "были", "просто", "очень", "тоже",
    "так", "вот", "там", "тут", "тогда", "какой", "какая",
    "какие", "который", "которая", "которые", "кто", "чем",
    "чём", "нет", "да", "ну", "же", "бы", "ли", "и", "а", "но",
    "the", "and", "for", "are", "was", "were", "with", "that",
    "this", "you", "your", "have", "has", "not",
}


# ============================================================
# FORTUNES (случайные фразы-квитки)
# ============================================================
#
# В обычном диалоге (не команды) с небольшим шансом Пёс
# вплетает в ответ одну фразу из data/fortunes.txt — самую
# подходящую по контексту из 5 случайно выбранных.

FORTUNE_CHANCE = float(
    os.getenv("DOG_BOT_FORTUNE_CHANCE", "0.1")
)

FORTUNE_SAMPLE_SIZE = int(
    os.getenv("DOG_BOT_FORTUNE_SAMPLE_SIZE", "5")
)

_FORTUNES_PATH = os.path.join(
    os.path.dirname(__file__),
    "data",
    "fortunes.txt",
)

_fortunes_cache = None


def _load_fortunes():
    global _fortunes_cache

    if _fortunes_cache is None:

        try:
            with open(
                _FORTUNES_PATH,
                "r",
                encoding="utf-8",
            ) as f:

                _fortunes_cache = [
                    line.strip()
                    for line in f
                    if line.strip()
                ]

        except OSError:

            logging.warning(
                "[FORTUNES] Не удалось прочитать %s",
                _FORTUNES_PATH,
            )

            _fortunes_cache = []

    return _fortunes_cache


def maybe_roll_fortunes(
    chance=None,
    sample_size=None,
):
    """
    С вероятностью `chance` возвращает `sample_size` случайных
    фраз из fortunes.txt (без повторов). Иначе — пустой список.

    Сам выбор "какая из них лучше подходит" — не здесь,
    это решает LLM по контексту диалога.
    """

    chance = (
        chance
        if chance is not None
        else FORTUNE_CHANCE
    )

    sample_size = (
        sample_size
        if sample_size is not None
        else FORTUNE_SAMPLE_SIZE
    )

    if random.random() >= chance:
        return []

    pool = _load_fortunes()

    if not pool:
        return []

    sample_size = min(
        sample_size,
        len(pool),
    )

    return random.sample(
        pool,
        sample_size,
    )


# ============================================================
# CONTEXT ROUTER CONFIG (распределённая сборка контекста)
# ============================================================
#
# Раньше ask_llm() тащил в промпт ВСЁ разом на каждый вызов:
# полную историю, память, абзац про дружбу с russoturisto —
# независимо от того, нужно ли это конкретному сообщению.
#
# Теперь перед основным вызовом LLM решается (см. router.py),
# какие куски контекста реально нужны:
#
#   message
#      ↓
#   Python precheck (уровень 0, без LLM — точные триггеры,
#                     то, что и так уже известно из metadata)
#      ↓
#   если что-то осталось неясным →
#   router LLM (уровень 1, дешёвая микро-классификация,
#               ~50-100 токенов, temperature=0)
#      ↓
#   routing-флаги (needs_memory/needs_history/needs_economy/
#                  needs_market/about_russoturisto)
#      ↓
#   context builder (в ask_llm) собирает ТОЛЬКО нужное
#      ↓
#   основной LLM

# Общий выключатель — если False, роутер вообще не вызывается,
# и всё неясное трактуется как "нужно" (старое поведение,
# без урезания контекста).
ROUTER_ENABLED = (
    os.getenv("DOG_BOT_ROUTER_ENABLED", "1") == "1"
)

# Отдельная (обычно та же самая) модель для роутера — можно
# указать через env более дешёвую/быструю модель, когда такая
# появится у провайдера. По умолчанию используется основная
# модель бота (см. DogCoreMixin.model), просто с маленьким
# max_tokens и temperature=0 — экономия идёт за счёт размера
# промпта и ответа, а не смены модели.
ROUTER_MODEL = os.getenv(
    "DOG_BOT_ROUTER_MODEL",
    "",
)  # пусто = взять self.model

# Роутеру запрещено рассуждать — только компактный JSON.
ROUTER_MAX_TOKENS = int(
    os.getenv("DOG_BOT_ROUTER_MAX_TOKENS", "160")
)

# ВАЖНО: маленький max_tokens сам по себе НЕ запрещает модели
# рассуждать. У DeepSeek V4 (deepseek-v4-flash/-pro) "thinking"
# включён по умолчанию — модель сперва пишет chain-of-thought в
# reasoning_content и только потом финальный текст в content. При
# ROUTER_MAX_TOKENS=80 весь бюджет уходит на рассуждение, генерация
# обрывается по лимиту ДО начала content — API возвращает валидный
# ответ с usage.completion_tokens > 0, но content="" ("нет
# текстового content" в _call_openrouter_once). Роутер ретраит 4
# раза (см. LLM_RETRY_MAX_ATTEMPTS) с тем же результатом, тратит
# ~20-30 с и падает на _FAILSAFE_DEFAULTS — то есть каждый вызов
# роутера ничего не оптимизирует, а только добавляет задержку и
# лишние токены. Явно выключаем thinking для роутера параметром
# {"thinking": {"type": "disabled"}} в теле запроса (см.
# _call_openrouter_once/call_openrouter, thinking_disabled=...).
# Если провайдера сменишь на модель без этого параметра — выключи
# (False/"0") и раздай ROUTER_MAX_TOKENS с запасом вместо этого.
#
# ЭТА ЖЕ БОЛЕЗНЬ НЕ ОГРАНИЧЕНА РОУТЕРОМ: на практике "main" (см.
# ask_llm, MAIN_MAX_TOKENS=800) тоже периодически ловит "нет
# текстового content (finish_reason=length reasoning_content_len=
# 2600+)" — модель на отдельных сообщениях уходит в рассуждение на
# тысячи символов и не укладывается уже в 800 токенов. Разница с
# роутером только в том, что там бюджет 80 и это происходило
# КАЖДЫЙ раз, а здесь бюджет больше и это происходит на отдельных
# сообщениях — но механизм и результат («Исчерпан лимит попыток»,
# бот молчит ~30-90 с и не отвечает вообще) совершенно те же самые.
# Поэтому thinking по умолчанию выключен ГЛОБАЛЬНО для всех вызовов
# (см. LLM_THINKING_DISABLED_DEFAULT ниже в блоке LLM RETRY CONFIG),
# а не только для роутера — ROUTER_DISABLE_THINKING оставлен как
# отдельный, независимый тумблер на случай, если понадобится
# отличающееся поведение именно для роутера.
ROUTER_DISABLE_THINKING = (
    os.getenv("DOG_BOT_ROUTER_DISABLE_THINKING", "1") == "1"
)

ROUTER_TIMEOUT = float(
    os.getenv("DOG_BOT_ROUTER_TIMEOUT", "12")
)

# Роутер не должен становиться ещё одним источником затрат —
# если провайдер уже перегружен (см. LLM_HOLD_UNTIL_RESET_ENABLED
# в call_openrouter), лишний вызов роутера только зря спалит время
# на таймаут, поэтому у него отдельный, короткий бюджет.

# ============================================================
# QUIZ JUDGE (второй уровень проверки ответа викторины)
# ============================================================
# Быстрый Python exact/normalized match (answer_matches) остаётся
# ПЕРВЫМ и единственным путём для очевидных ответов — LLM тут не
# вызывается вообще. Judge подключается ТОЛЬКО когда exact match
# не сработал, и решает ровно одну вещь: верен ли ответ по смыслу
# (1/0). Judge НЕ имеет доступа к начислению награды/закрытию
# викторины — это остаётся исключительно за try_win_quiz (см.
# core.py, защищённый critical section quiz_lock). Результат judge
# — лишь ещё один способ пройти тот же самый answer_matches-подобный
# путь дальше по коду, а не отдельный игровой механизм.
QUIZ_JUDGE_ENABLED = (
    os.getenv("DOG_BOT_QUIZ_JUDGE_ENABLED", "1") == "1"
)

# Отдельная (обычно та же дешёвая) модель — по умолчанию пусто =
# взять ROUTER_MODEL (тот же классификатор, что и у контекстного
# роутера), а не основную self.model.
QUIZ_JUDGE_MODEL = os.getenv(
    "DOG_BOT_QUIZ_JUDGE_MODEL",
    "",
)

# Ответ judge — буквально один символ ("1" или "0"), поэтому бюджет
# предельно маленький: запас на случайные пробелы/перенос строки,
# но не на рассуждения или JSON.
QUIZ_JUDGE_MAX_TOKENS = int(
    os.getenv("DOG_BOT_QUIZ_JUDGE_MAX_TOKENS", "8")
)

QUIZ_JUDGE_TIMEOUT = float(
    os.getenv("DOG_BOT_QUIZ_JUDGE_TIMEOUT", "12")
)

# Как и у роутера (см. ROUTER_DISABLE_THINKING) — без этого DeepSeek
# V4 может потратить весь and-так крошечный QUIZ_JUDGE_MAX_TOKENS на
# reasoning_content и вернуть пустой content.
QUIZ_JUDGE_DISABLE_THINKING = (
    os.getenv("DOG_BOT_QUIZ_JUDGE_DISABLE_THINKING", "1") == "1"
)

# Не вызываем judge на сообщениях, которые физически не похожи на
# попытку ответить (иначе при активной викторине LLM дёргалась бы на
# КАЖДОЕ обычное сообщение в чате, что и дорого, и медленно). Ответы
# сами по себе короткие (генератор просит "одно слово или число"),
# так что даже с запасом на естественную формулировку разумный лимит
# длины отсекает случайный оффтоп, не отсекая настоящие ответы.
QUIZ_JUDGE_MAX_ANSWER_LEN = int(
    os.getenv("DOG_BOT_QUIZ_JUDGE_MAX_ANSWER_LEN", "150")
)

# Отдельный, независимый от общих LLM/quiz лимитов rate limit на
# сам judge-вызов — по той же причине (иначе один болтливый чат при
# активной викторине может закидать провайдера запросами).
QUIZ_JUDGE_RATE_USER_LIMIT = int(
    os.getenv("DOG_BOT_QUIZ_JUDGE_RATE_USER_LIMIT", "4")
)
QUIZ_JUDGE_RATE_USER_WINDOW = float(
    os.getenv("DOG_BOT_QUIZ_JUDGE_RATE_USER_WINDOW", "30")
)
QUIZ_JUDGE_RATE_GLOBAL_LIMIT = int(
    os.getenv("DOG_BOT_QUIZ_JUDGE_RATE_GLOBAL_LIMIT", "20")
)
QUIZ_JUDGE_RATE_GLOBAL_WINDOW = float(
    os.getenv("DOG_BOT_QUIZ_JUDGE_RATE_GLOBAL_WINDOW", "30")
)


def parse_quiz_judge_verdict(raw):
    """
    Строгий парсер ответа judge. Принимает ТОЛЬКО '1' или '0' (с
    произвольными пробелами/переносами вокруг) — никакого доверия
    произвольному тексту LLM. Возвращает True/False/None (None —
    ответ не удалось разобрать однозначно, вызывающая сторона
    обязана трактовать это как "не подтверждено", а не как победу).
    """
    if raw is None:
        return None

    stripped = str(raw).strip()

    if stripped == "1":
        return True

    if stripped == "0":
        return False

    return None


# Совсем короткие/междометные реплики — Python сразу решает, что
# им не нужны ни память, ни история, ни рынок, ни экономика, и
# роутер для них вообще не вызывается (уровень 0, "очевидно").
TRIVIAL_REACTION_RE = re.compile(
    r"^[\s.,!?;:()~^]*"
    r"("
    r"[ах]{3,}|"
    r"лол+|кек+|рофл\w*|ору+|топ|бля|блин|"
    r"да|нет|неа|ага|угу|ок|окей|норм|"
    r"плюс|минус|[+\-]\d*"
    r")"
    r"[\s.,!?;:()~^]*$",
    re.IGNORECASE,
)

TRIVIAL_REACTION_MAX_LEN = 12


def is_trivial_reaction(text):
    """
    True для коротких междометных реплик ("ахаха", "+1", "кек",
    "да", "лол"), которым заведомо не нужен ни контекст памяти,
    ни история диалога, ни экономика/рынок — ни Python, ни тем
    более LLM-роутер тут разбираться не должны.
    """
    stripped = (text or "").strip()

    if not stripped or len(stripped) > TRIVIAL_REACTION_MAX_LEN:
        return False

    return bool(TRIVIAL_REACTION_RE.match(stripped))


# Точные триггеры экономики/рынка — по подстрокам, без словоформ,
# в духе LOAD_WORK_REQUEST_MARKERS выше. Если сработали — Python
# уверенно ставит флаг сам, LLM-роутер не нужен.
ECONOMY_MARKERS = (
    "коин", "монет", "банк", "инфляц", "баланс", "сколько у меня",
    "сколько денег", "разбогате", "бедн", "заработ",
)

MARKET_MARKERS = (
    "лот", "рынок", "маркет", "выкуп", "инвестир", "инвест",
    "продай информац", "продам информац", "куплю информац",
    "pump", "памп",
)


def has_marker(lower_text, markers):
    return any(marker in lower_text for marker in markers)


# Точные триггеры "вспомни/помнишь" — как ECONOMY_MARKERS/
# MARKET_MARKERS выше: если сработали, Python сразу ставит
# needs_recall=True без обращения к LLM-роутеру.
RECALL_MARKERS = (
    "вспомни", "вспоминай", "припомни",
    "помнишь", "не забыл", "что помнишь",
    "что там было про", "что знаешь про",
    "расскажи что знаешь", "расскажи, что знаешь",
)

# С этим шансом (на КАЖДОЕ уже решённое отвечать сообщение,
# кроме тривиальных реакций — см. is_trivial_reaction) Пёс сам,
# без явной просьбы, лезет в память что-то вспомнить — для
# спонтанных ремарок в характере ("О, кстати, вспомнил...").
RECALL_RANDOM_CHANCE = float(
    os.getenv("DOG_BOT_RECALL_RANDOM_CHANCE", "0.03")
)

# Сколько фактов максимум возвращает целевой (не ambient) поиск
# по recall — отдельно от MEMORY_CONTEXT_MAX_FACTS, т.к. это
# осознанный, а не фоновый подмес.
# Сколько упомянутых/адресованных в сообщении ников (НЕ отправителя)
# максимум получают лёгкий точечный факт в dossier без полного
# recall — см. блок MENTIONED-FACTS в llm.py. Ограничение защищает
# от лишних DB round-trip'ов и раздувания промпта на сообщениях с
# длинным списком упоминаний.
MENTIONED_FACT_LIMIT = int(
    os.getenv("DOG_BOT_MENTIONED_FACT_LIMIT", "2")
)

RECALL_SEARCH_LIMIT = int(
    os.getenv("DOG_BOT_RECALL_SEARCH_LIMIT", "5")
)


# ============================================================
# RECALL TARGET RESOLUTION (о КОМ поднимаем данные при recall)
# ============================================================
#
# needs_recall=True (см. router.py) говорит только "автору нужно
# что-то вспомнить", но не "про кого". Раньше цель искалась одним
# способом — target_nick/find_mentioned_nick, точное совпадение
# ника где-то в тексте — и молча схлопывалась в "никого не нашли"
# на любой словоформе/роли/опечатке (см. core.py:
# resolve_recall_target_nick и llm.py: блок RECALL). Ниже —
# конфиг для более полного каскада: точный ник → ролевой алиас →
# нечёткое сравнение по БД → LLM-роутер как последний фолбэк.

# Ролевые алиасы: слово в тексте → канонический ник аккаунта.
# Конечный список словоформ, а не морфологический анализ — для
# той горстки ролей, которая реально упоминается в чате (см.
# character-промпт в llm.py: "russoturisto - капитан,
# arthoriapendragon - кок, goos - боцман"). Если список ролей
# вырастет, стоит заменить перечисление падежей на pymorphy2 или
# аналог.
ROLE_ALIASES = {}


def _register_role_aliases(base_nick, *word_forms):
    for form in word_forms:
        ROLE_ALIASES[form.casefold()] = base_nick


_register_role_aliases(
    "russoturisto",
    "капитан", "капитана", "капитану",
    "капитаном", "капитане",
    "кэп", "кэпа", "кэпу", "кэпом", "кэпе",
    "шкипер", "шкипера", "шкиперу",
    "шкипером", "шкипере",
)

_register_role_aliases(
    "arthoriapendragon",
    "кок", "кока", "коку", "коком", "коке",
    "повар", "повара", "повару",
    "поваром", "поваре",
)

_register_role_aliases(
    "goos",
    "боцман", "боцмана", "боцману",
    "боцманом", "боцмане",
)

# Можно расширить/переопределить через окружение, не трогая код:
# DOG_BOT_ROLE_ALIASES="кэп=russoturisto,штурман=someone"
for _pair in os.getenv("DOG_BOT_ROLE_ALIASES", "").split(","):

    if "=" not in _pair:
        continue

    _word, _nick = _pair.split("=", 1)
    _word = _word.strip().casefold()
    _nick = _nick.strip()

    if _word and _nick:
        ROLE_ALIASES[_word] = _nick

# Порог нечёткого сравнения ника (см. database.py:
# _best_fuzzy_nick_match) — тот же механизм (difflib.
# SequenceMatcher), что и у резолва автора цитаты
# (core.py:find_quote_author, порог 0.6), но здесь строже:
# ошибка здесь означает подъём ЧУЖИХ личных фактов не под того
# пользователя, а не просто неверную атрибуцию цитаты.
RECALL_FUZZY_NICK_THRESHOLD = float(
    os.getenv("DOG_BOT_RECALL_FUZZY_THRESHOLD", "0.72")
)

# Насколько лучший кандидат должен ОБОГНАТЬ второго по score, чтобы
# нечёткое совпадение считалось однозначным, а не AMBIGUOUS (см.
# database.py:_best_fuzzy_nick_match). Раньше проверялся только
# best_score >= threshold — при похожих никах ("Вера"/"Вера123"/
# "Верочка") это позволяло уверенно выбрать один кандидат, хотя
# второй подходил почти так же хорошо, и recall мог поднять досье
# не того человека. AMBIGUOUS не откатывается на self — вызывающая
# сторона обязана сообщить, что цель не определена однозначно.
RECALL_FUZZY_NICK_MARGIN = float(
    os.getenv("DOG_BOT_RECALL_FUZZY_MARGIN", "0.05")
)

# Сколько согласившихся пользователей максимум тянем из БД как
# пул кандидатов для нечёткого сравнения — дёшево (сравнение в
# памяти, без LLM), поэтому пул может быть большим.
RECALL_FUZZY_CANDIDATE_LIMIT = int(
    os.getenv("DOG_BOT_RECALL_FUZZY_CANDIDATE_LIMIT", "300")
)

# Сколько ников максимум передаём LLM-роутеру на последнем
# фолбэке (_ask_recall_target в router.py) — в отличие от
# нечёткого сравнения, это реальный вызов LLM, поэтому список
# кандидатов должен быть маленьким и дешёвым для промпта.
RECALL_LLM_TARGET_CANDIDATE_LIMIT = int(
    os.getenv("DOG_BOT_RECALL_LLM_CANDIDATE_LIMIT", "25")
)


# ============================================================
# WILD HUNT (Дикая Охота) CONFIG
# ============================================================
#
# Ежеразовое событие в комнате: Пёс — Предводитель Охоты.
# За HUNT_REGISTRATION_MINUTES до старта открывается запись
# (через обычный токен-flow: !охота в общий чат, сессия +
# токен как у любой другой команды) и начинаются намёки.
# В T=0 фиксируется список тех, кто успел зарегистрироваться
# и всё ещё на связи (snapshot) — состав дальше не меняется.

HUNT_ADMIN_NICKS = {
    nick.strip().casefold()
    for nick in os.getenv(
        "DOG_BOT_HUNT_ADMINS",
        "russoturisto,goos",
    ).split(",")
    if nick.strip()
}

# За сколько минут до старта открывается регистрация —
# одновременно с первым намёком.
HUNT_REGISTRATION_MINUTES = int(
    os.getenv(
        "DOG_BOT_HUNT_REGISTRATION_MINUTES",
        "60",
    )
)

# На каких минутах до старта Пёс даёт намёки (по убыванию,
# последнее число — ближе всего к T=0).
HUNT_HINT_OFFSETS_MINUTES = sorted(
    (
        int(x)
        for x in os.getenv(
            "DOG_BOT_HUNT_HINT_OFFSETS",
            "60,40,20,5",
        ).split(",")
        if x.strip()
    ),
    reverse=True,
)

# Сколько длится сама Охота от T=0 до финала.
HUNT_DURATION_MINUTES = int(
    os.getenv(
        "DOG_BOT_HUNT_DURATION_MINUTES",
        "30",
    )
)

# Как часто фоновый цикл проверяет, не пора ли переходить
# к следующей фазе Охоты (секунды).
HUNT_TICK_SECONDS = int(
    os.getenv(
        "DOG_BOT_HUNT_TICK_SECONDS",
        "30",
    )
)

# Максимум участников, которых персонально называем и, может
# быть, снабжаем фактом о них в стартовом/финальном отыгрыше —
# чтобы не раздувать промпт при большой Охоте.
HUNT_PERSONALIZED_PARTICIPANTS = int(
    os.getenv(
        "DOG_BOT_HUNT_PERSONALIZED_PARTICIPANTS",
        "3",
    )
)

# ------------------------------------------------------------
# WILD HUNT: наследие лесной версии 2.0 (HOUNDS/RIDERS/DISTANCE)
# ------------------------------------------------------------
#
# Эти константы больше не читает активный морской боевой цикл
# (см. dogbot/hunt.py, HuntMixin) — он оперирует кораблями/NPC из
# hunt_ships/hunt_npcs напрямую. Они остались только как значения
# ПО УМОЛЧАНИЮ для одноимённых колонок hunt_player_state/hunts при
# миграции схемы (см. database.py) — трогать без необходимости не
# нужно, но и переименовывать их сейчас дороже, чем оставить как
# задокументированное наследие.

HUNT_INITIAL_HOUNDS = int(
    os.getenv("DOG_BOT_HUNT_HOUNDS", "12")
)

HUNT_INITIAL_RIDERS = int(
    os.getenv("DOG_BOT_HUNT_RIDERS", "2")
)

HUNT_DEFAULT_AUXILIARY_CREW = int(
    os.getenv("DOG_BOT_HUNT_AUXILIARY_CREW", "12")
)

# Легаси: не используется активным циклом.
HUNT_MIN_HOUNDS = 2

# Легаси: шкалы игрока 2.0, не используются активным морским циклом.
HUNT_STARTING_DISTANCE = 60
HUNT_STARTING_TRAIL = 30

# Легаси: использовалось только удалённой в 3.0.5 механикой поимки.
HUNT_RECOVER_DISTANCE = 25

# Минимальный интервал между действиями одного игрока.
HUNT_ACTION_COOLDOWN_SECONDS = int(
    os.getenv(
        "DOG_BOT_HUNT_ACTION_COOLDOWN",
        "25",
    )
)

# Случайное окно между самостоятельными "ходами" вражеского
# флота — чтобы Пёс не спамил сообщениями каждую минуту.
HUNT_EVENT_MIN_SECONDS = int(
    os.getenv(
        "DOG_BOT_HUNT_EVENT_MIN_SECONDS",
        "90",
    )
)

HUNT_EVENT_MAX_SECONDS = int(
    os.getenv(
        "DOG_BOT_HUNT_EVENT_MAX_SECONDS",
        "240",
    )
)

# Награды (пёс-коины и репутация) за достижения в конце Охоты.
HUNT_REWARD_SURVIVED = float(
    os.getenv("DOG_BOT_HUNT_REWARD_SURVIVED", "15")
)

HUNT_REWARD_HOUND_KILL = float(
    os.getenv("DOG_BOT_HUNT_REWARD_HOUND_KILL", "10")
)

HUNT_REWARD_SAVE = float(
    os.getenv("DOG_BOT_HUNT_REWARD_SAVE", "12")
)

HUNT_REWARD_NEVER_CAUGHT = float(
    os.getenv("DOG_BOT_HUNT_REWARD_NEVER_CAUGHT", "20")
)

HUNT_REWARD_LAST_ELIMINATED = float(
    os.getenv(
        "DOG_BOT_HUNT_REWARD_LAST_ELIMINATED",
        "8",
    )
)

HUNT_REP_SURVIVED = 3
HUNT_REP_PRIMARY_TARGET = 5
HUNT_REP_SAVE = 5

# ------------------------------------------------------------
# ПЕРСИСТЕНТНЫЙ МИР / ИНВЕНТАРЬ ЭКИПАЖА (активно, морская 3.0)
# ------------------------------------------------------------
#
# В отличие от блока HOUNDS/RIDERS выше, это живые настройки:
# LLM генерирует мир и стартовое снаряжение экипажа один раз при
# начале Охоты; дальше LLM только предлагает продолжения, а код
# (dogbot/hunt_world.py + database.py) проверяет и фиксирует, что
# из этого канонично. HUNT_HEAL_ITEM_AMOUNT и HUNT_MAX_INVENTORY_ITEMS
# напрямую участвуют в бою через HuntMixin._hunt_use_item. Мир
# хранится как append-only факты: старое утверждение никогда не
# стирается, но новый факт с тем же fact_key перекрывает его при
# чтении.

HUNT_START_LOCATION_KEY = "start"

HUNT_STARTING_HP = 100

HUNT_BARE_HANDS_DAMAGE = 6

HUNT_HEAL_ITEM_AMOUNT = 30

HUNT_MAX_INVENTORY_ITEMS = 6

# Легаси: ограничение генерации локаций 2.0 (движение между
# локациями удалено в 3.0.5); константа больше не читается.
HUNT_MAX_LOCATIONS_PER_HUNT = 40


# ============================================================
# HELPERS
# ============================================================

def safe_json_loads(value, default):
    if not value:
        return default

    try:
        result = json.loads(value)
        return result
    except (
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ):
        return default


def extract_json_object(content):
    """
    Надёжно достаёт JSON-объект из ответа модели.
    Не использует жадный {.*}.
    """
    if not isinstance(content, str):
        return None

    content = content.strip()

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    start = content.find("{")

    if start < 0:
        return None

    depth = 0
    in_string = False
    escaped = False

    for i in range(start, len(content)):
        ch = content[i]

        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False

            continue

        if ch == '"':
            in_string = True

        elif ch == "{":
            depth += 1

        elif ch == "}":
            depth -= 1

            if depth == 0:
                candidate = content[start:i + 1]

                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    return None

    return None


def normalize_answer(text):
    """
    Нормализация ответа викторины для быстрого Python exact-match.

    ВАЖНО: должна давать РОВНО тот же результат для одного и того же
    смысла, откуда бы строка ни пришла — из ответа генерирующей LLM
    (см. llm.py:generate_llm_quiz, где answers уже приводятся к
    casefold + ё->е при СОЗДАНИИ викторины) и из реального сообщения
    пользователя (эта функция, на КАЖДЫЙ входящий ответ). Раньше
    здесь не было ё->е — LLM-ответ "еж" (после её собственной
    ё->е-нормализации) не совпадал с пользовательским "ёж", хотя это
    один и тот же ответ. Приводим оба пути к одному виду.

    Остаётся НАМЕРЕННО простым (без словоформ/семантики) — это
    быстрый бесплатный путь; смысловые совпадения ("это Пушкин" при
    ответе "Пушкин", склонения и т.п.) для не-точных случаев решает
    отдельный LLM judge (см. judge_quiz_answer в llm.py), а не эта
    функция.
    """
    text = str(text).casefold().strip()

    # ё/е — тот же символ для целей викторины (см. docstring выше).
    text = text.replace("ё", "е")

    # Дефисы/тире всех видов -> обычный дефис, а не просто убрать,
    # чтобы "кто-то" не схлопнулось в "ктото" и не потеряло разделение
    # слов — визуально разные тире (-, –, —) не должны считаться
    # разными ответами.
    text = re.sub(r"[\u2010-\u2015\u2212]", "-", text)

    text = re.sub(r"\s+", " ", text)

    # Окружающая пунктуация/кавычки (в т.ч. «ёлочки» и “типографские”
    # кавычки, которые strip(" \"'") не берёт как отдельные символы).
    text = text.strip(
        " \t\r\n.,!?;:()[]{}\"'«»“”‘’…-"
    )

    return text


# Вводные фразы вроде "это Пушкин"/"ответ: Пушкин" — при точном
# совпадении ПОСЛЕ удаления такого префикса это тот же ответ, что и
# "Пушкин" без обвязки. re.search в answer_matches() ниже и так
# находит эталонный ответ как подстроку с границей слова (так что
# "это Пушкин" уже матчится на "Пушкин" через основной путь) — префиксы
# здесь снимаются ДОПОЛНИТЕЛЬНО, чтобы normalized == candidate тоже
# срабатывал напрямую, без опоры только на подстроковый поиск.
_ANSWER_PREFIX_RE = re.compile(
    r"^(это|ответ|отвечаю|наверное|по[- ]моему|я думаю|думаю)"
    r"[\s:,-]+",
    re.IGNORECASE,
)


def _strip_answer_prefix(normalized):
    stripped = _ANSWER_PREFIX_RE.sub("", normalized, count=1)
    return stripped.strip() or normalized


# ============================================================
# ЧИСЛОВЫЕ ОТВЕТЫ ВИКТОРИНЫ
#
# Баг: вопрос про погрешность округления 0.1 в double (правильное
# значение ~5.551115123125783e-18). Эталонный ответ от
# generate_llm_quiz и полный десятичный ответ игрока
# ("0.0000000000000000055511151231257827021181583404541015625")
# — ОДНО И ТО ЖЕ число, но текстово не совпадают ни посимвольно, ни
# как подстрока (см. answer_matches ниже). Это ушло на LLM judge
# (llm.py:judge_quiz_answer), а на числах такого масштаба (18+
# значащих нулей) LLM сам путается в порядке величины — что и
# подтвердилось: собственный текст бота в этом же диалоге назвал
# погрешность "5.55 на десять в минус семнадцатой", хотя верно
# "в минус восемнадцатой". Судья на глаз считает нули не надёжнее
# любой другой LLM — а начисление денег не должно зависеть от того,
# правильно ли модель посчитала количество нулей.
#
# Поэтому для чисел используем точное математическое сравнение в
# Python (быстрый бесплатный путь, как и остальной answer_matches),
# а не текстовое/LLM-сравнение. Понимает научную нотацию как через
# "e"/"E", так и через "×10^"/"*10**"/"x10^".
# ============================================================

_SCI_NOTATION_RE = re.compile(
    r"[×xX*]\s*10\s*(?:\^|\*\*)\s*([+-]?\d+)"
)

_NUMBER_RE = re.compile(
    r"[+-]?\d+(?:[.,]\d+)?(?:[eE][+-]?\d+)?"
)


def _extract_number(text):
    """
    Пытается найти и разобрать число в тексте (в т.ч. "5.55e-18",
    "5.55×10^-18", "5.55*10**-18", с окружающими словами/знаками
    вроде "≈"/"это"/"около"). Возвращает float, либо None, если в
    тексте нет распознаваемого числа.

    Намеренно не пытается разобрать ВЕСЬ текст как одно число —
    берёт первое числоподобное вхождение, этого достаточно для
    ответов вида "это 5.55e-18" или самого числа целиком.
    """
    if text is None:
        return None

    s = str(text).strip()

    if not s:
        return None

    # Юникод-минус и похожие тире -> обычный дефис, иначе "5×10−18"
    # не разберётся как отрицательный показатель.
    s = re.sub(r"[\u2010-\u2015\u2212]", "-", s)

    # "×10^-18" / "*10**-18" / "x10^18" -> "e-18" / "e18", чтобы
    # float() понял научную нотацию, записанную не через "e".
    s = _SCI_NOTATION_RE.sub(lambda m: f"e{m.group(1)}", s)

    match = _NUMBER_RE.search(s)

    if not match:
        return None

    token = match.group(0).replace(",", ".")

    try:
        return float(token)
    except ValueError:
        return None


def _numbers_match(a, b, rel_tol=1e-2):
    # abs_tol намеренно НЕ задаём (остаётся 0.0 по умолчанию у
    # math.isclose): у чисел вроде 5.55e-18 сама величина меньше
    # почти любого разумного abs_tol, и малейший ненулевой abs_tol
    # тогда сравнивает "все достаточно маленькие числа примерно
    # равны друг другу" — то есть 5.55e-17 (неверный порядок
    # величины) прошёл бы как совпадение с 5.55e-18 (верным). Чистого
    # rel_tol достаточно для сравнения двух ненулевых чисел; на
    # практике оба сравниваемых значения — реальные ответы викторины,
    # а не результат вычислений с плавающей точкой, которым нужен
    # запас на погрешность округления.
    return math.isclose(a, b, rel_tol=rel_tol)


def has_parseable_number(text):
    """Есть ли в тексте хоть одно распознаваемое число."""
    return _extract_number(text) is not None


def all_answers_are_numeric(answers):
    """
    True, только если КАЖДЫЙ эталонный ответ в списке — число (см.
    _extract_number). Используется в llm.py:judge_quiz_answer как
    предохранитель ПЕРЕД вызовом LLM judge: если вопрос чисто
    числовой, а в ответе пользователя вообще нет ни одного числа,
    это заведомо не может быть верным ответом — незачем тратить
    LLM-вызов и незачем доверять judge'у угадывать это на глаз (см.
    комментарий выше про ложное срабатывание на нерелевантный текст).
    """
    if not answers:
        return False

    return all(_extract_number(a) is not None for a in answers)


def answer_matches(text, answers):
    """
    Проверяет ответ викторины. Быстрый, бесплатный, БЕЗ обращения к
    LLM — это первый (и для очевидных ответов единственный) уровень
    проверки. Смысловые совпадения, которые этот exact/normalized
    matcher не ловит (синонимы, непрямые формулировки, серьёзные
    словоформы), закрывает отдельный LLM judge — см. вызывающий код
    в core.py:resolve_quiz_answer, не эта функция.

    Числовые ответы (см. блок выше) сравниваются математически, а не
    текстово — LLM-эталон и полная десятичная запись пользователя
    могут быть одним и тем же числом в разной форме записи.
    """
    normalized = normalize_answer(text)
    normalized_no_prefix = _strip_answer_prefix(normalized)
    user_number = _extract_number(text)

    for answer in answers:
        candidate = normalize_answer(answer)

        if not candidate:
            continue

        if normalized == candidate or normalized_no_prefix == candidate:
            return True

        if len(candidate) >= 4:
            if re.search(
                rf"(?<!\w){re.escape(candidate)}(?!\w)",
                normalized,
                re.IGNORECASE,
            ):
                return True

        if user_number is not None:
            candidate_number = _extract_number(answer)

            if candidate_number is not None and _numbers_match(
                user_number, candidate_number
            ):
                return True

    return False


def clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def hunt_now():
    """
    Текущее время для всей логики «Дикой Охоты» — теперь в UTC.

    Раньше здесь был datetime.now(DOG_TIMEZONE), а naive-таймстампы при
    чтении из БД (_hunt_dt) тоже интерпретировались как DOG_TIMEZONE.
    Но now_iso() — общий хелпер, которым БД помечает время строк почти
    везде в проекте, — пишет наивное datetime.now() (фактически UTC на
    любом типичном сервере/контейнере). Из-за этого расхождения любое
    время Охоты, попавшее в БД в наивном виде, читалось назад со сдвигом
    на DOG_BOT_TZ_OFFSET_HOURS часов.

    Теперь ВСЁ внутреннее планирование/сравнение/хранение времени Охоты
    идёт в UTC. DOG_TIMEZONE используется только на двух границах:
    разбор времени, введённого человеком (!охота начать 20:00 — это
    местное время экипажа), и форматирование времени для показа в
    чате — см. dogbot/hunt.py: _parse_hunt_time, _hunt_cmd_start,
    _hunt_cmd_status, _hunt_cmd_join_or_status.
    """
    return datetime.now(timezone.utc)


# ============================================================
# RATE LIMITER
# ============================================================


# ============================================================
# PRIVACY / PASSIVE CONTEXT
# ============================================================

CONTEXT_BATCH_SIZE = int(os.getenv("DOG_BOT_CONTEXT_BATCH_SIZE", "15"))
CONTEXT_BATCH_CHECK_SECONDS = int(os.getenv("DOG_BOT_CONTEXT_BATCH_CHECK_SECONDS", "5"))


# ============================================================
# SESSION / TOKEN AUTH CONFIG
# ============================================================

# Сколько секунд ждём ввода токена в личку после запроса команды.
AUTH_TOKEN_TIMEOUT = int(os.getenv("DOG_BOT_AUTH_TIMEOUT", "120"))

# Сколько секунд авторизованная сессия действует максимум, даже если
# пользователь непрерывно онлайн в комнате (доп. защита; сессия всё
# равно сбрасывается раньше при обрыве присутствия в комнате).
SESSION_MAX_AGE_SECONDS = int(
    os.getenv("DOG_BOT_SESSION_MAX_AGE", str(12 * 3600))
)

# Команды, которым всегда нужен свежий ввод токена, даже при активной
# сессии — для действий, меняющих доступ к аккаунту.
SENSITIVE_COMMANDS = {
    "!сменить_токен",
    "!отозвать",
}


# ============================================================
# LLM RETRY CONFIG ("автодозвон")
# ============================================================

# Глобальный дефолт thinking для ВСЕХ вызовов LLM (main/nickname/
# quiz/hunt-нарратив/privacy/...), которые не задают thinking_disabled
# явно сами (см. ROUTER_DISABLE_THINKING — у роутера свой отдельный
# тумблер). У DeepSeek V4 thinking включён по умолчанию; на "main"
# (MAIN_MAX_TOKENS=800) это на практике периодически съедало весь
# бюджет на reasoning_content и роняло ответ целиком ("нет
# текстового content", finish_reason=length, reasoning_content_len
# 2600+) — бот молчал ~30-90 с (все ретраи) и в итоге не отвечал.
# Персонажу-боту для короткой реплики глубокое рассуждение не нужно
# (см. рекомендацию DeepSeek: non-thinking для latency-sensitive/
# high-volume чата), поэтому по умолчанию thinking выключен везде.
# Резолвится в call_openrouter(): если вызывающий код НЕ передал
# thinking_disabled (оставил None) — берётся это значение; если
# передал явно (True/False, как делает router.py) — используется
# именно оно, этот дефолт игнорируется.
# Верни "0"/"false", если захочешь вернуть thinking по умолчанию
# (например, сменишь провайдера/модель, или поднимешь MAIN_MAX_TOKENS
# с большим запасом и захочешь более "продуманные" ответы ценой
# риска повторения этой же проблемы на редких сообщениях).
LLM_THINKING_DISABLED_DEFAULT = (
    os.getenv("DOG_BOT_LLM_DISABLE_THINKING", "1")
    .strip()
    .lower()
    not in ("0", "false", "no", "off")
)

# Сколько раз пробуем запрос к LLM, прежде чем сдаться.
LLM_RETRY_MAX_ATTEMPTS = int(
    os.getenv("DOG_BOT_LLM_RETRY_MAX_ATTEMPTS", "4")
)

# Пауза перед 2-й попыткой (секунды). Каждая следующая пауза
# умножается на LLM_RETRY_BACKOFF_FACTOR, до потолка LLM_RETRY_MAX_DELAY.
LLM_RETRY_BASE_DELAY = float(
    os.getenv("DOG_BOT_LLM_RETRY_BASE_DELAY", "1.5")
)
LLM_RETRY_BACKOFF_FACTOR = float(
    os.getenv("DOG_BOT_LLM_RETRY_BACKOFF_FACTOR", "2.0")
)
LLM_RETRY_MAX_DELAY = float(
    os.getenv("DOG_BOT_LLM_RETRY_MAX_DELAY", "20")
)

# Таймаут самого HTTP-запроса тоже растёт с каждой попыткой
# (вдруг сервис просто медленный, а не лежит), до потолка.
LLM_RETRY_TIMEOUT_GROWTH = float(
    os.getenv("DOG_BOT_LLM_RETRY_TIMEOUT_GROWTH", "1.5")
)
LLM_RETRY_MAX_TIMEOUT = float(
    os.getenv("DOG_BOT_LLM_RETRY_MAX_TIMEOUT", "60")
)

# Жёсткий потолок суммарного времени на все попытки одного запроса —
# защита от того, чтобы один запрос "автодозванивался" бесконечно
# и забивал очередь остальным.
LLM_RETRY_MAX_ELAPSED = float(
    os.getenv("DOG_BOT_LLM_RETRY_MAX_ELAPSED", "90")
)

# HTTP-статусы (в т.ч. "code" внутри тела ответа при HTTP 200 —
# некоторые провайдеры через OpenRouter заворачивают апстрим-ошибку
# в JSON-тело, не меняя код ответа), которые имеет смысл повторять.
LLM_RETRYABLE_STATUSES = {
    404,  # "Provider returned error" — обычно временный сбой роутинга
    408,
    409,
    425,
    429,
    500,
    502,
    503,
    504,
    520,
    521,
    522,
    523,
    524,
}


# ============================================================
# LLM DAILY-LIMIT HOLD ("подожди до сброса")
# ============================================================

# Если провайдер (например, бесплатный тариф OpenRouter) явно вернул
# "дневной лимит исчерпан" с временем сброса в заголовке
# X-RateLimit-Reset — вместо того чтобы жечь все ретраи впустую,
# ставим LLM на холд до этого времени: запросы до сброса не уходят
# вообще, только логируются.
#
# Выключи (False / "0"), если сменишь провайдера (например, на
# DeepSeek) — там этот формат ошибки не подойдёт, и хочется, чтобы
# 429 снова обрабатывался как обычная ретраящаяся ошибка.
LLM_HOLD_UNTIL_RESET_ENABLED = (
    os.getenv(
        "DOG_BOT_LLM_HOLD_UNTIL_RESET",
        "true",
    )
    .strip()
    .lower()
    not in ("0", "false", "no", "off")
)


# ============================================================
# LLM DEBUG LOGGING
# ============================================================
#
# Обычные логи (Попытка N/M, tokens: ...) не отвечают на вопрос
# "какой именно вызов (router/main/nickname/quiz/...) сейчас
# ретраится и почему content пустой" — сообщения о ретрае/паузе
# раньше не содержали tag, а на конкурентных сообщениях в чате
# несколько call_openrouter() крутятся параллельно, и их строки
# перемешиваются в одном логе. При LLM_DEBUG_ENABLED=1
# дополнительно логируется:
#   - call_id (короткий, на весь автодозвон одного запроса) и tag
#     во ВСЕХ строках call_openrouter (это добавлено всегда, не
#     только в debug-режиме — само по себе дёшево и не спамит);
#   - перед каждой попыткой: модель, max_tokens, temperature,
#     thinking (вкл/выкл), таймаут, размер сообщений в символах;
#   - при пустом content: finish_reason и длина reasoning_content
#     из ответа провайдера (объясняет ПОЧЕМУ content пуст — см.
#     ROUTER_DISABLE_THINKING) плюс сырой ответ (обрезанный).
#
# Переключается в любой момент через переменную окружения, без
# правок кода — "0"/"false"/"no"/"off" выключает.
LLM_DEBUG_ENABLED = (
    os.getenv("DOG_BOT_LLM_DEBUG", "0")
    .strip()
    .lower()
    not in ("0", "false", "no", "off")
)


# ============================================================
# АВТОНОМНАЯ ВЫДАЧА КЛИЧЕК
# ============================================================
#
# Раз в NICKNAME_CHECK_TICK_SECONDS Пёс дозированно (не более
# NICKNAME_BATCH_SIZE пользователей за тик) проверяет, кому пора
# пересмотреть кличку: с последней проверки должно пройти от
# NICKNAME_MIN_INTERVAL_DAYS до NICKNAME_MAX_INTERVAL_DAYS дней
# (порог перебрасывается заново на каждом тике, как и в
# исходном плане). Решение — за LLM, на основе фактов и
# репутации из досье пользователя.

NICKNAME_MIN_INTERVAL_DAYS = int(
    os.getenv("DOG_BOT_NICKNAME_MIN_DAYS", "5")
)
NICKNAME_MAX_INTERVAL_DAYS = int(
    os.getenv("DOG_BOT_NICKNAME_MAX_DAYS", "15")
)

# Как часто просыпается фоновый цикл проверки кличек.
NICKNAME_CHECK_TICK_SECONDS = int(
    os.getenv("DOG_BOT_NICKNAME_TICK_SECONDS", str(12 * 3600))
)

# Сколько кандидатов проверяем за один тик — чтобы не дёргать
# LLM-API по всем пользователям разом.
NICKNAME_BATCH_SIZE = int(
    os.getenv("DOG_BOT_NICKNAME_BATCH_SIZE", "3")
)

# Пауза между LLM-запросами внутри одной пачки (не упереться в
# rate limit провайдера).
NICKNAME_BATCH_PAUSE_SECONDS = float(
    os.getenv("DOG_BOT_NICKNAME_BATCH_PAUSE", "10")
)

NICKNAME_MAX_LEN = 30


# ============================================================
# АДМИН-ТОКЕН (внеочередная выдача клички по команде)
# ============================================================
#
# Позволяет администратору немедленно инициировать проверку
# конкретного пользователя, не дожидаясь фонового цикла.
# Команда с токеном принимается ТОЛЬКО в личном сообщении Псу —
# ни сама команда, ни токен никогда не звучат в общем чате и,
# как и любое содержимое личных сообщений, не попадают в БД
# (см. on_private_message / handle_admin_nickname_command).

ADMIN_TOKEN = os.getenv("DOG_BOT_ADMIN_TOKEN", "")

# Тот же пул операторов, что уже используется для админ-команд
# Дикой Охоты (HUNT_ADMIN_NICKS) — единый allowlist для
# админ-действий бота, а не отдельный список на каждую фичу.
BOT_ADMIN_NICKS = HUNT_ADMIN_NICKS


# ============================================================
# ТЕКУЩЕЕ ВРЕМЯ (программно, для router и main)
# ============================================================
#
# Раньше время нигде программно в промпт не подмешивалось. Теперь
# оба LLM-вызова (router.py:_ask_context_router и llm.py:ask_llm)
# перед КАЖДЫМ запросом заново зовут get_current_time() —
# статическая дата в system_prompt протухала бы уже на следующий
# день, а роутер без времени путается в "сегодня"/"вчера"/
# "завтра"/"через час" при классификации needs_history/needs_recall.

DOG_TIMEZONE = timezone(
    timedelta(
        hours=float(
            os.getenv("DOG_BOT_TZ_OFFSET_HOURS", "7")
        )
    )
)

_WEEKDAYS_RU = (
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
)


def get_current_time():
    """
    Текущее время в часовом поясе бота (DOG_TIMEZONE, по умолчанию
    UTC+7) — вызывается заново на каждый LLM-запрос, а не один
    раз при старте процесса.
    """
    now = datetime.now(DOG_TIMEZONE)

    offset_hours = now.utcoffset().total_seconds() / 3600

    return {
        "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "weekday": _WEEKDAYS_RU[now.weekday()],
        "timezone": f"UTC{offset_hours:+g}",
        "iso": now.isoformat(),
    }


# ============================================================
# WEB PARSER (dogbot/web.py) — чтение веб-страниц по ссылке
# ============================================================
#
# Каскад по мотивам разбора прод-лога с Руссотуристо: если автор
# прислал/упомянул конкретный URL — страница читается НАПРЯМУЮ
# (детерминированно, через URL_RE в router.py:context_precheck),
# без похода в LLM-роутер. Полноценный web_search на каждое
# сообщение НЕ делаем — это быстро вернуло бы к тем же проблемам
# с лишними запросами, которые уже решает контекст-роутер выше
# (см. WEB_SEARCH_ENABLED ниже — отдельный, по умолчанию
# выключенный тумблер).

WEB_ENABLED = (
    os.getenv("DOG_BOT_WEB_ENABLED", "1") == "1"
)

WEB_FETCH_TIMEOUT = float(
    os.getenv("DOG_BOT_WEB_FETCH_TIMEOUT", "12")
)

# Сколько символов ОЧИЩЕННОГО текста страницы максимум уходит в
# промпт — сырой HTML в LLM никогда не отдаём (см. web.py:
# _extract_content / fetch_web_page).
MAX_WEB_CHARS = int(
    os.getenv("DOG_BOT_WEB_MAX_CHARS", "6000")
)

# Не больше N ссылок из одного сообщения — иначе сообщение с
# кучей URL превращается в N параллельных HTTP-запросов и раздувает
# промпт.
MAX_WEB_URLS_PER_MESSAGE = int(
    os.getenv("DOG_BOT_WEB_MAX_URLS", "2")
)

# Жёсткий потолок на размер СКАЧИВАЕМОГО тела ответа (до всякого
# парсинга) — защита от ссылки на многогигабайтный файл.
WEB_MAX_BYTES = int(
    os.getenv("DOG_BOT_WEB_MAX_BYTES", str(2 * 1024 * 1024))
)

# Разрешённые схемы — только http(s); никаких file:///, ftp://,
# data: и т.п.
WEB_ALLOWED_SCHEMES = ("http", "https")

# Поиск в интернете (а не чтение конкретного URL) — отдельная,
# более дорогая и более редкая способность (web_open(url) vs
# web_search(query)). По умолчанию выключена: без нормального
# search-API это HTML-скрейпинг, который легко ломается при смене
# разметки провайдера — включается явно оператором.
WEB_SEARCH_ENABLED = (
    os.getenv("DOG_BOT_WEB_SEARCH_ENABLED", "0") == "1"
)

WEB_SEARCH_MAX_RESULTS = int(
    os.getenv("DOG_BOT_WEB_SEARCH_MAX_RESULTS", "4")
)

# ------------------------------------------------------------
# Клирнет-фолбэк при сбое Tor
# ------------------------------------------------------------
#
# По умолчанию ВСЕ веб/картиночные запросы (fetch_web_page,
# fetch_image_bytes, web_search) идут через TOR_PROXY — тот же
# прокси, что уже используется для вызовов OpenRouter. Но у этой
# фичи есть два типичных отказа именно через Tor: (1) целевой сайт
# сам блокирует известные Tor exit-узлы (частый случай для антибот-
# защиты/CDN — отвечает 403/429/451, это как раз и есть "не
# смог" из-за блокировки), и (2) сеть Tor просто нестабильна или
# недоступна прямо сейчас (таймаут/обрыв соединения). В обоих
# случаях по умолчанию делаем ОДИН повтор НАПРЯМУЮ, минуя Tor —
# иначе страница/картинка просто не прочитается, хотя обычным
# браузером открылась бы без проблем.
#
# ВАЖНО: клирнет-фолбэк раскрывает реальный IP бота целевому
# серверу — на этом конкретном запросе Tor-анонимность теряется.
# Это осознанный компромисс по явному запросу оператора. Если для
# конкретного деплоя это неприемлемо — выключи
# DOG_BOT_WEB_TOR_FALLBACK_ENABLED=0, тогда сбой через Tor так и
# останется сбоем (без повтора напрямую).
WEB_TOR_FALLBACK_ENABLED = (
    os.getenv("DOG_BOT_WEB_TOR_FALLBACK_ENABLED", "1") == "1"
)

# HTTP-статусы, которые ЧЕРЕЗ TOR трактуются как "скорее всего
# заблокировали exit-узел", а не как обычный ответ сервера — при
# них тоже пробуем клирнет-фолбэк, а не только при сетевых
# ошибках/таймаутах. Обычные 404/500 сюда сознательно НЕ входят:
# клирнет их не исправит (сервер и так ответил), а светить реальный
# IP бота без причины незачем.
WEB_TOR_FALLBACK_STATUSES = tuple(
    int(code)
    for code in os.getenv(
        "DOG_BOT_WEB_TOR_FALLBACK_STATUSES", "403,429,451"
    ).split(",")
    if code.strip()
)

# Автоматическое распознавание URL в сообщении — детерминировано,
# роутер тут не нужен вообще (см. router.py:context_precheck и
# web.py:extract_urls).
URL_RE = re.compile(
    r'https?://[^\s<>"\']+',
    re.IGNORECASE,
)


# ============================================================
# VISION (распознавание картинок) — dogbot/web.py + llm.py
# ============================================================
#
# В XMPP картинки почти всегда приходят как обычная http(s)-ссылка
# в теле сообщения (XEP-0363 HTTP Upload — файл заливается на
# сервер, а в чат падает просто ссылка на него с исходным именем
# файла, включая расширение). Поэтому картинка от обычной веб-
# страницы отличается ТОЧНО ТАК ЖЕ детерминированно, как и
# needs_web/web_urls — по расширению в пути ссылки, без LLM (см.
# web.py:is_image_url / split_web_urls). Роутер тут вообще не
# участвует: он не умеет "хотеть" картинку, Python решает сам.

VISION_ENABLED = (
    os.getenv("DOG_BOT_VISION_ENABLED", "1") == "1"
)

# Отдельная vision-модель, а не основная OPENROUTER_MODEL — Пёс
# по умолчанию текстовый (дешёвый) deepseek-v4-flash, распознавание
# картинок — отдельный, более дорогой вызов на другую модель,
# вызывается только когда в сообщении реально есть картинка.
VISION_MODEL = os.getenv(
    "DOG_BOT_VISION_MODEL",
    "deepseek-v4-flash-vision-exp",
)

VISION_TIMEOUT = float(
    os.getenv("DOG_BOT_VISION_TIMEOUT", "25")
)

# Бюджет вывода на ОПИСАНИЕ картинки, не на реплику Пса — сама
# реплика по-прежнему собирается основной моделью в ask_llm() по
# готовому текстовому описанию (см. llm.py:describe_image), а не
# напрямую vision-моделью.
VISION_MAX_TOKENS = int(
    os.getenv("DOG_BOT_VISION_MAX_TOKENS", "400")
)

# Не больше N картинок из одного сообщения — иначе одно сообщение
# с кучей ссылок превращается в N параллельных vision-вызовов.
MAX_IMAGES_PER_MESSAGE = int(
    os.getenv("DOG_BOT_MAX_IMAGES_PER_MESSAGE", "1")
)

# Потолок на размер СКАЧИВАЕМОГО файла картинки — картинки обычно
# крупнее HTML-страниц, поэтому лимит отдельный от WEB_MAX_BYTES,
# но всё равно жёсткий (защита от ссылки на многогигабайтный файл
# под видом картинки).
MAX_IMAGE_BYTES = int(
    os.getenv("DOG_BOT_MAX_IMAGE_BYTES", str(5 * 1024 * 1024))
)

# Только эти MIME-типы принимаются как картинка — сверяется с
# РЕАЛЬНЫМ Content-Type ответа сервера, а не с расширением в
# ссылке (расширение только решает, ПРОБОВАТЬ ли качать как
# картинку, само доверие — по заголовку ответа).
ALLOWED_IMAGE_MIME_TYPES = (
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
    "image/bmp",
)

# Расширения, по которым ссылка распознаётся как картинка —
# см. web.py:is_image_url. Известное ограничение: ссылки БЕЗ
# расширения в пути (динамические image-эндпоинты) картинками не
# считаются — можно будет добавить HEAD-проверку Content-Type
# как запасной путь, если это станет частым случаем.
IMAGE_URL_RE = re.compile(
    r"\.(jpe?g|png|gif|webp|bmp)(?:[?#]|$)",
    re.IGNORECASE,
)
