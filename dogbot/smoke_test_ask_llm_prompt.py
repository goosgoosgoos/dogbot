"""
Изолированный smoke-тест для сборки промпта в ask_llm()
(dogbot/llm.py) — проверяет, что context_needs реально управляет
тем, что попадает в system_prompt/user_message, а не просто
существует как параметр.

call_openrouter подменяется на фейк, который ЗАПОМИНАЕТ переданные
messages/kwargs и возвращает None (имитация "LLM недоступна") — это
достаточно, потому что ask_llm() при пустом raw возвращает None
СРАЗУ (см. `if not raw: return None`) до любых операций записи в
БД, которые здесь не мокаются. Тестируется РЕАЛЬНАЯ сборка промпта
из dogbot/llm.py, а не её копия.
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
from dogbot.config import MAIN_MAX_TOKENS  # noqa: E402


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


class FakeDB:
    def __init__(
        self,
        memory_facts=None,
        active_lots=None,
        users_by_nick=None,
        facts_by_user=None,
    ):
        self._memory_facts = memory_facts or []
        self._active_lots = active_lots or []
        # nick -> {"user_id":..., "nickname":...} — только для
        # сценариев MENTIONED-FACTS ниже; по умолчанию пусто, и
        # тогда hasattr(self.db, "resolve_user_by_nick") в llm.py
        # всё равно True (метод есть), просто вернёт None для
        # любого ника — блок MENTIONED-FACTS сам ничего не добавит.
        self._users_by_nick = users_by_nick or {}
        self._facts_by_user = facts_by_user or {}

    async def get_user_info(self, account_id):
        return "Тестовый", 137

    async def select_memory_context(
        self,
        account_id,
        message_text=None,
        min_facts=None,
        max_facts=None,
        category=None,
    ):
        facts = self._facts_by_user.get(account_id, self._memory_facts)
        if category:
            facts = [
                f for f in facts if f.get("category") == category
            ]
        return facts

    async def resolve_user_by_nick(self, nickname):
        return self._users_by_nick.get(str(nickname))

    def get_reputation_title(self, rep):
        # Не awaited в реальном коде — сознательно синхронный.
        return "старожил"

    async def get_respect_status(self, account_id):
        return False

    async def get_bank_balance(self):
        return 54321.0

    async def get_inflation_multiplier(self):
        return 1.23

    async def get_active_lots(self):
        return self._active_lots


class FakeBot(DogLlmMixin):
    def __init__(
        self,
        memory_facts=None,
        active_lots=None,
        users_by_nick=None,
        facts_by_user=None,
    ):
        self.db = FakeDB(
            memory_facts=memory_facts,
            active_lots=active_lots,
            users_by_nick=users_by_nick,
            facts_by_user=facts_by_user,
        )
        self.nick = "Пёс"
        self.history = [
            "vasya: го в доту",
            "petya (к vasya): го",
        ]
        self.captured = None

    async def call_openrouter(self, messages, **kwargs):
        # Запоминаем ровно то, что реально ушло бы в модель, и
        # обрываем цепочку тут же — дальше ask_llm() делает запись
        # фактов/лотов в БД, которую мы не мокаем (и для этого
        # теста она не нужна).
        self.captured = {
            "messages": messages,
            "kwargs": kwargs,
        }
        return None


def run(coro):
    return asyncio.run(coro)


ALL_MINIMAL = {
    "needs_memory": False,
    "needs_history": False,
    "needs_economy": False,
    "needs_market": False,
    "about_russoturisto": False,
}

ALL_MAXIMAL = {
    "needs_memory": True,
    "needs_history": True,
    "needs_economy": True,
    "needs_market": True,
    "about_russoturisto": True,
}


# ============================================================
# 1) max_tokens реально доходит до call_openrouter
# ============================================================

bot = FakeBot()

run(
    bot.ask_llm(
        "vasya",
        "привет",
        context_needs=ALL_MINIMAL,
    )
)

check(
    "ask_llm передаёт max_tokens=MAIN_MAX_TOKENS в call_openrouter",
    bot.captured["kwargs"].get("max_tokens") == MAIN_MAX_TOKENS,
)

check(
    "ask_llm помечен tag='main'",
    bot.captured["kwargs"].get("tag") == "main",
)


# ============================================================
# 2) Минимальные context_needs -> тяжёлые блоки отсутствуют
# ============================================================

bot = FakeBot(memory_facts=[{"fact": "любит доту", "category": "HOBBY"}])

run(
    bot.ask_llm(
        "vasya",
        "как дела вообще",
        context_needs=ALL_MINIMAL,
    )
)

minimal_system = bot.captured["messages"][0]["content"]
minimal_user = bot.captured["messages"][1]["content"]

check(
    "needs_memory=False -> абзац памяти в dossier отсутствует",
    "ПОМНИШЬ О ПОЛЬЗОВАТЕЛЕ" not in minimal_system
    and "ПАМЯТЬ:" not in minimal_system,
)

check(
    "about_russoturisto=False -> абзац дружбы отсутствует",
    "ПОСТОЯННЫЙ ФАКТ О ТЕБЕ" not in minimal_system,
)

check(
    "needs_history=False, нет target_nick, но базовая история "
    "чата ВСЁ РАВНО фундаментальный default -> блок ОБРАЩЕНИЯ И "
    "ТЕГИ присутствует (есть context из self.history)",
    "ОБРАЩЕНИЯ И ТЕГИ" in minimal_system,
)

check(
    "needs_history=False БОЛЬШЕ НЕ ЛИШАЕТ модель базовой истории "
    "разговора -> история чата попадает в user_message даже когда "
    "роутер решил needs_history=False (см. ТЗ: needs_history "
    "управляет только ДОПОЛНИТЕЛЬНЫМ контекстом, а не базовым)",
    "го в доту" in minimal_user,
)

check(
    "needs_economy=False -> состояние экономики отсутствует",
    "СОСТОЯНИЕ ЭКОНОМИКИ" not in minimal_system,
)

check(
    "needs_market=False -> блок рынка отсутствует",
    "АКТИВНЫЕ ЛОТЫ" not in minimal_system and "РЫНОК:" not in minimal_system,
)

# Универсальные правила (facts/create_lot/JSON-схема, тон по
# репутации) — ДОЛЖНЫ остаться даже при минимальных context_needs:
# facts[] описывает запись НОВЫХ фактов из ТЕКУЩЕГО сообщения, это
# не то же самое, что needs_memory (чтение СТАРЫХ фактов) — гасить
# их по needs_memory значило бы тихо сломать обучение памяти на
# большинстве сообщений.
check(
    "правила facts/create_lot остаются даже при needs_memory=False "
    "(это ЗАПИСЬ новых фактов, не чтение старых)",
    "create_lot" in minimal_system and '"facts"' in minimal_system,
)

check(
    "ОТНОШЕНИЕ К ПОЛЬЗОВАТЕЛЮ (тон по репутации) остаётся всегда — "
    "репутация в dossier показывается независимо от context_needs",
    "ОТНОШЕНИЕ К ПОЛЬЗОВАТЕЛЮ" in minimal_system,
)


# ============================================================
# 3) Максимальные context_needs -> все блоки присутствуют
# ============================================================

bot = FakeBot(
    memory_facts=[{"fact": "любит доту", "category": "HOBBY"}],
    active_lots=[
        {
            "id": 1,
            "item_name": "секрет",
            "base_price": 500.0,
            "multiplier": 1.1,
        }
    ],
)

run(
    bot.ask_llm(
        "vasya",
        "как дела вообще",
        target_nick="petya",
        context_needs=ALL_MAXIMAL,
    )
)

maximal_system = bot.captured["messages"][0]["content"]
maximal_user = bot.captured["messages"][1]["content"]

check(
    "needs_memory=True + есть факты -> попадают в dossier",
    "любит доту" in maximal_system,
)

check(
    "about_russoturisto=True -> абзац дружбы присутствует",
    "ПОСТОЯННЫЙ ФАКТ О ТЕБЕ" in maximal_system,
)

check(
    "target_nick задан -> блок ОБРАЩЕНИЯ И ТЕГИ присутствует",
    "ОБРАЩЕНИЯ И ТЕГИ" in maximal_system,
)

check(
    "needs_history=True -> история чата попала в user_message",
    "го в доту" in maximal_user,
)

check(
    "needs_economy=True -> состояние экономики присутствует",
    "СОСТОЯНИЕ ЭКОНОМИКИ" in maximal_system,
)

check(
    "needs_market=True + есть лоты -> список лотов присутствует",
    "секрет" in maximal_system,
)


# ============================================================
# 4) Реальная разница в размере промпта (грубая прокси-метрика
#    вместо точного токенайзера — но направление и порядок
#    величины видно сразу).
# ============================================================

minimal_len = len(minimal_system) + len(minimal_user)
maximal_len = len(maximal_system) + len(maximal_user)

check(
    f"минимальный контекст короче максимального "
    f"({minimal_len} vs {maximal_len} символов)",
    minimal_len < maximal_len,
)




# ============================================================
# 5) КРИТИЧЕСКИЙ REGRESSION: адресат != автор.
#    Пёс всегда отвечает отправителю, даже если отправитель
#    обращается к другому участнику по имени.
# ============================================================

bot = FakeBot()

run(
    bot.ask_llm(
        "arthoriapendragon",
        "russoturisto, слушай, а оно точно не ебанёт?",
        target_nick="russoturisto",
        context_needs=ALL_MAXIMAL,
    )
)

routing_system = bot.captured["messages"][0]["content"]
routing_user = bot.captured["messages"][1]["content"]

check(
    "явно зафиксировано правило: Пёс отвечает АВТОРУ, "
    "а не адресату",
    "ТЫ ВСЕГДА ОТВЕЧАЕШЬ" in routing_system
    and "АВТОРУ ТЕКУЩЕГО СООБЩЕНИЯ" in routing_system,
)

check(
    "в структурированном сообщении автор остаётся arthoriapendragon",
    "Автор: arthoriapendragon" in routing_user,
)

check(
    "получатель ответа Пса программно закреплён за автором",
    "Получатель ответа Пса: arthoriapendragon" in routing_user,
)

check(
    "russoturisto передан именно как адресат, а не как автор",
    "Адресат: russoturisto" in routing_user
    and "Автор: russoturisto" not in routing_user,
)

check(
    "для модели явно запрещено считать, что адресат написал "
    "текущую реплику",
    "нельзя приписывать эту реплику russoturisto" in routing_system,
)


# ============================================================
# 6) Второй реальный регресс: goos сообщает о фиксе, адресуя
#    russoturisto. Это всё равно сообщение goos.
# ============================================================

bot = FakeBot()

run(
    bot.ask_llm(
        "goos",
        "russoturisto: фикс накатил, ща должно при упоминании корректно вытягивать",
        target_nick="russoturisto",
        context_needs=ALL_MAXIMAL,
    )
)

routing_system2 = bot.captured["messages"][0]["content"]
routing_user2 = bot.captured["messages"][1]["content"]

check(
    "пример с фиксом: Автор=goos, Адресат=russoturisto",
    "Автор: goos" in routing_user2
    and "Адресат: russoturisto" in routing_user2,
)

check(
    "пример с фиксом: получатель ответа Пса — goos",
    "Получатель ответа Пса: goos" in routing_user2,
)

check(
    "пример с фиксом сохраняет правило: адресат — не автор "
    "и не источник текущей реплики",
    "НЕ тот человек,\nчьи слова нужно считать текущими"
    in routing_system2
    and "НЕ\nтот, кому принадлежит текущая реплика"
    in routing_system2,
)

# ============================================================
# 7) БАГ ИЗ ОБРАЩЕНИЯ ПОЛЬЗОВАТЕЛЯ: самоцитирование ("боцман"
#    цитирует своё же вчерашнее "Пёс: ..." и снова зовёт бота).
#    Раньше при quote_author == sender модель получала
#    противоречивое "эти слова принадлежат Х, а не Х" — цитата
#    на практике игнорировалась. Теперь формулировка должна
#    явно называть это СВОИМИ словами автора, без "а не {sender}".
# ============================================================

bot = FakeBot()

run(
    bot.ask_llm(
        "боцман",
        "",
        target_nick="Пёс",
        quoted_text="Пёс: че там вчера было, мы с коком наебенились, не помню",
        quote_author="боцман",
        context_needs=ALL_MAXIMAL,
    )
)

self_quote_system = bot.captured["messages"][0]["content"]
self_quote_user = bot.captured["messages"][1]["content"]

check(
    "самоцитирование: НЕТ противоречивой фразы 'а не боцман'",
    "а не боцман" not in self_quote_system,
)

check(
    "самоцитирование: явно помечено как 'СВОЁ ЖЕ' сообщение автора",
    "СВОЁ ЖЕ" in self_quote_system,
)

check(
    "самоцитирование: quote_block помечен как собственный текст "
    "автора, а не 'ЧУЖОЙ'",
    "СОБСТВЕННЫЙ более" in self_quote_user
    and "ЧУЖОЙ текст" not in self_quote_user,
)

check(
    "самоцитирование: сам текст цитаты всё равно передан модели "
    "дословно",
    "че там вчера было" in self_quote_user,
)

check(
    "самоцитирование: собственный текст автора пуст (просто позвал "
    "бота) -> добавлена явная подсказка отвечать по цитате как "
    "по своей же более ранней",
    "ТОЛЬКО свою же" in self_quote_system
    and "старую цитату" in self_quote_system,
)


# ============================================================
# 8) Регресс-проверка: обычная (НЕ само-) цитата чужих слов
#    по-прежнему помечается как "ЧУЖОЙ" текст, формулировка не
#    сломана изменением из пункта 7.
# ============================================================

bot = FakeBot()

run(
    bot.ask_llm(
        "арторя",
        "во дела, а он мне так и не ответил",
        quoted_text="капитан скоро будет, ждите на палубе",
        quote_author="кэп",
        context_needs=ALL_MAXIMAL,
    )
)

other_quote_system = bot.captured["messages"][0]["content"]
other_quote_user = bot.captured["messages"][1]["content"]

check(
    "обычная цитата чужих слов: по-прежнему 'принадлежат кэп, а не арторя'",
    "принадлежат кэп" in other_quote_system
    and "а не арторя" in other_quote_system,
)

check(
    "обычная цитата чужих слов: quote_block по-прежнему помечен "
    "как ЧУЖОЙ",
    "ЧУЖОЙ текст" in other_quote_user,
)

check(
    "обычная цитата: собственный текст автора непустой -> "
    "подсказка про пустой текст НЕ добавляется",
    "ТОЛЬКО цитату" not in other_quote_system
    and "ТОЛЬКО свою же" not in other_quote_system,
)


# ============================================================
# 12) MENTIONED-FACTS: роутер решает нужен ли факт об упомянутом
#     и какой категории — не "раз кто-то упомянут, тянем что
#     найдётся" (см. router.py needs_mention_facts/
#     mention_fact_category и llm.py, блок MENTIONED-FACTS).
# ============================================================

bot = FakeBot(
    users_by_nick={
        "kolya": {"user_id": "u_kolya", "nickname": "kolya"},
    },
    facts_by_user={
        "u_kolya": [
            {
                "fact": "чинит видеокарты за еду",
                "category": "TECH",
                "date": "2026-08-01",
            },
            {
                "fact": "не любит доту",
                "category": "HOBBY",
                "date": "2026-07-15",
            },
        ],
    },
)

run(
    bot.ask_llm(
        "vasya",
        "а kolya вообще шарит в железе?",
        context_needs={
            **ALL_MINIMAL,
            "needs_mention_facts": True,
            "mention_fact_category": "TECH",
        },
        mentioned_nicks=["kolya"],
    )
)

mention_system = bot.captured["messages"][0]["content"]

check(
    "needs_mention_facts=True + category=TECH -> в dossier попал "
    "именно TECH-факт, а не HOBBY",
    "чинит видеокарты за еду" in mention_system
    and "не любит доту" not in mention_system,
)

check(
    "MENTIONED-FACTS: факт помечен как 'другой человек', "
    "с категорией и датой, не смешан с ДОСЬЕ автора",
    "КОРОТКО ПРО kolya" in mention_system
    and "[TECH, 2026-08-01]" in mention_system,
)


# --- упомянутый есть в комнате, но фактов о нём нет вообще ---

bot = FakeBot(
    users_by_nick={
        "senya": {"user_id": "u_senya", "nickname": "senya"},
    },
    facts_by_user={"u_senya": []},
)

run(
    bot.ask_llm(
        "vasya",
        "senya вроде программист?",
        context_needs={
            **ALL_MINIMAL,
            "needs_mention_facts": True,
            "mention_fact_category": None,
        },
        mentioned_nicks=["senya"],
    )
)

no_facts_system = bot.captured["messages"][0]["content"]

check(
    "MENTIONED-FACTS: фактов нет -> явное 'не выдумывай', "
    "а не тишина",
    "ПРО senya КОНКРЕТНЫХ ФАКТОВ НЕТ" in no_facts_system
    and "не выдумывай подробности" in no_facts_system,
)


# --- needs_mention_facts=False (роутер решил, что не нужно) ---
# несмотря на то, что мentioned_nicks непустой -> блок вообще
# не должен обращаться к БД / попадать в промпт.

bot = FakeBot(
    users_by_nick={
        "kolya": {"user_id": "u_kolya", "nickname": "kolya"},
    },
    facts_by_user={
        "u_kolya": [
            {"fact": "чинит видеокарты", "category": "TECH", "date": "2026-08-01"},
        ],
    },
)

run(
    bot.ask_llm(
        "vasya",
        "kolya сегодня в сети?",
        context_needs={
            **ALL_MINIMAL,
            "needs_mention_facts": False,
        },
        mentioned_nicks=["kolya"],
    )
)

skipped_system = bot.captured["messages"][0]["content"]

check(
    "needs_mention_facts=False -> блок MENTIONED-FACTS молчит, "
    "даже если есть mentioned_nicks и факты в БД реально есть",
    "КОРОТКО ПРО kolya" not in skipped_system
    and "КОНКРЕТНЫХ ФАКТОВ НЕТ" not in skipped_system,
)


# ============================================================
# CONTEXT RELIABILITY: базовая история — фундаментальный default,
# который НЕ зависит от needs_history (см. ТЗ, часть "ПЁС ТЕРЯЕТ
# КОНТЕКСТ"). Пункты 11-16 из требуемого списка тестов.
# ============================================================

# 11) needs_history=False на обычном сообщении -> базовая история
#     всё равно присутствует (уже проверено выше через minimal_user,
#     дублируем здесь под явным номером для читаемости отчёта).
bot = FakeBot()
run(
    bot.ask_llm(
        "vasya",
        "го",
        context_needs={**ALL_MINIMAL, "needs_history": False},
    )
)
check(
    "11) router needs_history=False -> basic history всё равно "
    "присутствует в user_message",
    "го в доту" in bot.captured["messages"][1]["content"],
)

# 12) needs_history отсутствует вовсе (router failure -> muc.py/
#     ask_llm получают context_needs=None -> ask_llm использует
#     собственный failsafe-дефолт needs_history=True) -> история
#     присутствует. Раньше при needs_history=False она бы пропала
#     ВНЕ ЗАВИСИМОСТИ от того, как получилось это False — сбоем
#     роутера или его осознанным решением; теперь оба случая
#     безопасны одинаково.
bot = FakeBot()
run(
    bot.ask_llm(
        "vasya",
        "го",
        context_needs=None,
    )
)
check(
    "12) context_needs=None (как при сбое роутера до вызова "
    "ask_llm) -> basic history присутствует",
    "го в доту" in bot.captured["messages"][1]["content"],
)

# 13) "router malformed JSON" на уровне ask_llm неотличимо от (11) —
#     resolve_context_needs (router.py) в этом случае уже сам
#     подставляет _FAILSAFE_DEFAULTS (needs_history=True, см.
#     smoke_test_router.py: "битый ответ роутера -> ... failsafe
#     True") ДО того, как context_needs дойдёт до ask_llm. Здесь же
#     дополнительно проверяем инвариант ask_llm на случай, если
#     когда-нибудь в resolved всё же окажется needs_history=False
#     (например, будущий баг в резолвере) — базовая история и тогда
#     не должна пропасть, это и есть весь смысл фикса.
bot = FakeBot()
run(
    bot.ask_llm(
        "vasya",
        "го",
        context_needs={**ALL_MAXIMAL, "needs_history": False},
    )
)
check(
    "13) needs_history=False даже среди прочих needs_*=True "
    "(гипотетический баг резолвера) -> basic history всё равно "
    "присутствует",
    "го в доту" in bot.captured["messages"][1]["content"],
)

# 14) Дополнительный retrieval (needs_chat_archive БЕЗ конкретной
#     даты -> НЕ historical_archive_mode, см. llm.py) продолжает
#     работать одновременно с базовой историей — они не взаимно
#     исключающие.
bot = FakeBot()
run(
    bot.ask_llm(
        "vasya",
        "го",
        context_needs={**ALL_MINIMAL, "needs_chat_archive": True},
    )
)
check(
    "14) needs_chat_archive=True без даты -> НЕ historical_archive_"
    "mode -> базовая история всё равно в user_message",
    "го в доту" in bot.captured["messages"][1]["content"],
)

# 15) Обычное сообщение (без всяких флагов needs_*) не теряет
#     предыдущий разговор — использует дефолтный context_needs
#     ask_llm (needs_history=True), как и было бы при прямом вызове
#     в обход muc.py/router.py.
bot = FakeBot()
run(bot.ask_llm("vasya", "продолжаем?"))
check(
    "15) обычное сообщение без context_needs не теряет предыдущий "
    "разговор",
    "го в доту" in bot.captured["messages"][1]["content"],
)

# 16) historical_archive_mode — ЕДИНСТВЕННОЕ легитимное исключение,
#     где базовая self.history сознательно подавляется (архив с
#     конкретной датой заменяет её, чтобы не смешивать с уже
#     сгенерированными ранее ответами Пса, см. комментарий в
#     llm.py). Регресс-тест на то, что это исключение НЕ было
#     случайно снесено вместе с остальным фиксом.
bot = FakeBot()
run(
    bot.ask_llm(
        "vasya",
        "что было вчера",
        context_needs={
            **ALL_MINIMAL,
            "needs_chat_archive": True,
            "archive_target_date": "2026-09-03",
            "archive_target_end_date": "2026-09-03",
        },
    )
)
check(
    "16) historical_archive_mode (needs_chat_archive + конкретная "
    "дата) остаётся единственным исключением -> self.history НЕ "
    "подмешивается в user_message",
    "го в доту" not in bot.captured["messages"][1]["content"],
)

# Контроль на "тест всегда зелёный": при действительно пустой
# истории (и без target_nick) блок ОБРАЩЕНИЯ И ТЕГИ по-прежнему
# должен отсутствовать — фикс не превращает `if target_nick or
# context` в вечное True, а честно отражает, есть ли реальный
# контекст.
bot = FakeBot()
bot.history = []
run(
    bot.ask_llm(
        "vasya",
        "го",
        context_needs=ALL_MINIMAL,
    )
)
check(
    "контроль: пустая self.history и нет target_nick -> блок "
    "ОБРАЩЕНИЯ И ТЕГИ по-прежнему отсутствует (не всегда True)",
    "ОБРАЩЕНИЯ И ТЕГИ" not in bot.captured["messages"][0]["content"],
)


print(f"\n{passed} passed, {failed} failed")

if failed:
    sys.exit(1)
