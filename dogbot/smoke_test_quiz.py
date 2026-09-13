"""
Изолированный smoke-тест для викторины (dogbot/config.py:
normalize_answer/answer_matches, dogbot/llm.py:judge_quiz_answer,
dogbot/core.py:try_win_quiz/resolve_quiz_answer).

Как и smoke_test_router.py — использует локальные заглушки из
smoke_stubs/, но импортирует и тестирует РЕАЛЬНЫЙ код dogbot/*.py.
call_openrouter подменяется предсказуемым фейком (запоминает вызовы
и отдаёт заранее заданный "ответ judge"), поэтому реальная сеть не
нужна — тестируется логика мерджа exact match / LLM judge /
атомарного try_win_quiz, а не HTTP.

Покрывает обязательный список тестов из ТЗ:
  1. exact match
  2. регистр
  3. ё/е
  4. лишняя пунктуация
  5. "это Пушкин" против "Пушкин"
  6. exact mismatch -> LLM judge
  7. judge=1 -> правильный ответ
  8. judge=0 -> неправильный ответ
  9. judge возвращает мусор -> безопасная обработка
  10. два одновременных правильных ответа -> только один победитель
плюс несколько дополнительных регресс-тестов (см. ниже).
"""
import asyncio
import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
_STUBS_DIR = os.path.join(_PROJECT_ROOT, "smoke_stubs")

sys.path.insert(0, _STUBS_DIR)
sys.path.insert(0, _PROJECT_ROOT)

from dogbot.config import (  # noqa: E402
    normalize_answer,
    answer_matches,
    parse_quiz_judge_verdict,
    has_parseable_number,
    all_answers_are_numeric,
)
from dogbot.core import DogCoreMixin  # noqa: E402
from dogbot.llm import DogLlmMixin  # noqa: E402
from dogbot.rate_limiters import SlidingWindowRateLimiter  # noqa: E402


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
    def __init__(self, accounts=None, bank=100000.0):
        self.accounts = (
            accounts if accounts is not None else {}
        )
        self.bank = bank
        self.transfers = []

    async def resolve_user_by_nick(self, nick):
        # Реалистичный await (а не мгновенный синхронный возврат) —
        # без этого в тестах гонки (см. ниже) обе "параллельные"
        # asyncio-таски выполнялись бы полностью последовательно, ни
        # разу не уступая друг другу управление, и race_lost-ветка
        # никогда бы не воспроизводилась.
        await asyncio.sleep(0)
        return self.accounts.get(nick)

    async def transfer_bank_to_user(
        self, user_id, amount, transaction_type=None, description=None
    ):
        actual = min(float(amount), self.bank)
        self.bank -= actual
        self.transfers.append((user_id, actual))
        return actual


class FakeQuizBot(DogCoreMixin, DogLlmMixin):
    """
    Голый носитель нужных для quiz-флоу методов (try_win_quiz/
    resolve_quiz_answer из DogCoreMixin, judge_quiz_answer/
    call_openrouter из DogLlmMixin) — без реального XMPP/DB/HTTP.
    __init__ намеренно НЕ вызывает DogCoreMixin.__init__ (там
    поднимается slixmpp-клиент и реальная БД).
    """

    def __init__(
        self,
        accounts=None,
        canned_judge_response="__unset__",
        judge_raise=False,
        judge_delay=0.0,
        judge_side_effect=None,
    ):
        self.db = FakeDB(accounts=accounts if accounts is not None else {
            "vasya": {"user_id": "u_vasya"},
            "petya": {"user_id": "u_petya"},
        })
        self.active_quiz = None
        self.quiz_lock = asyncio.Lock()
        self.quiz_judge_rate_limiter = SlidingWindowRateLimiter(
            100, 60, 1000, 60
        )
        self.canned_judge_response = canned_judge_response
        self.judge_raise = judge_raise
        self.judge_delay = judge_delay
        self.judge_side_effect = judge_side_effect
        self.judge_calls = []

    async def call_openrouter(self, messages, **kwargs):
        self.judge_calls.append((messages, kwargs))

        if self.judge_side_effect:
            await self.judge_side_effect()

        if self.judge_delay:
            await asyncio.sleep(self.judge_delay)

        if self.judge_raise:
            raise RuntimeError("boom")

        if self.canned_judge_response == "__unset__":
            raise AssertionError(
                "judge не должен был вызываться в этом тесте"
            )

        return self.canned_judge_response


