"""
Изолированный smoke-тест для LLM Context Router
(dogbot/router.py) — двухуровневой сборки контекста для ask_llm():
Python precheck (уровень 0) + дешёвый router LLM (уровень 1) только
для того, что precheck не смог решить сам.

Сеть/пакеты (slixmpp, aiohttp, aiosqlite, aiohttp_socks) недоступны
в песочнице, поэтому используются локальные заглушки из
smoke_stubs/ (см. рядом с этим файлом) — импортируется и тестируется
РЕАЛЬНЫЙ код dogbot/router.py и dogbot/config.py, а не его копия.
call_openrouter при этом подменяется на предсказуемый фейк — сама
сетевая заглушка aiohttp не умеет отвечать содержательно, да это и
не нужно: юнит-тест роутера должен проверять логику мерджа флагов,
а не HTTP.
"""
import asyncio
import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
_STUBS_DIR = os.path.join(_PROJECT_ROOT, "smoke_stubs")

sys.path.insert(0, _STUBS_DIR)
sys.path.insert(0, _PROJECT_ROOT)

from dogbot.config import is_trivial_reaction  # noqa: E402
from dogbot import router as router_module  # noqa: E402
from dogbot.router import (  # noqa: E402
    DogRouterMixin,
    ROUTER_FLAG_KEYS,
)


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


class FakeRouterBot(DogRouterMixin):
    """
    Голый носитель DogRouterMixin — только то, что нужно роутеру
    (call_openrouter), без реального DogBot/slixmpp/БД.
    """

    def __init__(self, canned_response=None, raise_error=False):
        # canned_response — либо строка (сырой content от "модели"),
        # либо None (имитирует сбой call_openrouter → None, как при
        # исчерпанных ретраях).
        self.canned_response = canned_response
        self.raise_error = raise_error
        self.calls = []

    async def call_openrouter(self, messages, **kwargs):
        self.calls.append((messages, kwargs))

        if self.raise_error:
            raise RuntimeError("симуляция сбоя сети")

        return self.canned_response


def run(coro):
    return asyncio.run(coro)


# ============================================================
# 1) is_trivial_reaction: короткие междометные реплики
# ============================================================

check(
    "'ахаха' — тривиальная реакция",
    is_trivial_reaction("ахаха") is True,
)

check(
    "'+1' — тривиальная реакция",
    is_trivial_reaction("+1") is True,
)

check(
    "'да' — тривиальная реакция",
    is_trivial_reaction("да") is True,
)

check(
    "длинное содержательное сообщение — НЕ тривиальная реакция",
    is_trivial_reaction(
        "слушай, а помнишь я говорил про новую работу?"
    )
    is False,
)

check(
    "пустая строка — не тривиальная реакция (нет текста вообще)",
    is_trivial_reaction("") is False,
)


# ============================================================
# 2) context_precheck: уровень 0, без LLM
# ============================================================

bot = FakeRouterBot()

# Тривиальная реакция — все флаги решены сразу, ничего не остаётся
# роутеру.
precheck = bot.context_precheck(
    "ахаха", False, False, None
)

check(
    "тривиальная реакция: все флаги False и решены Python'ом",
    all(precheck.get(k) is False for k in ROUTER_FLAG_KEYS),
)

# Явное упоминание russoturisto — about_russoturisto решается
# Python'ом уверенно (True), остальное остаётся неясным (None).
precheck = bot.context_precheck(
    "О, кстати, russoturisto вчера такое устроил",
    True,
    False,
    None,
)

check(
    "упоминание russoturisto -> about_russoturisto=True (Python)",
    precheck.get("about_russoturisto") is True,
)

check(
    "упоминание russoturisto -> needs_memory всё ещё неясен (None)",
    precheck.get("needs_memory") is None,
)

# Обращение к самому russoturisto по нику — тоже уверенно True.
precheck = bot.context_precheck(
    "как дела вообще",
    False,
    False,
    "russoturisto",
)

check(
    "обращение к russoturisto по нику -> about_russoturisto=True",
    precheck.get("about_russoturisto") is True,
)

# Сам russoturisto пишет (даже не упоминая себя по имени) —
# about_russoturisto тоже уверенно True: dossier в ask_llm() и без
# роутера всегда отмечает "russoturisto — твой кореш" когда он сам
# пишет, так что абзац-дружбы в system_prompt должен попасть в
# промпт синхронно с этим — иначе модель получит противоречивую
# картину (dossier говорит "кореш", а system_prompt о дружбе
# молчит).
precheck = bot.context_precheck(
    "го покидаем зомби на сервере",
    False,
    True,
    None,
    sender="russoturisto",
)

