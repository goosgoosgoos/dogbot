from .config import *

class SlidingWindowRateLimiter:
    """
    Простой async-safe sliding-window rate limiter.

    Ограничивает:
      - пользователя;
      - глобальный поток.
    """

    def __init__(
        self,
        user_limit,
        user_window,
        global_limit,
        global_window,
    ):
        self.user_limit = int(user_limit)
        self.user_window = float(user_window)

        self.global_limit = int(global_limit)
        self.global_window = float(global_window)

        self.user_events = {}
        self.global_events = deque()

        self.lock = asyncio.Lock()

    @staticmethod
    def _cleanup(queue, now, window):
        cutoff = now - window

        while queue and queue[0] <= cutoff:
            queue.popleft()

    async def allow(self, user_id):
        now = time.monotonic()

        async with self.lock:
            global_queue = self.global_events

            self._cleanup(
                global_queue,
                now,
                self.global_window,
            )

            user_queue = self.user_events.setdefault(
                user_id,
                deque(),
            )

            self._cleanup(
                user_queue,
                now,
                self.user_window,
            )

            if len(global_queue) >= self.global_limit:
                return False, (
                    "Глобальный лимит обращений к мозгу Пса "
                    "временно исчерпан. Попробуй чуть позже."
                )

            if len(user_queue) >= self.user_limit:
                oldest = user_queue[0]
                wait_for = max(
                    1,
                    int(
                        self.user_window
                        - (now - oldest)
                    ),
                )

                return False, (
                    f"Слишком много запросов. "
                    f"Подожди примерно {wait_for} сек."
                )

            user_queue.append(now)
            global_queue.append(now)

            return True, None


class LoadDegradationTracker:
    """
    Плавная защита от пользователей, злоупотребляющих ботом как
    бесплатной техподдержкой/рабочей силой для LLM.

    В отличие от SlidingWindowRateLimiter (который просто режет
    частоту запросов по принципу разрешено/запрещено), здесь для
    каждого пользователя копится непрерывный "уровень нагрузки"
    0.0..1.0:

      - bump() поднимает уровень при релевантном сообщении, но
        НАСЫЩАЯСЬ — чем ближе к 1.0, тем меньше даёт следующий шаг
        (level += step * weight * (1 - level)), так что рост
        плавный и без резкого потолка;
      - при отсутствии новых сообщений уровень сам по себе
        экспоненциально спадает обратно к 0 (period = half_life:
        за это время уровень падает вдвое) — тоже плавно, без
        скачка "восстановлен/не восстановлен".

    Состояние держится только в памяти процесса (как и
    SlidingWindowRateLimiter) — это осознанно: смысл в "остывании"
    со временем, а не в вечном бане, так что переживать перезапуск
    ему и не нужно.
    """

    def __init__(self, half_life_seconds, increment_step):
        self.half_life = max(
            1.0,
            float(half_life_seconds),
        )
        self.increment_step = float(increment_step)

        # user_id -> (level: float 0..1, ts: time.monotonic() при
        # последнем обновлении)
        self._state = {}

        self.lock = asyncio.Lock()

    def _decayed(self, level, elapsed):
        if level <= 0.0 or elapsed <= 0.0:
            return level

        factor = 0.5 ** (
            elapsed / self.half_life
        )

        return level * factor

    async def bump(self, user_id, weight=1.0):
        """
        Регистрирует релевантное сообщение от user_id и возвращает
        АКТУАЛЬНЫЙ уровень нагрузки (после спада за прошедшее
        время и добавления инкремента).

        weight — множитель шага роста: > 1 для сообщений, явно
        похожих на просьбу порешать рабочую/техническую задачу,
        < 1 для обычного трёпа, который тоже дёргает LLM, но
        гораздо слабее нагружает "терпение".
        """
        now = time.monotonic()

        async with self.lock:
            level, ts = self._state.get(
                user_id,
                (0.0, now),
            )

            level = self._decayed(
                level,
                now - ts,
            )

            increment = (
                self.increment_step
                * max(0.0, float(weight))
                * (1.0 - level)
            )

            level = min(
                1.0,
                level + increment,
            )

            self._state[user_id] = (level, now)

            return level

    async def peek(self, user_id):
        """
        Текущий уровень нагрузки без побочных эффектов (спад
        применяется, но не сохраняется и не увеличивается).
        """
        now = time.monotonic()

        async with self.lock:
            level, ts = self._state.get(
                user_id,
                (0.0, now),
            )

            return self._decayed(
                level,
                now - ts,
            )


class QuizRateLimiter:
    def __init__(self, limit, window):
        self.limit = int(limit)
        self.window = float(window)
        self.events = {}
        self.lock = asyncio.Lock()

    async def allow(self, user_id):
        now = time.monotonic()

        async with self.lock:
            queue = self.events.setdefault(
                user_id,
                deque(),
            )

            cutoff = now - self.window

            while queue and queue[0] <= cutoff:
                queue.popleft()

            if len(queue) >= self.limit:
                oldest = queue[0]
                wait_for = max(
                    1,
                    int(
                        self.window
                        - (now - oldest)
                    ),
                )

                return False, (
                    f"Викторины для тебя временно "
                    f"ограничены. Подожди {wait_for} сек."
                )

            queue.append(now)

            return True, None


# ============================================================
# DATABASE
# ============================================================

