"""
Изолированный smoke-тест для резолва RECALL TARGET — "о КОМ идёт
речь", когда needs_recall=True (см. config.py: ROLE_ALIASES/
RECALL_FUZZY_*, core.py: resolve_recall_target_nick, database.py:
_best_fuzzy_nick_match, router.py: _ask_recall_target).

Как и smoke_test_router.py — использует локальные заглушки из
smoke_stubs/, но импортирует и тестирует РЕАЛЬНЫЙ код dogbot/*.py.
aiosqlite-стаб не умеет реальных запросов, поэтому уровень БД
(resolve_user_by_fuzzy_nick целиком) здесь не тестируется — только
чистая функция сопоставления (_best_fuzzy_nick_match), которая не
трогает соединение. call_openrouter подменяется предсказуемым
фейком, как и в smoke_test_router.py.

ОБНОВЛЕНО: _best_fuzzy_nick_match теперь возвращает FuzzyNickMatch
(status=FOUND/AMBIGUOUS/NOT_FOUND + .nick/.score/.runner_up_score)
вместо голого ника/None — раньше похожие ники ("Вера"/"Вера123")
молча резолвились в первого попавшегося кандидата, из-за чего
recall мог поднять досье не того человека (см. P0 в разборе
архитектуры). Тесты ниже адаптированы под новый контракт и
дополнены явным кейсом AMBIGUOUS.
"""
import asyncio
import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
_STUBS_DIR = os.path.join(_PROJECT_ROOT, "smoke_stubs")

sys.path.insert(0, _STUBS_DIR)
sys.path.insert(0, _PROJECT_ROOT)

from dogbot.config import ROLE_ALIASES  # noqa: E402
from dogbot.core import DogCoreMixin  # noqa: E402
from dogbot.database import _best_fuzzy_nick_match, FuzzyNickStatus  # noqa: E402
from dogbot.router import DogRouterMixin  # noqa: E402


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


def run(coro):
    return asyncio.run(coro)


class FakeCoreBot(DogCoreMixin):
    """
    Голый носитель нужных для resolve_recall_target_nick методов
    (find_mentioned_nick, get_room_nicks) — без реального XMPP-
    соединения. __init__ намеренно НЕ вызывает
    DogCoreMixin.__init__ (там поднимается slixmpp-клиент).
    """

    def __init__(self, nick, room_nicks):
        self.nick = nick
        self._room_nicks = list(room_nicks)

    def get_room_nicks(self):
        return list(self._room_nicks)