check(
    "russoturisto сам пишет -> about_russoturisto=True (sender)",
    precheck.get("about_russoturisto") is True,
)

# Точный триггер экономики — Python решает needs_economy сам.
precheck = bot.context_precheck(
    "Пёс, а сколько у меня коинов на балансе?",
    False,
    True,
    None,
)

check(
    "'сколько коинов' -> needs_economy=True (точный маркер)",
    precheck.get("needs_economy") is True,
)

check(
    "экономический вопрос -> needs_market остаётся неясным (None)",
    precheck.get("needs_market") is None,
)

# Точный триггер рынка.
precheck = bot.context_precheck(
    "го глянем активные лоты на рынке",
    False,
    True,
    None,
)

check(
    "'лоты на рынке' -> needs_market=True (точный маркер)",
    precheck.get("needs_market") is True,
)

# Не адресовано боту и не обращение по нику — фоновая реплика.
# Раньше здесь Python жёстко решал needs_history=False (история
# чата вообще не шла в промпт), из-за чего редкие спонтанные
# ответы Пса на такие реплики (см. случайный trigger в muc.py)
# получались "из ниоткуда". Теперь needs_history для фоновых
# сообщений тоже остаётся неясным и решается наравне с остальными
# полями — либо router LLM по смыслу, либо фейлсейф True.
precheck = bot.context_precheck(
    "погода сегодня так себе если что",
    False,
    False,
    None,
)

check(
    "фоновая реплика не по адресу -> needs_history неясен (None), решает роутер/фейлсейф",
    precheck.get("needs_history") is None,
)

# Обращение к боту напрямую — needs_history НЕ фиксируется
# программно. Раньше был короткий эксперимент с жёстким форсом
# needs_history=True при is_direct_to_me, но это тянуло историю и
# на "Пёс, привет"/"спасибо" просто потому, что обращение прямое.
# Теперь is_direct_to_me передаётся роутеру как ОДИН ИЗ сигналов
# для классификации (см. _ask_context_router), а не как готовый
# ответ — precheck оставляет needs_history неясным точно так же,
# как и для фоновых реплик.
precheck = bot.context_precheck(
    "а ты серьёзно только что сказал?",
    False,
    True,
    None,
)

check(
    "прямое обращение к боту -> needs_history по-прежнему неясен "
    "(None) — решает роутер, а не Python",
    precheck.get("needs_history") is None,
)


# ============================================================
# 3) resolve_context_needs: precheck решил всё -> роутер НЕ
#    вызывается вообще (самый дешёвый и самый частый путь).
# ============================================================

bot = FakeRouterBot(canned_response='{"should":"not be called"}')

resolved = run(
    bot.resolve_context_needs("ахаха", False, False, None)
)

check(
    "тривиальная реакция: call_openrouter НЕ вызван",
    len(bot.calls) == 0,
)

check(
    "тривиальная реакция: итоговые флаги все False",
    all(resolved[k] is False for k in ROUTER_FLAG_KEYS),
)


# ============================================================
# 4) resolve_context_needs: precheck оставил часть неясной ->
#    роутер вызывается ТОЛЬКО по оставшимся полям.
# ============================================================

bot = FakeRouterBot(
    canned_response=(
        '{"needs_memory": 1, "needs_history": 0}'
    )
)

# Здесь Python уверенно решает about_russoturisto (упоминание) и
# needs_economy/needs_market (точные маркеры "коинов"/"лоты") —
# неясным для роутера остаются needs_memory И needs_history
# (is_direct_to_me=True больше не фиксирует needs_history
# программно, а передаётся роутеру как сигнал — см. проверку ниже).
resolved = run(
    bot.resolve_context_needs(
        "russoturisto, ты серьёзно помнишь, что я говорил "
        "про баланс коинов и активные лоты?",
        True,
        True,
        None,
    )
)

check(
    "роутер вызван ровно один раз",
    len(bot.calls) == 1,
)

router_messages, router_kwargs = bot.calls[0]

check(
    "роутер вызван с cheap-профилем (temperature=0, малый max_tokens)",
    router_kwargs.get("temperature") == 0
    and router_kwargs.get("max_tokens") == router_module.ROUTER_MAX_TOKENS
    and router_kwargs.get("tag") == "router",
)

check(
    "роутер вызван с thinking_disabled=ROUTER_DISABLE_THINKING "
    "(без этого DeepSeek V4 тратит ROUTER_MAX_TOKENS на "
    "reasoning_content и роутер систематически возвращает "
    "пустой content, см. config.py)",
    router_kwargs.get("thinking_disabled")
    == router_module.ROUTER_DISABLE_THINKING,
)