def make_quiz(answers, question_id="q1", reward=500.0, question="Кто автор?"):
    return {
        "q": question,
        "a": list(answers),
        "reward": reward,
        "question_id": question_id,
    }


# ============================================================
# ЧАСТЬ A: normalize_answer / answer_matches — чистые функции,
# без LLM, без асинхронности.
# ============================================================

# 1) exact match
check(
    "1) exact match: 'Пушкин' == 'Пушкин'",
    answer_matches("Пушкин", ["Пушкин"]),
)

# 2) регистр
check(
    "2) регистр: 'пушкин' матчит 'Пушкин'",
    answer_matches("пушкин", ["Пушкин"]),
)
check(
    "2b) регистр: 'ПУШКИН' матчит 'пушкин'",
    answer_matches("ПУШКИН", ["пушкин"]),
)

# 3) ё/е — в обе стороны (LLM могла сохранить ответ и с ё, и с е;
# пользователь мог ответить и с ё, и с е).
check(
    "3) ё/е: ответ 'ёж' матчит эталон 'еж'",
    answer_matches("ёж", ["еж"]),
)
check(
    "3b) ё/е: ответ 'еж' матчит эталон 'ёж'",
    answer_matches("еж", ["ёж"]),
)
check(
    "3c) normalize_answer('Ёж') == normalize_answer('еж')",
    normalize_answer("Ёж") == normalize_answer("еж"),
)

# 4) лишняя пунктуация / повторные пробелы / кавычки / дефисы
check(
    "4) лишняя пунктуация: 'Пушкин!!!' матчит 'Пушкин'",
    answer_matches("Пушкин!!!", ["Пушкин"]),
)
check(
    "4b) кавычки: '«Пушкин».' матчит 'Пушкин'",
    answer_matches("«Пушкин».", ["Пушкин"]),
)
check(
    "4c) повторные пробелы: 'кто-то   там' матчит 'кто-то там'",
    answer_matches("кто-то   там", ["кто-то там"]),
)
check(
    "4d) разные тире схлопываются в один вид: 'кто–то' матчит "
    "'кто-то' (обычный дефис)",
    answer_matches("кто\u2013то", ["кто-то"]),
)

# 5) "это Пушкин" против "Пушкин"
check(
    "5) вводная фраза: 'это Пушкин' матчит эталон 'Пушкин'",
    answer_matches("это Пушкин", ["Пушкин"]),
)
check(
    "5b) вводная фраза с двоеточием: 'ответ: Пушкин' матчит "
    "'Пушкин'",
    answer_matches("ответ: Пушкин", ["Пушкин"]),
)

# Отрицательный контроль: явно другой ответ НЕ матчится.
check(
    "контроль: явно неверный ответ не матчится",
    not answer_matches("Лермонтов", ["Пушкин"]),
)

# parse_quiz_judge_verdict — строгий парсер.
check(
    "parse_quiz_judge_verdict('1') -> True",
    parse_quiz_judge_verdict("1") is True,
)
check(
    "parse_quiz_judge_verdict('0') -> False",
    parse_quiz_judge_verdict("0") is False,
)
check(
    "parse_quiz_judge_verdict(' 1\\n') -> True (пробелы/переносы "
    "допустимы)",
    parse_quiz_judge_verdict(" 1\n") is True,
)
check(
    "parse_quiz_judge_verdict('да, верно') -> None (мусор)",
    parse_quiz_judge_verdict("да, верно") is None,
)
check(
    "parse_quiz_judge_verdict('{\"verdict\": 1}') -> None (не "
    "доверяем произвольному JSON)",
    parse_quiz_judge_verdict('{"verdict": 1}') is None,
)


# ============================================================
# ЧАСТЬ B: resolve_quiz_answer / try_win_quiz — полный флоу,
# включая LLM judge и атомарность.
# ============================================================

