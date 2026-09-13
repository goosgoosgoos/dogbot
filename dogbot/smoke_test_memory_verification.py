"""Regression test: bot-authored archive messages must not verify memory claims."""
import asyncio
import os
import sys

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(THIS_DIR)
STUBS = os.path.join(ROOT, "smoke_stubs")
sys.path.insert(0, STUBS)
sys.path.insert(0, ROOT)

from dogbot.core import DogCoreMixin  # noqa: E402


class FakeDB:
    async def search_chat_archive(self, *args, **kwargs):
        return [
            {
                "id": 1, "sender_nick": "Пёс", "user_id": None,
                "created_at": "2026-09-04 12:00:00", "body": "Бормотуха-капитан?",
                "is_command": 0, "quote_author": None, "quoted_text": None,
            },
        ]


class FakeBot(DogCoreMixin):
    def __init__(self):
        self.db = FakeDB()
        self.room = "room"
        self.nick = "Пёс"


async def main():
    bot = FakeBot()
    block = await bot.get_chat_archive_context(
        "не, там чото про бормотуху",
        {
            "needs_chat_archive": True,
            "search_plan": {
                "enabled": True,
                "sources": ["chat_archive"],
                "queries": ["бормотух"],
                "must_verify": True,
            },
        },
    )
    assert "подтверждающих сообщений не найдено" in block
    assert "НЕ ДОКАЗАТЕЛЬСТВО" in block


asyncio.run(main())
print("memory verification: 1 passed")
