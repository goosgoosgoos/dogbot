"""Regression tests for quote-aware context routing and historical date flow."""
import asyncio
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(THIS_DIR)
STUBS = os.path.join(ROOT, "smoke_stubs")
sys.path.insert(0, STUBS)
sys.path.insert(0, ROOT)

from dogbot.router import DogRouterMixin  # noqa: E402


class FakeBot(DogRouterMixin):
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.nick = "Пёс"

    async def call_openrouter(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return self.response

    def _resolve_router_temporal_date(self, text, message_time=None):
        # Use the real core implementation shape only for this test's fallback
        # contract; router must pass the complete quote-aware text here.
        from datetime import datetime, timedelta
        base = datetime.fromisoformat(message_time["datetime"] if isinstance(message_time, dict) else message_time)
        if "вчера" in text.casefold():
            return (base - timedelta(days=1)).strftime("%Y-%m-%d")
        return None


def run(coro):
    return asyncio.run(coro)


# Router decides archive + exact date from the quoted historical message.
bot = FakeBot('{"needs_memory":0,"needs_history":1,"needs_chat_archive":1,"archive_target_date":"2026-09-03","archive_target_end_date":"2026-09-03"}')
result = run(bot.resolve_context_needs(
    "",
    False,
    True,
    "Пёс",
    sender="goos",
    quoted_text=">> Пёс: че там вчера было, мы с коком наебенились, не помню\n> Пёс:",
    message_time="2026-09-04 11:40:35",
))

assert result["needs_chat_archive"] is True
assert result["archive_target_date"] == "2026-09-03"
assert result["archive_target_end_date"] == "2026-09-03"
router_user = bot.calls[0][0][1]["content"]
assert "че там вчера было" in router_user
assert "[ЦИТАТА — НЕ СЛОВА ТЕКУЩЕГО АВТОРА]" in router_user
assert "direct_to_bot=1" in router_user

# If the model classifies the historical need but truncates before the date,
# only the unambiguous relative date is safely reconstructed by Python.
bot = FakeBot('{"needs_memory":0,"needs_history":1,"needs_chat_archive":1}')
result = run(bot.resolve_context_needs(
    "",
    False,
    True,
    "Пёс",
    sender="goos",
    quoted_text="> Пёс: че там вчера было",
    message_time="2026-09-04 11:40:35",
))
assert result["needs_chat_archive"] is True
assert result["archive_target_date"] == "2026-09-03"
assert result["archive_target_end_date"] == "2026-09-03"

print("quote routing: 2 passed")