# 6) exact mismatch -> LLM judge вызывается
bot = FakeQuizBot(canned_judge_response="1")
bot.active_quiz = make_quiz(["Пушкин"])
result = run(bot.resolve_quiz_answer("vasya", "Александр Сергеевич"))
check(
    "6) exact mismatch -> LLM judge реально вызван",
    len(bot.judge_calls) == 1,
)
check(
    "6b) exact match НЕ вызывает judge вообще (см. тест 7 ниже "
    "тоже это подтверждает)",
    True,
)

# 7) judge=1 -> правильный ответ, награда начислена, викторина
#    закрыта.
check(
    "7) judge=1 -> resolve_quiz_answer вернул status=won",
    isinstance(result, dict) and result.get("status") == "won",
)
check(
    "7b) judge=1 -> награда реально переведена через db",
    len(bot.db.transfers) == 1 and bot.db.transfers[0][0] == "u_vasya",
)
check(
    "7c) judge=1 -> викторина закрыта (active_quiz=None)",
    bot.active_quiz is None,
)

# exact match НЕ должен звать judge вообще (быстрый путь).
bot2 = FakeQuizBot()  # canned_judge_response="__unset__" -> упадёт,
# если judge вызовется
bot2.active_quiz = make_quiz(["Пушкин"])
result2 = run(bot2.resolve_quiz_answer("vasya", "Пушкин"))
check(
    "1b) exact match НЕ вызывает LLM judge вообще",
    len(bot2.judge_calls) == 0,
)
check(
    "1c) exact match -> status=won и корректная награда",
    result2 is not None
    and result2.get("status") == "won"
    and len(bot2.db.transfers) == 1,
)

# 8) judge=0 -> неправильный ответ, викторина остаётся открытой,
#    награда НЕ начислена.
bot = FakeQuizBot(canned_judge_response="0")
bot.active_quiz = make_quiz(["Пушкин"])
result = run(bot.resolve_quiz_answer("vasya", "Лермонтов"))
check(
    "8) judge=0 -> resolve_quiz_answer вернул None (не победа)",
    result is None,
)
check(
    "8b) judge=0 -> викторина осталась открытой",
    bot.active_quiz is not None,
)
check(
    "8c) judge=0 -> награда НЕ начислена",
    len(bot.db.transfers) == 0,
)

# 9) judge возвращает мусор -> безопасная обработка (трактуется как
#    "не подтверждено", НЕ как победа).
for garbage in ("да", "верно!", "1 или 0, не уверен", "", "  ", "11"):
    bot = FakeQuizBot(canned_judge_response=garbage)
    bot.active_quiz = make_quiz(["Пушкин"])
    result = run(bot.resolve_quiz_answer("vasya", "Лермонтов"))
    check(
        f"9) judge вернул мусор ({garbage!r}) -> безопасно "
        "трактуется как НЕ победа",
        result is None and bot.active_quiz is not None,
    )

# judge отключён (QUIZ_JUDGE_ENABLED=0) -> не вызывается вовсе, и
# ответ, требующий семантики, просто не засчитывается (безопасный
# default), без исключений.
import dogbot.llm as llm_module  # noqa: E402

_orig_enabled = llm_module.QUIZ_JUDGE_ENABLED
llm_module.QUIZ_JUDGE_ENABLED = False
try:
    bot = FakeQuizBot()
    bot.active_quiz = make_quiz(["Пушкин"])
    result = run(bot.resolve_quiz_answer("vasya", "Александр Сергеевич"))
    check(
        "QUIZ_JUDGE_ENABLED=0 -> judge не вызывается, ответ не "
        "засчитан, исключений нет",
        result is None and len(bot.judge_calls) == 0,
    )
finally:
    llm_module.QUIZ_JUDGE_ENABLED = _orig_enabled

# Ошибка вызова judge (сеть упала) -> тоже безопасно, без победы и
# без необработанного исключения наружу.
bot = FakeQuizBot(judge_raise=True)
bot.active_quiz = make_quiz(["Пушкин"])
result = run(bot.resolve_quiz_answer("vasya", "Александр Сергеевич"))
check(
    "судья упал с исключением -> безопасно трактуется как НЕ "
    "победа, исключение не улетает наружу",
    result is None and bot.active_quiz is not None,
)

