"""
Smoke-тест: разговор с Псом больше не требует регистрации.

Сценарий 1 (главный): незарегистрированный ("аноним") участник
обращается к Псу напрямую в MUC -> сообщение всё равно доходит до
ask_llm() и Пёс отвечает. При этом account-only side effects
(запись в фоновый батч фактов, пополнение банка) НЕ вызываются —
это то, что "жёстко завязано на регистрацию" и должно остаться
только для зарегистрированных.

Сценарий 2 (регрессия): для зарегистрированного sender'а всё
работает как раньше — ask_llm вызывается С реальным user_id, и
account-only side effects срабатывают.

Сеть недоступна, поэтому aiosqlite/aiohttp/aiohttp_socks/slixmpp
подменяются лёгкими заглушками (см. smoke_stubs/), а db/ask_llm/
router/moderation — простыми фейками прямо в этом файле: тестируем
РЕАЛЬНЫЙ dogbot/muc.py (оркестрацию), а не копию его логики.
"""
import asyncio
import os
import sys
import types
from collections import deque

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
_STUBS_DIR = os.path.join(_PROJECT_ROOT, "smoke_stubs")
sys.path.insert(0, _STUBS_DIR)
sys.path.insert(0, _PROJECT_ROOT)

from dogbot.core import DogCoreMixin  # noqa: E402
from dogbot.muc import DogMucMixin  # noqa: E402
from dogbot.moderation import DogModerationMixin  # noqa: E402
import slixmpp  # noqa: E402 (стаб)


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
    """account=None имитирует незарегистрированного/несогласившегося
    отправителя; account={"user_id": ...} — зарегистрированного."""

    def __init__(self, account):
        self.account = account
        self.record_chat_message_called = False
        self.add_to_bank_called = False
        self.add_reputation_called = False
        self.add_facts_called = False
        self.add_coins_called = False

    async def resolve_user_by_nick(self, nick):
        return self.account

    async def record_chat_archive_message(self, *a, **kw):
        return None

    async def record_chat_message(self, user_id, sender, text):
        self.record_chat_message_called = True

    async def get_inflation_multiplier(self):
        return 1.0

    async def add_to_bank(self, amount):
        self.add_to_bank_called = True

    def sanitize_facts(self, facts):
        return facts or []

    async def add_reputation(self, user_id, amount):
        self.add_reputation_called = True
        return amount

    async def add_facts(self, user_id, facts):
        self.add_facts_called = True

    async def add_coins(self, user_id, amount, **kw):
        self.add_coins_called = True


class FakeLoadTracker:
    async def bump(self, account_id, weight=1.0):
        return 0.0


class FakeRateLimiter:
    async def allow(self, sender):
        return True, None


class FakeBot(DogCoreMixin, DogMucMixin, DogModerationMixin):
    def __init__(self, db, nick="Пёс"):
        self.db = db
        self.nick = nick
        self.room = "room@muc.example.com"
        self.quote_lookback = deque(maxlen=60)
        self.history = deque(maxlen=8)
        self.engaged = False
        self.active_quiz = None
        self.sent_messages = []
        self.ask_llm_calls = []
        self.load_tracker = FakeLoadTracker()
        self.llm_rate_limiter = FakeRateLimiter()

    # --- внешние системы, не относящиеся к теме теста ---
    async def moderate_incoming_muc(self, *a, **kw):
        return False

    async def resolve_context_needs(self, *a, **kw):
        return {
            "needs_memory": False,
            "needs_history": False,
            "needs_economy": False,
            "needs_market": False,
            "about_russoturisto": False,
            "needs_recall": False,
            "needs_chat_archive": False,
        }

    async def ask_llm(self, sender, text, *a, user_id=None, **kw):
        self.ask_llm_calls.append({"sender": sender, "user_id": user_id})
        return {
            "body": "Гав, я тут!",
            "decision": 0,
            "rep_change": 5,
            "facts": [{"fact": "любит гулять", "category": "HOBBY"}],
            "create_lot": None,
            "pump_lot_id": None,
            "grant_coins": 300,  # sanitize_llm_response требует 100..1000
        }

    async def handle_commands(self, sender, text, room_jid):
        return False

    def is_addressed_to_me(self, text, target_nick):
        return True  # тестируем именно прямое обращение к Псу

    def send_message(self, mto, mbody, mtype="chat"):
        self.sent_messages.append((mto, mbody, mtype))


