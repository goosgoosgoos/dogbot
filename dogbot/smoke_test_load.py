"""
Изолированный smoke-тест для LoadDegradationTracker и
looks_like_work_request — плавная защита от использования бота
как бесплатной техподдержки (см. блок LOAD DEGRADATION в
config.py).

Сеть недоступна, поэтому используются те же заглушки, что и в
smoke_test_quotes.py (см. smoke_stubs/ рядом с этим файлом) —
тестируется РЕАЛЬНЫЙ код dogbot/rate_limiters.py и dogbot/config.py,
а не его копия.
"""
import asyncio
import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
_STUBS_DIR = os.path.join(_PROJECT_ROOT, "smoke_stubs")

sys.path.insert(0, _STUBS_DIR)
sys.path.insert(0, _PROJECT_ROOT)

from dogbot.config import looks_like_work_request  # noqa: E402
from dogbot import rate_limiters  # noqa: E402
from dogbot.rate_limiters import LoadDegradationTracker  # noqa: E402


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


class FakeTime:
    """
    Подменяет time.monotonic() внутри dogbot.rate_limiters
    контролируемыми значениями, чтобы проверять экспоненциальный
    спад без реального sleep() на 900+ секунд.
    """

    def __init__(self, start=1000.0):
        self.now = start

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


# ============================================================
# 1) looks_like_work_request: эвристика "рабочего" сообщения
# ============================================================

check(
    "код в бэктиках считается рабочим запросом",
    looks_like_work_request(
        "глянь, тут `ip addr` что-то странное показывает"
    )
    is True,
)

check(
    "явный маркер 'не работает'/'настрой' считается рабочим запросом",
    looks_like_work_request(
        "почему не работает мост, всё настроил как ты сказал"
    )
    is True,
)

check(
    "английский технический маркер (nginx) считается рабочим запросом",
    looks_like_work_request(
        "nginx reverse proxy почему-то не проксирует"
    )
    is True,
)

check(
    "длинное сообщение с вопросом без явных техслов тоже считается",
    looks_like_work_request(
        "слушай, у меня тут всё сложно и совсем запуталось, я уже "
        "третий день бьюсь и никак не могу разобраться, объясни "
        "мне пожалуйста максимально подробно вообще с самого "
        "начала что вообще происходит, откуда это всё взялось и "
        "что мне вообще делать дальше со всем этим хозяйством?"
    )
    is True,
)

check(
    "обычный трёп НЕ считается рабочим запросом",
    looks_like_work_request("го в 20:00 фильм смотреть?") is False,
)

check(
    "короткий вопрос без техслов НЕ считается рабочим запросом",
    looks_like_work_request("а как дела?") is False,
)

check(
    "пустая строка НЕ считается рабочим запросом",
    looks_like_work_request("") is False,
)


# ============================================================
# 2) LoadDegradationTracker: рост, насыщение, спад
# ============================================================

async def run_tracker_tests():
    fake_time = FakeTime()
    original_time_module = rate_limiters.time
    # Подмена ограничена этим модулем — реальный time модуль
    # нигде больше не трогается.
    rate_limiters.time = fake_time

    try:
        tracker = LoadDegradationTracker(
            half_life_seconds=100.0,
            increment_step=0.22,
        )

        # 2a) уровень растёт монотонно от последовательных
        # "рабочих" сообщений
        level1 = await tracker.bump("user_a", weight=1.0)
        level2 = await tracker.bump("user_a", weight=1.0)
        level3 = await tracker.bump("user_a", weight=1.0)

        check(
            "уровень растёт монотонно от последовательных запросов",
            0 < level1 < level2 < level3,
        )

        check(
            "первый шаг равен increment_step "
            "(level=0 -> step*weight*(1-0))",
            abs(level1 - 0.22) < 1e-9,
        )

        # 2b) насыщение: уровень никогда не превышает 1.0, даже
        # после очень многих подряд идущих бампов
        level_sat = level3

        for _ in range(200):
            level_sat = await tracker.bump(
                "user_a", weight=1.0
            )

        check(
            "уровень насыщается и не превышает 1.0",
            level_sat <= 1.0,
        )

        check(
            "после многих бампов уровень близок к максимуму (>0.99)",
            level_sat > 0.99,
        )

        # 2c) обычный трёп (малый вес) грузит заметно слабее
        level_chat = await tracker.bump(
            "user_b", weight=0.25
        )

        check(
            "сообщение с малым весом (обычный трёп) даёт "
            "заметно меньший прирост, чем рабочий запрос",
            level_chat < level1,
        )

        # 2d) разные пользователи независимы друг от друга
        level_c_fresh = await tracker.peek("user_c")

        check(
            "новый, ранее не встречавшийся пользователь "
            "стартует с нулевого уровня",
            level_c_fresh == 0.0,
        )

        # 2e) плавный спад со временем (экспоненциальный decay,
        # а не резкий сброс)
        level_before_decay = await tracker.peek("user_a")

        fake_time.advance(100.0)  # ровно один half-life

        level_after_one_halflife = await tracker.peek("user_a")

        check(
            "после ровно одного half-life уровень падает "
            "примерно вдвое",
            abs(
                level_after_one_halflife
                - level_before_decay / 2.0
            )
            < 1e-6,
        )

        fake_time.advance(100.0 * 20)  # ещё много half-life'ов

        level_after_long_wait = await tracker.peek("user_a")

        check(
            "после долгого отсутствия активности уровень "
            "плавно спадает почти до нуля",
            level_after_long_wait < 0.001,
        )

        # 2f) уровень так и не станет отрицательным даже при
        # огромном времени ожидания (граничный случай)
        fake_time.advance(10**9)

        level_extreme_wait = await tracker.peek("user_a")

        check(
            "уровень не уходит в отрицательные значения при "
            "экстремально долгом ожидании",
            level_extreme_wait >= 0.0,
        )

        # 2g) peek() — только чтение, без побочных эффектов
        level_peek_1 = await tracker.peek("user_d")
        level_peek_2 = await tracker.peek("user_d")

        check(
            "peek() не имеет побочных эффектов "
            "(повторный peek даёт тот же результат)",
            level_peek_1 == level_peek_2 == 0.0,
        )

    finally:
        rate_limiters.time = original_time_module


asyncio.run(run_tracker_tests())


print()
print(f"passed={passed} failed={failed}")
sys.exit(1 if failed else 0)