# Не-ответ (сообщение-команда или отсутствие активной викторины)
# вообще не должен трогать judge/try_win_quiz.
bot = FakeQuizBot()
bot.active_quiz = None
check(
    "нет активной викторины -> resolve_quiz_answer сразу None, "
    "без вызовов judge",
    run(bot.resolve_quiz_answer("vasya", "Пушкин")) is None
    and len(bot.judge_calls) == 0,
)

bot = FakeQuizBot()
bot.active_quiz = make_quiz(["Пушкин"])
check(
    "команда (начинается с '!') не считается попыткой ответить, "
    "даже если совпадает с эталоном",
    run(bot.resolve_quiz_answer("vasya", "!Пушкин")) is None
    and len(bot.judge_calls) == 0
    and bot.active_quiz is not None,
)


# ============================================================
# 10) Гонка: два одновременных правильных ответа -> только один
#     победитель (атомарный critical section в try_win_quiz).
# ============================================================

bot = FakeQuizBot()
bot.active_quiz = make_quiz(["Пушкин"])


async def _race_exact():
    return await asyncio.gather(
        bot.resolve_quiz_answer("vasya", "Пушкин"),
        bot.resolve_quiz_answer("petya", "Пушкин"),
    )


results = run(_race_exact())

statuses = [r.get("status") if r else None for r in results]

check(
    "10) ровно один из двух одновременных верных ответов победил",
    statuses.count("won") == 1,
)
check(
    "10b) второй участник получил race_lost, а не тихо потерялся "
    "и не выиграл тоже",
    statuses.count("race_lost") == 1,
)
check(
    "10c) награда переведена ровно один раз",
    len(bot.db.transfers) == 1,
)
check(
    "10d) викторина закрыта после гонки",
    bot.active_quiz is None,
)

# Тот же сценарий гонки, но через LLM judge (оба ответа требуют
# судью) — важно, что и здесь награда выдаётся только одному.
bot = FakeQuizBot(canned_judge_response="1")
bot.active_quiz = make_quiz(["Пушкин"])


async def _race_judge():
    return await asyncio.gather(
        bot.resolve_quiz_answer("vasya", "Александр Сергеевич"),
        bot.resolve_quiz_answer("petya", "Александр наш Сергеевич"),
    )


results = run(_race_judge())
statuses = [r.get("status") if r else None for r in results]
check(
    "10e) гонка через LLM judge -> тоже ровно один победитель",
    statuses.count("won") == 1,
)
check(
    "10f) гонка через LLM judge -> награда переведена ровно один "
    "раз",
    len(bot.db.transfers) == 1,
)


# ============================================================
# Доп. регресс-тест: судья "думал" достаточно долго, что раунд
# успел смениться НОВЫМ вопросом (question_id другой) -> просроченный
# judge=1 не должен засчитаться за новый вопрос (см.
# expected_question_id в try_win_quiz).
# ============================================================

async def _swap_quiz_mid_flight():
    bot.active_quiz = make_quiz(
        ["Другой ответ"], question_id="q2-new-round"
    )


bot = FakeQuizBot(
    canned_judge_response="1",
    judge_side_effect=_swap_quiz_mid_flight,
)
bot.active_quiz = make_quiz(["Пушкин"], question_id="q1-old-round")

result = run(bot.resolve_quiz_answer("vasya", "Александр Сергеевич"))

check(
    "просроченный judge=1 (раунд сменился, пока судья думал) -> "
    "race_lost, а не победа над НОВЫМ вопросом",
    result is not None and result.get("status") == "race_lost",
)
check(
    "просроченный judge=1 не тронул награду",
    len(bot.db.transfers) == 0,
)
check(
    "новый раунд (q2-new-round), запущенный, пока судья думал, "
    "остался нетронутым",
    bot.active_quiz is not None
    and bot.active_quiz.get("question_id") == "q2-new-round",
)


# ============================================================
# Ответ верному аккаунту, но без регистрации (no_account) —
# викторина НЕ закрывается, приз не сгорает.
# ============================================================

