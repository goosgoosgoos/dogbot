"""
Регресс-тест на реальный SQL-баг в database.py:_search_chat_archive.

История: в nick_cond была пропущена закрывающая скобка у EXISTS(...)
—

    "EXISTS (SELECT 1 FROM chat_archive_mentions cam "
    "WHERE cam.message_id = chat_archive.id AND LOWER(cam.nick) = LOWER(?)"
    # <- не хватало ")"

Баг древний и не связан напрямую с обработкой цитат, но становится
гарантированно достижимым, как только в chat-archive поиск попадает
ЛЮБОЙ ник (например quote_author при самоцитировании, или обычный
target_nick при прямом обращении к боту) — то есть практически при
каждом реальном вызове с непустым `nicks=[...]`. До сих пор в проекте
не было ни одного теста, который реально ИСПОЛНЯЕТ этот SQL (только
aiosqlite-стаб, который вообще не умеет запросов) — поэтому баг не
ловился smoke-тестами.

Здесь используется настоящий sqlite3 (stdlib, синхронный) через
тонкую async-обёртку, эмулирующую интерфейс aiosqlite ровно в том
объёме, который использует _search_chat_archive (execute/fetchall,
row_factory).
"""
import asyncio
import os
import sqlite3
import sys
from datetime import datetime, timedelta

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
_STUBS_DIR = os.path.join(_PROJECT_ROOT, "smoke_stubs")

sys.path.insert(0, _STUBS_DIR)
sys.path.insert(0, _PROJECT_ROOT)

from dogbot.database import _search_chat_archive  # noqa: E402


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


class _SyncCursor:
    def __init__(self, cur):
        self._cur = cur

    async def fetchall(self):
        return self._cur.fetchall()


class _SyncConn:
    """Тонкая async-обёртка над реальным sqlite3.Connection —
    достаточная для _search_chat_archive (execute/fetchall)."""

    def __init__(self, conn):
        self._conn = conn

    async def execute(self, sql, params=None):
        cur = self._conn.execute(sql, params or [])
        return _SyncCursor(cur)


class _FakeSelf:
    def __init__(self, conn):
        self.conn = conn


def make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE chat_archive (
            id INTEGER PRIMARY KEY,
            room_jid TEXT,
            sender_nick TEXT,
            sender_jid TEXT,
            user_id TEXT,
            body TEXT,
            created_at TEXT,
            stanza_id TEXT,
            mentions_json TEXT,
            quoted_text TEXT,
            quote_author TEXT,
            quote_stanza_id TEXT,
            is_command INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE chat_archive_mentions (
            message_id INTEGER,
            nick TEXT
        )
        """
    )
    # users нужна для consent-границы (chat_archive.user_id IS NULL
    # OR EXISTS(... consent_at IS NOT NULL ...)) — без неё любой
    # nick-фильтрованный запрос падает с "no such table: users".
    conn.execute(
        """
        CREATE TABLE users (
            user_id TEXT PRIMARY KEY,
            consent_at TEXT
        )
        """
    )
    now = datetime.now()
    yesterday = (now - timedelta(days=1)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO chat_archive "
        "(room_jid, sender_nick, body, created_at, is_command) "
        "VALUES (?, ?, ?, ?, 0)",
        ("room@conf.example", "боцман", "Пёс: че там вчера было", yesterday),
    )
    conn.execute(
        "INSERT INTO chat_archive "
        "(room_jid, sender_nick, body, created_at, is_command) "
        "VALUES (?, ?, ?, ?, 0)",
        ("room@conf.example", "кэп", "боцман, ты как всегда", yesterday),
    )
    conn.execute(
        "INSERT INTO chat_archive_mentions (message_id, nick) VALUES (2, ?)",
        ("боцман",),
    )
    conn.commit()
    return conn


# ============================================================
# 1) Поиск по sender_nick (LOWER(sender_nick) = LOWER(?)) — сам по
#    себе не требует EXISTS(...), но идёт в ТОЙ ЖЕ nick_cond-ветке,
#    поэтому один сломанный EXISTS(...) валит весь запрос целиком,
#    даже когда искомый ник — просто автор, а не упоминание.
# ============================================================
conn = make_db()
fake_self = _FakeSelf(_SyncConn(conn))

try:
    rows = run(
        _search_chat_archive(
            fake_self,
            room_jid="room@conf.example",
            nicks=["боцман"],
            limit=50,
        )
    )
    check(
        "поиск по nicks=['боцман'] не падает с OperationalError "
        "(баг: пропущенная ')' в EXISTS(...) валила ЛЮБОЙ "
        "nick-фильтрованный запрос)",
        True,
    )
    check(
        "поиск по нику находит и сообщение автора, и сообщение, "
        "где он упомянут",
        len(rows) == 2,
    )
except sqlite3.OperationalError as exc:
    check(f"поиск по nicks=['боцман'] не падает с OperationalError ({exc})", False)
    check("поиск по нику находит оба сообщения", False)

conn.close()


# ============================================================
# 2) Самоцитирование конкретно: quote_author == sender передаётся
#    в archive_nicks (см. llm.py) — тот самый путь, которым баг
#    реально достигается в проде при обработке цитат.
# ============================================================
conn = make_db()
fake_self = _FakeSelf(_SyncConn(conn))

try:
    rows = run(
        _search_chat_archive(
            fake_self,
            room_jid="room@conf.example",
            nicks=["боцман", "Пёс"],
            limit=50,
        )
    )
    check(
        "поиск с несколькими никами (как при самоцитировании: "
        "target_nick + quote_author) тоже не падает",
        True,
    )
except sqlite3.OperationalError as exc:
    check(
        f"поиск с несколькими никами не падает с OperationalError ({exc})",
        False,
    )

conn.close()


print()
print(f"passed={passed} failed={failed}")
sys.exit(1 if failed else 0)
