"""Регрессия DB archive: дата + поисковые термы цитаты + SQL mentions."""
import asyncio
import sqlite3
import sys
import types

THIS = __import__('os').path.dirname(__import__('os').path.abspath(__file__))
ROOT = __import__('os').path.dirname(THIS)
sys.path.insert(0, __import__('os').path.join(ROOT, 'smoke_stubs'))
sys.path.insert(0, ROOT)

# database.py imports aiosqlite; the project smoke stub is enough because
# this test supplies its own async connection adapter.
from dogbot.database import UserFactsDB


class AsyncCursor:
    def __init__(self, cursor):
        self.cursor = cursor

    async def fetchone(self):
        return self.cursor.fetchone()

    async def fetchall(self):
        return self.cursor.fetchall()


class AsyncConn:
    def __init__(self):
        self.raw = sqlite3.connect(':memory:')
        self.raw.row_factory = sqlite3.Row

    async def execute(self, sql, params=()):
        return AsyncCursor(self.raw.execute(sql, params))

    async def executemany(self, sql, params=()):
        self.raw.executemany(sql, params)
        return AsyncCursor(self.raw.execute('SELECT 1'))

    async def commit(self):
        self.raw.commit()


async def main():
    db = UserFactsDB.__new__(UserFactsDB)
    db.conn = AsyncConn()

    await db.conn.execute('''
        CREATE TABLE chat_archive (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_jid TEXT NOT NULL,
            sender_nick TEXT NOT NULL,
            sender_jid TEXT,
            user_id TEXT,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL,
            stanza_id TEXT,
            mentions_json TEXT,
            quoted_text TEXT,
            quote_author TEXT,
            quote_stanza_id TEXT,
            is_command INTEGER DEFAULT 0
        )
    ''')
    await db.conn.execute('''
        CREATE TABLE chat_archive_mentions (
            message_id INTEGER NOT NULL,
            nick TEXT NOT NULL
        )
    ''')
    # users нужна для consent-границы в _search_chat_archive
    # (chat_archive.user_id IS NULL OR EXISTS(... consent_at IS
    # NOT NULL ...)) — раньше архив никак не проверял согласие,
    # и сообщение отозвавшего consent пользователя всё равно
    # уходило в LLM-контекст через archive (см. P1 в разборе).
    await db.conn.execute('''
        CREATE TABLE users (
            user_id TEXT PRIMARY KEY,
            consent_at TEXT
        )
    ''')
    await db.conn.commit()

    await db.conn.execute('''
        INSERT INTO chat_archive
        (room_jid,sender_nick,body,created_at,mentions_json)
        VALUES (?,?,?,?,?)
    ''', ('room', 'goos', 'Пёс: да вчера было, мы с коком наебенились', '2026-09-03T10:00:00', '[]'))
    await db.conn.execute('''
        INSERT INTO chat_archive
        (room_jid,sender_nick,body,created_at,mentions_json)
        VALUES (?,?,?,?,?)
    ''', ('room', 'Пёс', 'Я ничего не помню', '2026-09-03T10:01:00', '[]'))
    await db.conn.commit()

    rows = await db.search_chat_archive(
        'room',
        target_date='2026-09-03',
        target_end_date='2026-09-03',
        nicks=['Пёс'],
        terms=['наебенились', 'коком'],
        limit=20,
        neighbor_days=0,
    )
    assert rows, 'archive search returned no rows'
    assert any('наебенились' in r['body'] for r in rows), rows
    print('OK   date window + quote terms find the cited historical episode')

    # The exact SQL path that previously crashed with "near ORDER" must
    # also work when a nick filter produces the chat_archive_mentions EXISTS.
    await db.conn.execute(
        'INSERT INTO chat_archive_mentions(message_id,nick) VALUES (?,?)',
        (1, 'Пёс'),
    )
    await db.conn.commit()
    rows = await db.search_chat_archive(
        'room', target_date='2026-09-03', target_end_date='2026-09-03',
        nicks=['Пёс'], terms=[], limit=20, neighbor_days=0,
    )
    assert rows, 'mentions EXISTS query returned no rows'
    print('OK   mentions EXISTS SQL is syntactically valid')

    # ------------------------------------------------------------
    # Consent-граница (P1-фикс): сообщение зарегистрированного, но
    # НЕ давшего (или отозвавшего) consent пользователя не должно
    # попадать в archive-контекст, который уходит в LLM — иначе
    # consent для memory/dossier обходится через chat_archive.
    # Сообщения БЕЗ user_id (гости, не имеющие аккаунта вовсе)
    # исключением не считаются и по-прежнему проходят.
    # ------------------------------------------------------------
    await db.conn.execute(
        "INSERT INTO users (user_id, consent_at) VALUES (?, ?)",
        ('consented_user', '2026-01-01T00:00:00'),
    )
    await db.conn.execute(
        "INSERT INTO users (user_id, consent_at) VALUES (?, NULL)",
        ('no_consent_user',),
    )
    await db.conn.execute('''
        INSERT INTO chat_archive
        (room_jid,sender_nick,user_id,body,created_at,mentions_json)
        VALUES (?,?,?,?,?,?)
    ''', ('room', 'соглас', 'consented_user',
          'секретный рецепт бормотухи от согласившегося',
          '2026-09-03T10:02:00', '[]'))
    await db.conn.execute('''
        INSERT INTO chat_archive
        (room_jid,sender_nick,user_id,body,created_at,mentions_json)
        VALUES (?,?,?,?,?,?)
    ''', ('room', 'несоглас', 'no_consent_user',
          'секретный рецепт бормотухи от НЕ согласившегося',
          '2026-09-03T10:03:00', '[]'))
    await db.conn.commit()

    rows = await db.search_chat_archive(
        'room', target_date='2026-09-03', target_end_date='2026-09-03',
        nicks=None, terms=['бормотухи'], limit=20, neighbor_days=0,
    )
    bodies = [r['body'] for r in rows]
    assert any('от согласившегося' in b for b in bodies), (
        'consented user message should be visible', bodies
    )
    assert not any('от НЕ согласившегося' in b for b in bodies), (
        'no-consent user message leaked into LLM-visible archive', bodies
    )
    print('OK   consent boundary: no-consent user message excluded, '
          'consented user message still visible')


if __name__ == '__main__':
    asyncio.run(main())