bot = FakeQuizBot(accounts={})  # никто не зарегистрирован
bot.active_quiz = make_quiz(["Пушкин"])
result = run(bot.resolve_quiz_answer("vasya", "Пушкин"))
check(
    "верный ответ без аккаунта -> status=no_account",
    result is not None and result.get("status") == "no_account",
)
check(
    "верный ответ без аккаунта -> викторина НЕ закрывается",
    bot.active_quiz is not None,
)


# ============================================================
# ЧАСТЬ C: числовые ответы (регресс на реальный баг — вопрос про
# погрешность округления 0.1 в double, эталон и полная десятичная
# запись пользователя это одно и то же число в разной форме записи,
# но не совпадают ни посимвольно, ни как подстрока).
# ============================================================

check(
    "11) число: полная десятичная запись матчит научную нотацию "
    "эталона (тот самый баг с 0.1 в double)",
    answer_matches(
        "0.0000000000000000055511151231257827021181583404541015625",
        ["5.551115123125783e-18"],
    ),
)
check(
    "11b) число: '×10^' нотация матчит 'e' нотацию",
    answer_matches("5.55×10^-18", ["5.551115123125783e-18"]),
)
check(
    "11c) число: обёртка словами ('это примерно ...') не мешает",
    answer_matches("это примерно 5.551115e-18", ["5.55e-18"]),
)
check(
    "11d) число: явно другой порядок величины НЕ матчится",
    not answer_matches("5.55e-17", ["5.551115123125783e-18"]),
)
check(
    "11e) число: явно другое число той же длины не матчится",
    not answer_matches("1.23e-18", ["5.551115123125783e-18"]),
)

check(
    "12) has_parseable_number: число внутри текста находится",
    has_parseable_number("это примерно 5.55e-18, наверное"),
)
check(
    "12b) has_parseable_number: в тексте без цифр числа нет",
    not has_parseable_number("Отвисло наконец-то."),
)

check(
    "13) all_answers_are_numeric: список из одних чисел -> True",
    all_answers_are_numeric(["5.551115123125783e-18"]),
)
check(
    "13b) all_answers_are_numeric: смешанный список -> False",
    not all_answers_are_numeric(["Пушкин", "5.55e-18"]),
)
check(
    "13c) all_answers_are_numeric: пустой список -> False",
    not all_answers_are_numeric([]),
)

# 14) Регресс на реальный баг: числовая викторина, ответ без единой
#     цифры и совершенно не по теме -> judge вообще не вызывается
#     (а не "судья ошибся и сказал 1") -> не засчитывается.
bot = FakeQuizBot()  # canned_judge_response="__unset__" -> упадёт,
# если judge всё-таки будет вызван
bot.active_quiz = make_quiz(
    ["5.551115123125783e-18"],
    question="Погрешность округления 0.1 в double?",
)
result = run(
    bot.resolve_quiz_answer("arthoriapendragon", "Отвисло наконец-то.")
)
check(
    "14) числовая викторина + нерелевантный ответ без цифр -> "
    "judge НЕ вызывается",
    len(bot.judge_calls) == 0,
)
check(
    "14b) числовая викторина + нерелевантный ответ без цифр -> "
    "не засчитано, викторина осталась открытой",
    result is None and bot.active_quiz is not None,
)

# 15) Тот же числовой вопрос, но точный правильный ответ в полной
#     десятичной записи -> засчитывается через быстрый exact-путь,
#     БЕЗ обращения к judge вообще (см. баг: раньше это уходило на
#     LLM judge и терялось в порядке величины).
bot = FakeQuizBot()
bot.active_quiz = make_quiz(
    ["5.551115123125783e-18"],
    question="Погрешность округления 0.1 в double?",
)
result = run(
    bot.resolve_quiz_answer(
        "vasya",
        "0.0000000000000000055511151231257827021181583404541015625",
    )
)
check(
    "15) числовой ответ в полной десятичной записи -> won, без "
    "вызова judge",
    result is not None
    and result.get("status") == "won"
    and len(bot.judge_calls) == 0,
)


print(f"\n{passed} passed, {failed} failed")

if failed:
    sys.exit(1)