class FakeRouterBot(DogRouterMixin):

    def __init__(self, canned_response=None):
        self.canned_response = canned_response
        self.calls = []

    async def call_openrouter(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return self.canned_response


# ============================================================
# 1) resolve_recall_target_nick: баг с target_nick == ник бота
# ============================================================

bot = FakeCoreBot("Пёс", ["goos", "russoturisto", "Вера"])

# Автор адресует сообщение прямо боту ("Пёс: ...") — parse_mention
# отдал бы target_nick="Пёс". Раньше это ошибочно становилось
# recall_nick. Теперь должно игнорироваться и падать дальше по
# каскаду (роль "капитана" в тексте -> russoturisto).
result = bot.resolve_recall_target_nick(
    "нука напомни, какую брехню ты насобирал про нашего капитана?",
    "goos",
    target_nick="Пёс",
)

check(
    "target_nick == ник бота игнорируется, а не берётся как цель",
    result != "Пёс",
)

check(
    "...вместо этого резолвится ролевой алиас 'капитана' -> russoturisto",
    result == "russoturisto",
)


# ============================================================
# 2) resolve_recall_target_nick: явный ник другого участника
# ============================================================

result = bot.resolve_recall_target_nick(
    "слушай, а Вера вообще молодец, да?",
    "goos",
    target_nick=None,
)

check(
    "явное упоминание живого ника комнаты (в точной форме) "
    "резолвится напрямую",
    result == "Вера",
)

# Склонённая форма ("Верой", а не "Вера") НЕ ловится литеральным
# поиском — это ожидаемо и специально проверяется отдельно на
# уровне _best_fuzzy_nick_match ниже (п.7): resolve_recall_target_nick
# — только точный/детерминированный уровень каскада, склонения
# закрывает следующий уровень (fuzzy DB-поиск в database.py).
result = bot.resolve_recall_target_nick(
    "слушай, а что там было с Верой на прошлой неделе?",
    "goos",
    target_nick=None,
)

check(
    "склонённая форма ника НЕ резолвится на детерминированном "
    "уровне (ожидаемо — за это отвечает fuzzy-уровень, см. п.7)",
    result is None,
)


# ============================================================
# 3) resolve_recall_target_nick: явный чужой адресат сохраняется
# ============================================================

result = bot.resolve_recall_target_nick(
    "а ты вообще помнишь что-нибудь?",
    "goos",
    target_nick="russoturisto",
)

check(
    "явный target_nick (не бот, не автор) используется как есть",
    result == "russoturisto",
)


# ============================================================
# 4) resolve_recall_target_nick:self-reference через роль
#    исключается (человек сам является этой ролью)
# ============================================================

result = bot.resolve_recall_target_nick(
    "я же капитан, ты должен помнить про меня всё",
    "russoturisto",
    target_nick=None,
)

check(
    "ролевой алиас, указывающий на самого автора, не возвращается "
    "(recall падает дальше по каскаду, а не 'находит' автора же)",
    result is None,
)


# ============================================================
# 5) resolve_recall_target_nick: ничего не найдено -> None
# ============================================================

result = bot.resolve_recall_target_nick(
    "просто трёп ни о чём особенном",
    "goos",
    target_nick=None,
)

check(
    "нет ни адресата, ни ника, ни роли в тексте -> None "
    "(дальше по каскаду — нечёткий поиск/LLM, см. llm.py)",
    result is None,
)


# ============================================================
# 6) ROLE_ALIASES: базовые роли из character-промпта (llm.py)
#    зарегистрированы для нескольких падежей
# ============================================================

check(
    "капитан (им.п.) -> russoturisto",
    ROLE_ALIASES.get("капитан") == "russoturisto",
)

check(
    "капитаном (тв.п.) -> russoturisto",
    ROLE_ALIASES.get("капитаном") == "russoturisto",
)

check(
    "боцман -> goos",
    ROLE_ALIASES.get("боцман") == "goos",
)

check(
    "кок -> arthoriapendragon",
    ROLE_ALIASES.get("кок") == "arthoriapendragon",
)


# ============================================================
# 7) _best_fuzzy_nick_match: склонённые формы, опечатки и —
#    ключевое для P0-фикса — неоднозначные похожие ники
# ============================================================

check(
    "склонённая форма ('с Верой') резолвится в 'Вера' (FOUND)",
    _best_fuzzy_nick_match(
        "а что там было с Верой вчера",
        ["goos", "Вера", "russoturisto"],
    ).nick
    == "Вера",
)

check(
    "...и статус именно FOUND, а не AMBIGUOUS/NOT_FOUND",
    _best_fuzzy_nick_match(
        "а что там было с Верой вчера",
        ["goos", "Вера", "russoturisto"],
    ).status
    == FuzzyNickStatus.FOUND,
)

check(
    "опечатка в нике (пропущенная буква, тот же алфавит) "
    "всё ещё резолвится",
    _best_fuzzy_nick_match(
        "спроси у rusoturisto",
        ["goos", "Вера", "russoturisto"],
    ).nick
    == "russoturisto",
)

check(
    "совсем непохожий текст не резолвится ни в кого "
    "(NOT_FOUND, нет ложных совпадений)",
    _best_fuzzy_nick_match(
        "просто трёп ни о чём особенном вообще",
        ["goos", "Вера", "russoturisto"],
    ).status
    == FuzzyNickStatus.NOT_FOUND,
)

check(
    "пустой список кандидатов -> NOT_FOUND, без исключений",
    _best_fuzzy_nick_match("что угодно", []).status
    == FuzzyNickStatus.NOT_FOUND,
)

# P0-регресс: два похожих ника, между которыми модель НЕ должна
# угадывать. Раньше _best_fuzzy_nick_match тихо возвращала первого
# кандидата с максимальным score, даже если второй отставал всего
# на пару сотых (или не отставал вовсе) — recall в этом случае мог
# поднять досье не того человека. Теперь это обязано быть
# AMBIGUOUS, а не FOUND. "Максима"/"Максимк" оба дают одинаковый
# score (0.923) против токена "максим" — гарантированная ничья
# выше threshold, а не подобранный на глазок пример.
ambiguous_result = _best_fuzzy_nick_match(
    "спроси у максим об этом",
    ["Максима", "Максимк", "goos"],
)

check(
    "два кандидата с одинаковым score выше threshold -> AMBIGUOUS, "
    "а не случайный выбор одного из них",
    ambiguous_result.status == FuzzyNickStatus.AMBIGUOUS,
)

check(
    "AMBIGUOUS не считается 'найденным' в булевом контексте "
    "(bool(match) is False) — вызывающий код не может случайно "
    "принять его за FOUND через 'if match:'",
    bool(ambiguous_result) is False,
)


# ============================================================
# 8) router.py: _ask_recall_target
# ============================================================

candidates = ["goos", "Вера", "russoturisto"]

router_bot = FakeRouterBot(
    canned_response='{"target": "russoturisto"}'
)

result = run(
    router_bot._ask_recall_target(
        "вспомни, что было с нашим капитаном", candidates
    )
)

check(
    "роутер вернул валидный ник из списка кандидатов",
    result == "russoturisto",
)

router_bot = FakeRouterBot(canned_response='{"target": "self"}')

result = run(
    router_bot._ask_recall_target(
        "вспомни, что я тебе говорил", candidates
    )
)

check(
    "роутер вернул 'self' -> метод отдаёт 'self'",
    result == "self",
)

router_bot = FakeRouterBot(
    canned_response='{"target": "какой-то-левый-ник"}'
)

result = run(
    router_bot._ask_recall_target("текст", candidates)
)

check(
    "роутер вернул ник НЕ из списка кандидатов -> не доверяем, None",
    result is None,
)

router_bot = FakeRouterBot(canned_response=None)

result = run(
    router_bot._ask_recall_target("текст", candidates)
)

check(
    "сбой call_openrouter (None) -> None, без исключений",
    result is None,
)

router_bot = FakeRouterBot(
    canned_response='{"target": "russoturisto"}'
)

result = run(
    router_bot._ask_recall_target("текст", [])
)

check(
    "пустой список кандидатов -> None БЕЗ вызова call_openrouter",
    result is None and router_bot.calls == [],
)


# ============================================================
# ИТОГ
# ============================================================

print(f"\n{passed} passed, {failed} failed")

if failed:
    sys.exit(1)
