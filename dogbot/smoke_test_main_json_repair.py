"""
Изолированный smoke-тест для надёжности разбора JSON основного
ответа ask_llm() (dogbot/llm.py: extract_json_object + repair-проход
_repair_main_json). Покрывает пункты 17-22 обязательного списка
тестов из ТЗ:

  17. valid JSON
  18. malformed JSON
  19. repair success
  20. repair failure
  21. degraded mode
  22. отсутствие "Чё надо?" как необоснованного fallback на
      структурной ошибке

call_openrouter подменяется фейком, который различает вызовы по
tag=... ("main" для основного вызова, "main_repair" для
repair-прохода) и отдаёт заранее заданный ответ для каждого —
реальная сеть не нужна, тестируется реальный код dogbot/llm.py.
"""
import asyncio
import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
_STUBS_DIR = os.path.join(_PROJECT_ROOT, "smoke_stubs")

sys.path.insert(0, _STUBS_DIR)
sys.path.insert(0, _PROJECT_ROOT)

from dogbot.llm import DogLlmMixin  # noqa: E402


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


class FakeDB:
    async def get_user_info(self, account_id):
        return "Тестовый", 100

    async def select_memory_context(self, *a, **kw):
        return []

    async def resolve_user_by_nick(self, nickname):
        return None

    def get_reputation_title(self, rep):
        return "старожил"

    async def get_respect_status(self, account_id):
        return False

    async def get_bank_balance(self):
        return 1000.0

    async def get_inflation_multiplier(self):
        return 1.0

    async def get_active_lots(self):
        return []


class FakeBot(DogLlmMixin):
    """
    main_response/repair_response — то, что должен "ответить"
    call_openrouter на основной вызов (tag="main") и на repair-проход
    (tag="main_repair") соответственно. None имитирует полный отказ
    LLM (как при сетевом сбое после исчерпания retry/backoff внутри
    самого call_openrouter — этот уровень тестируется отдельно, не
    здесь).
    """

    def __init__(self, main_response, repair_response=None):
        self.db = FakeDB()
        self.nick = "Пёс"
        self.history = []
        self.main_response = main_response
        self.repair_response = repair_response
        self.calls = []

    async def call_openrouter(self, messages, **kwargs):
        tag = kwargs.get("tag")
        self.calls.append(tag)
        if tag == "main_repair":
            return self.repair_response
        return self.main_response


VALID_JSON = (
    '{"body": "гав", "decision": 1, "rep_change": 0, '
    '"grant_coins": 0, "facts": [], "create_lot": null, '
    '"pump_lot_id": null}'
)

# Обрезанный на середине JSON — типичный реальный случай (модель
# упёрлась в max_tokens посреди генерации).
MALFORMED_JSON = '{"body": "гав, но тут всё оборв'


# ============================================================
# 17) valid JSON -> разобран штатно, repair не вызывается вовсе.
# ============================================================

bot = FakeBot(main_response=VALID_JSON)
result = run(bot.ask_llm("vasya", "привет"))

check(
    "17) valid JSON -> ask_llm вернул корректно разобранный dict",
    isinstance(result, dict) and result.get("body") == "гав",
)
check(
    "17b) valid JSON -> repair НЕ вызывался (только 1 вызов "
    "call_openrouter)",
    bot.calls == ["main"],
)


# ============================================================
# 18) malformed JSON -> ask_llm пытается repair (второй вызов с
#     tag=main_repair).
# ============================================================

bot = FakeBot(main_response=MALFORMED_JSON, repair_response=VALID_JSON)
result = run(bot.ask_llm("vasya", "привет"))

check(
    "18) malformed JSON -> был сделан repair-вызов "
    "(tag=main_repair)",
    "main_repair" in bot.calls,
)


# ============================================================
# 19) repair success -> ask_llm в итоге вернул валидный dict,
#     восстановленный repair-проходом.
# ============================================================

check(
    "19) repair success -> ask_llm вернул dict, восстановленный "
    "repair'ом",
    isinstance(result, dict) and result.get("body") == "гав",
)


# ============================================================
# 20) repair failure (сам repair тоже вернул мусор) -> ask_llm
#     возвращает None, а не бросает исключение.
# ============================================================

bot = FakeBot(
    main_response=MALFORMED_JSON,
    repair_response="тоже не JSON, просто текст",
)
result = run(bot.ask_llm("vasya", "привет"))

check(
    "20) repair failure -> ask_llm вернул None (без исключений)",
    result is None,
)
check(
    "20b) repair failure -> оба вызова реально были сделаны (main "
    "+ main_repair), а не пропущены",
    bot.calls == ["main", "main_repair"],
)


# ============================================================
# 21) degraded mode: repair вообще не ответил (LLM недоступна на
#     repair-проходе, call_openrouter вернул None после исчерпания
#     СВОЕГО retry/backoff) -> тот же безопасный None, без
#     исключений и без повторных repair-попыток (не бесконечный
#     цикл — ровно один repair-вызов).
# ============================================================

bot = FakeBot(main_response=MALFORMED_JSON, repair_response=None)
result = run(bot.ask_llm("vasya", "привет"))

check(
    "21) degraded mode (repair недоступен) -> ask_llm вернул None",
    result is None,
)
check(
    "21b) degraded mode -> ровно один repair-вызов, не бесконечный "
    "retry-цикл поверх retry/backoff call_openrouter",
    bot.calls.count("main_repair") == 1,
)

# Полный отказ LLM на основном вызове (raw пуст) -> ask_llm выходит
# сразу, repair вообще не нужен (нечего чинить).
bot = FakeBot(main_response=None)
result = run(bot.ask_llm("vasya", "привет"))
check(
    "21c) основной вызов вообще не ответил -> None сразу, без "
    "repair-вызова (нечего чинить)",
    result is None and "main_repair" not in bot.calls,
)


# ============================================================
# 22) "Чё надо?" НЕ появляется как fallback на структурной ошибке —
#     это ответственность более высокого уровня (muc.py:
#     sanitize_llm_response, только когда JSON СТРУКТУРНО валиден,
#     но поле body пустое/отсутствует), а не ask_llm(). ask_llm при
#     структурном сбое (после неудачного repair) возвращает None —
#     проверяем, что "Чё надо?" не появляется нигде на этом уровне.
# ============================================================

for main_resp, repair_resp in (
    (MALFORMED_JSON, "тоже мусор"),
    (MALFORMED_JSON, None),
    (None, None),
):
    bot = FakeBot(main_response=main_resp, repair_response=repair_resp)
    result = run(bot.ask_llm("vasya", "привет"))
    check(
        f"22) main={main_resp!r:.30} repair={repair_resp!r:.30} -> "
        "ask_llm НЕ подставляет 'Чё надо?' сам (либо None, либо "
        "остальное — на более высоком уровне)",
        result is None or (
            isinstance(result, dict)
            and result.get("body") != "Чё надо?"
        ),
    )


print(f"\n{passed} passed, {failed} failed")

if failed:
    sys.exit(1)