sent_system_prompt = router_messages[0]["content"]
sent_user_prompt = router_messages[1]["content"]

check(
    "в промпт роутера НЕ попали уже решённые Python'ом поля "
    "(about_russoturisto/needs_economy/needs_market)",
    "about_russoturisto" not in sent_system_prompt
    and "needs_economy" not in sent_system_prompt
    and "needs_market" not in sent_system_prompt,
)

check(
    "needs_history ОСТАЛСЯ неясным для Python -> попал в промпт "
    "роутера (needs_history: не программный override)",
    "needs_history:" in sent_system_prompt,
)

check(
    "is_direct_to_me передан роутеру как сигнал в user-сообщении "
    "(не готовый ответ, просто факт о сообщении для классификации)",
    "Прямое обращение к боту" in sent_user_prompt
    and "да" in sent_user_prompt.split("\n\n")[0],
)

check(
    "needs_memory взят из ответа роутера (True)",
    resolved["needs_memory"] is True,
)

check(
    "needs_history взят из ответа роутера (False) — "
    "is_direct_to_me не переопределяет его вслепую",
    resolved["needs_history"] is False,
)

check(
    "needs_economy/needs_market — решены Python'ом как True "
    "(точные маркеры уже были в тексте)",
    resolved["needs_economy"] is True
    and resolved["needs_market"] is True,
)


# ============================================================
# 5) Роутер выключен (ROUTER_ENABLED=False) -> сразу дефолты,
#    без попытки вызова.
# ============================================================

original_enabled = router_module.ROUTER_ENABLED
router_module.ROUTER_ENABLED = False

try:
    bot = FakeRouterBot(canned_response='{"needs_memory": 0}')

    resolved = run(
        bot.resolve_context_needs(
            "а ты серьёзно помнишь, что я говорил про работу?",
            False,
            True,
            None,
        )
    )

    check(
        "роутер выключен -> call_openrouter не вызывается",
        len(bot.calls) == 0,
    )

    check(
        "роутер выключен -> needs_memory/needs_history падают на "
        "дореформенный дефолт True (ничего не теряем функционально)",
        resolved["needs_memory"] is True
        and resolved["needs_history"] is True,
    )

finally:
    router_module.ROUTER_ENABLED = original_enabled


# ============================================================
# 6) Сбой роутера (исключение внутри call_openrouter) -> не
#    роняет обработку сообщения, использует безопасные дефолты.
# ============================================================

bot = FakeRouterBot(raise_error=True)

resolved = run(
    bot.resolve_context_needs(
        "а ты серьёзно помнишь, что я говорил про работу?",
        False,
        True,
        None,
    )
)

check(
    "сбой роутера: needs_memory/needs_history -> failsafe True",
    resolved["needs_memory"] is True
    and resolved["needs_history"] is True,
)

check(
    "сбой роутера: needs_economy/needs_market -> failsafe False",
    resolved["needs_economy"] is False
    and resolved["needs_market"] is False,
)


# ============================================================
# 7) Роутер вернул мусор вместо JSON -> тоже дефолты, не падаем.
# ============================================================

bot = FakeRouterBot(canned_response="не JSON, а текст с рассуждениями")

resolved = run(
    bot.resolve_context_needs(
        "а ты серьёзно помнишь, что я говорил про работу?",
        False,
        True,
        None,
    )
)

check(
    "битый ответ роутера -> needs_memory/needs_history -> failsafe True",
    resolved["needs_memory"] is True
    and resolved["needs_history"] is True,
)


# ============================================================
# ИТОГ
# ============================================================

# ============================================================
# 9) Цитата участвует в выборе контекста, но не в решении отвечать.
#    Важный регресс: если author_text пуст (например, после цитаты
#    стоит только "Пёс:"), роутер всё равно должен увидеть "вчера"
#    внутри цитаты и выбрать исторический контекст.
# ============================================================
quote_routing = "[ЦИТАТА ДЛЯ КОНТЕКСТА]\nПёс: что вчера было с коком?"
precheck_quote = bot.context_precheck(
    quote_routing,
    False,
    True,
    "Пёс",
)
check(
    "временной маркер внутри цитаты -> needs_temporal=True",
    precheck_quote.get("needs_temporal") is True,
)
check(
    "маркер 'вчера' внутри цитаты -> роутер видит смысл цитаты",
    precheck_quote.get("needs_temporal") is True,
)

print(f"\n{passed} passed, {failed} failed")
if failed:
    sys.exit(1)