class FakeFrom:
    def __init__(self, bare):
        self.bare = bare


def make_msg(body, mucnick):
    return {
        "body": body,
        "mucnick": mucnick,
        "from": FakeFrom("room@muc.example.com"),
    }


async def main():
    # ========================================================
    # 1) АНОНИМ обращается напрямую -> Пёс всё равно отвечает
    # ========================================================
    db_anon = FakeDB(account=None)
    bot_anon = FakeBot(db_anon)

    await bot_anon.muc_message(
        make_msg("Пёс, привет!", "аноним")
    )

    check(
        "аноним: ask_llm вызван (Пёс отвечает без регистрации)",
        len(bot_anon.ask_llm_calls) == 1,
    )
    check(
        "аноним: ask_llm получил user_id=None",
        bot_anon.ask_llm_calls
        and bot_anon.ask_llm_calls[0]["user_id"] is None,
    )
    check(
        "аноним: Пёс реально отправил ответ в комнату",
        any(
            "Гав, я тут!" in m[1]
            for m in bot_anon.sent_messages
        ),
    )
    check(
        "аноним: record_chat_message НЕ вызывается (нет аккаунта)",
        db_anon.record_chat_message_called is False,
    )
    check(
        "аноним: add_to_bank НЕ вызывается (нет аккаунта)",
        db_anon.add_to_bank_called is False,
    )
    check(
        "аноним: сообщение попало в quote_lookback (доступен архив/"
        "контекст, его можно процитировать)",
        any(
            item["sender"] == "аноним"
            for item in bot_anon.quote_lookback
        ),
    )
    check(
        "аноним: сообщение попало в self.history (контекст для LLM)",
        any("аноним" in h for h in bot_anon.history),
    )
    check(
        "аноним: add_reputation НЕ вызывается (ask_llm вернул "
        "rep_change, но аккаунта нет)",
        db_anon.add_reputation_called is False,
    )
    check(
        "аноним: add_facts НЕ вызывается (ask_llm вернул facts, "
        "но аккаунта нет)",
        db_anon.add_facts_called is False,
    )
    check(
        "аноним: add_coins НЕ вызывается (ask_llm вернул "
        "grant_coins, но аккаунта нет)",
        db_anon.add_coins_called is False,
    )

    # ========================================================
    # 2) РЕГРЕССИЯ: зарегистрированный ведёт себя как раньше
    # ========================================================
    db_reg = FakeDB(account={"user_id": "DOG-AAAAAAAA"})
    bot_reg = FakeBot(db_reg)

    await bot_reg.muc_message(
        make_msg("Пёс, как дела?", "старожил")
    )

    check(
        "зарегистрированный: ask_llm вызван",
        len(bot_reg.ask_llm_calls) == 1,
    )
    check(
        "зарегистрированный: ask_llm получил реальный user_id",
        bot_reg.ask_llm_calls
        and bot_reg.ask_llm_calls[0]["user_id"] == "DOG-AAAAAAAA",
    )
    check(
        "зарегистрированный: record_chat_message ВЫЗЫВАЕТСЯ "
        "(фоновая память по-прежнему для аккаунтов)",
        db_reg.record_chat_message_called is True,
    )
    check(
        "зарегистрированный: add_to_bank ВЫЗЫВАЕТСЯ",
        db_reg.add_to_bank_called is True,
    )
    check(
        "зарегистрированный: add_reputation ВЫЗЫВАЕТСЯ (аккаунт есть)",
        db_reg.add_reputation_called is True,
    )
    check(
        "зарегистрированный: add_facts ВЫЗЫВАЕТСЯ (аккаунт есть)",
        db_reg.add_facts_called is True,
    )
    check(
        "зарегистрированный: add_coins ВЫЗЫВАЕТСЯ (аккаунт есть)",
        db_reg.add_coins_called is True,
    )


asyncio.run(main())
print(f"{passed} passed, {failed} failed")
if failed:
    sys.exit(1)
