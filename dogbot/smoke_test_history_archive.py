"""
Regress-тест на новый путь needs_history -> chat_archive (см. брифинг
"self.history -> прицельный поиск по chat_archive", CHANGES.md).

Два независимых блока:

1) SQL-корректность database.py:_search_recent_chat_archive — через
   НАСТОЯЩИЙ sqlite3 (aiosqlite-стаб в песочнице запросов не исполняет
   вообще, см. smoke_test_chat_archive_sql.py — тот же паттерн здесь).
   needs_history — самый частый флаг (почти каждое сообщение), поэтому
   любая SQL-опечатка здесь бьёт по основному чату, а не по нишевому
   блоку — стоит регресс-теста не меньше, чем needs_chat_archive.

2) Поведение core.py:get_recent_history_context — фиче-флаг,
   постепенный rollout и обязательная деградация на self.history при
   любом сбое/узком результате archive-запроса.
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


# ============================================================
# 1) SQL: database.py:_search_recent_chat_archive
# ============================================================

from dogbot.database import _search_recent_chat_archive  # noqa: E402


class _SyncCursor:
    def __init__(self, cur):
        self._cur = cur

    async def fetchall(self):
        return self._cur.fetchall()


class _SyncConn:
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
    # users нужна для consent-границы, добавленной в
    # _search_recent_chat_archive (см. smoke_test_chat_archive_sql.py
    # для того же требования у _search_chat_archive).
    conn.execute(
        """
        CREATE TABLE users (
            user_id TEXT PRIMARY KEY,
            consent_at TEXT
        )
        """
    )
    now = datetime.now()
    old = (now - timedelta(minutes=90)).isoformat(timespec="seconds")
    recent1 = (now - timedelta(minutes=20)).isoformat(timespec="seconds")
    recent2 = (now - timedelta(minutes=10)).isoformat(timespec="seconds")
    recent3 = (now - timedelta(minutes=5)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO chat_archive (id, room_jid, sender_nick, body, created_at, is_command) "
        "VALUES (1, ?, ?, ?, ?, 0)",
        ("room@conf.example", "боцман", "тут было час с лишним назад", old),
    )
    conn.execute(
        "INSERT INTO chat_archive (id, room_jid, sender_nick, body, created_at, is_command) "
        "VALUES (2, ?, ?, ?, ?, 0)",
        ("room@conf.example", "кэп", "го в доту", recent1),
    )
    conn.execute(
        "INSERT INTO chat_archive (id, room_jid, sender_nick, body, created_at, is_command) "
        "VALUES (3, ?, ?, ?, ?, 0)",
        ("room@conf.example", "боцман", "го", recent2),
    )
    conn.execute(
        "INSERT INTO chat_archive (id, room_jid, sender_nick, body, created_at, is_command) "
        "VALUES (4, ?, ?, ?, ?, 0)",
        ("room@conf.example", "кок", "кэп, го тоже", recent3),
    )
    conn.execute(
        "INSERT INTO chat_archive_mentions (message_id, nick) VALUES (4, ?)",
        ("кэп",),
    )
    conn.commit()
    return conn, now


# 1a) Recency-окно исключает старую строку, но не свежие.
conn, now = make_db()
fake_self = _FakeSelf(_SyncConn(conn))
since = now - timedelta(minutes=45)
rows = run(
    _search_recent_chat_archive(
        fake_self, room_jid="room@conf.example", since=since, limit=50,
    )
)
check(
    "recency-окно (45 мин) не включает сообщение часовой давности",
    all(r["id"] != 1 for r in rows),
)
check(
    "recency-окно включает все три свежих сообщения",
    {r["id"] for r in rows} == {2, 3, 4},
)
check(
    "результат в хронологическом (возрастающем) порядке, как self.history",
    [r["id"] for r in rows] == [2, 3, 4],
)
conn.close()


# 1b) Фильтр по нику: находит и автора, и упоминание (EXISTS-ветка,
#     тот же SQL-паттерн, что однажды был сломан в _search_chat_archive
#     — см. smoke_test_chat_archive_sql.py; регресс здесь обязателен).
conn, now = make_db()
fake_self = _FakeSelf(_SyncConn(conn))
since = now - timedelta(minutes=45)
try:
    rows = run(
        _search_recent_chat_archive(
            fake_self,
            room_jid="room@conf.example",
            since=since,
            nicks=["кэп"],
            limit=50,
        )
    )
    check(
        "поиск с nicks=['кэп'] не падает с OperationalError",
        True,
    )
    check(
        "находит и сообщение автора (id=2), и упоминание (id=4), но не id=3",
        {r["id"] for r in rows} == {2, 4},
    )
except sqlite3.OperationalError as exc:
    check(f"поиск с nicks=['кэп'] не падает с OperationalError ({exc})", False)
    check("находит автора и упоминание", False)
conn.close()


# 1c) limit реально ограничивает выдачу САМЫМИ свежими строками (не
#     первыми N с начала окна) — иначе при активном чате новые реплики
#     обрезались бы, а не старые.
conn, now = make_db()
fake_self = _FakeSelf(_SyncConn(conn))
since = now - timedelta(minutes=45)
rows = run(
    _search_recent_chat_archive(
        fake_self, room_jid="room@conf.example", since=since, limit=2,
    )
)
check(
    "limit=2 берёт два САМЫХ СВЕЖИХ сообщения (id=3,4), а не первые по времени",
    [r["id"] for r in rows] == [3, 4],
)
conn.close()


# 1d) since за пределами retention сжимается до retention_cutoff, а не
#     улетает в SQL как есть (нельзя обещать больше, чем реально хранится).
conn, now = make_db()
fake_self = _FakeSelf(_SyncConn(conn))
since = now - timedelta(days=365)
rows = run(
    _search_recent_chat_archive(
        fake_self, room_jid="room@conf.example", since=since, limit=50,
    )
)
check(
    "since на год назад не падает и не тянет несуществующие древние строки "
    "(вернулись все 4 тестовые строки в пределах retention)",
    len(rows) == 4,
)
conn.close()


# 1e) Consent-граница: сообщение зарегистрированного, но НЕ давшего
#     (или отозвавшего) consent пользователя не должно попадать в
#     контекст, который уходит в LLM через needs_history/archive —
#     иначе consent для memory/dossier обходится через chat_archive
#     (тот же P1-фикс, что и в _search_chat_archive, но здесь для
#     "недавнего" archive-пути). Сообщения БЕЗ user_id (гости)
#     исключением не считаются.
conn, now = make_db()
conn.execute(
    "INSERT INTO users (user_id, consent_at) VALUES (?, ?)",
    ("consented_user", "2026-01-01T00:00:00"),
)
conn.execute(
    "INSERT INTO users (user_id, consent_at) VALUES (?, NULL)",
    ("no_consent_user",),
)
conn.execute(
    "INSERT INTO chat_archive (id, room_jid, sender_nick, user_id, body, created_at, is_command) "
    "VALUES (5, ?, ?, ?, ?, ?, 0)",
    ("room@conf.example", "соглас", "consented_user",
     "от согласившегося", (now - timedelta(minutes=3)).isoformat(timespec="seconds")),
)
conn.execute(
    "INSERT INTO chat_archive (id, room_jid, sender_nick, user_id, body, created_at, is_command) "
    "VALUES (6, ?, ?, ?, ?, ?, 0)",
    ("room@conf.example", "несоглас", "no_consent_user",
     "от НЕ согласившегося", (now - timedelta(minutes=2)).isoformat(timespec="seconds")),
)
conn.commit()
fake_self = _FakeSelf(_SyncConn(conn))
since = now - timedelta(minutes=45)
rows = run(
    _search_recent_chat_archive(
        fake_self, room_jid="room@conf.example", since=since, limit=50,
    )
)
bodies = [r["body"] for r in rows]
check(
    "consent boundary: сообщение согласившегося пользователя видно",
    any("от согласившегося" in b for b in bodies),
)
check(
    "consent boundary: сообщение НЕ согласившегося пользователя "
    "не попадает в LLM-контекст",
    not any("от НЕ согласившегося" in b for b in bodies),
)
conn.close()


# ============================================================
# 2) core.py:get_recent_history_context — флаг/rollout/деградация
# ============================================================


def _reload_core_with_env(env):
    """
    HISTORY_ARCHIVE_* читаются один раз при импорте config.py, поэтому
    для разных сценариев rollout модуль нужно перезагрузить под новым
    окружением, а не мутировать уже импортированные константы.
    """
    for key in (
        "DOG_BOT_HISTORY_ARCHIVE_ENABLED",
        "DOG_BOT_HISTORY_ARCHIVE_ROLLOUT_PERCENT",
        "DOG_BOT_HISTORY_ARCHIVE_RECENCY_MINUTES",
        "DOG_BOT_HISTORY_ARCHIVE_MAX_MESSAGES",
    ):
        os.environ.pop(key, None)
    os.environ.update(env)

    for mod in ("dogbot.core", "dogbot.config"):
        sys.modules.pop(mod, None)

    import importlib
    return importlib.import_module("dogbot.core")


class FakeDB:
    def __init__(self, responses=None, raise_exc=None):
        # responses: список возвращаемых значений, по одному на вызов
        # (позволяет сценарию "первый вызов пуст, второй — с данными").
        self.responses = list(responses or [])
        self.raise_exc = raise_exc
        self.calls = []

    async def search_recent_chat_archive(self, room, since, nicks=None, limit=None):
        self.calls.append({"room": room, "since": since, "nicks": nicks, "limit": limit})
        if self.raise_exc:
            raise self.raise_exc
        if not self.responses:
            return []
        return self.responses.pop(0)


def make_bot(core_mod, db, history=None):
    class FakeBot(core_mod.DogCoreMixin):
        def __init__(self):
            self.db = db
            self.room = "room@conf.example"
            self.history = list(history or ["vasya: го в доту", "petya (к vasya): го"])

    return FakeBot()


# Fallback = self.history МИНУС последний элемент. В реальном вызове
# (см. muc.py) self.history.append(текущее сообщение) всегда происходит
# ДО get_recent_history_context() для этого же сообщения — так что
# последний элемент это ВСЕГДА текущая реплика, которая и так отдельно
# уходит в промпт как "СЕЙЧАС ПИШЕТ" (llm.py). Раньше fallback отдавал
# self.history целиком, и текущая реплика дублировалась в промпте
# (см. P1 в разборе архитектуры) — здесь это заложено как инвариант
# теста, а не побочный эффект.
EXPECTED_FALLBACK = "\n".join(
    ["vasya: го в доту", "petya (к vasya): го"][:-1]
)

# 2a) Флаг выключен (дефолт) -> всегда self.history (минус текущее
#     сообщение), БД вообще не трогаем.
core_off = _reload_core_with_env({"DOG_BOT_HISTORY_ARCHIVE_ENABLED": "0"})
db = FakeDB(responses=[[{"sender_nick": "x", "body": "y"}]])
bot = make_bot(core_off, db)
result = run(bot.get_recent_history_context())
check(
    "HISTORY_ARCHIVE_ENABLED=0 -> результат = self.history без "
    "последнего (текущего) сообщения",
    result == EXPECTED_FALLBACK,
)
check(
    "HISTORY_ARCHIVE_ENABLED=0 -> archive-поиск НЕ вызывается ни разу",
    db.calls == [],
)


# 2b) Флаг включён, rollout=100 -> всегда архив, результат из БД.
core_on = _reload_core_with_env({
    "DOG_BOT_HISTORY_ARCHIVE_ENABLED": "1",
    "DOG_BOT_HISTORY_ARCHIVE_ROLLOUT_PERCENT": "100",
})
db = FakeDB(responses=[[
    {"sender_nick": "кэп", "body": "го в доту"},
    {"sender_nick": "боцман", "body": "го"},
]])
bot = make_bot(core_on, db)
result = run(bot.get_recent_history_context())
check(
    "rollout=100 -> результат собран из archive-строк, а не self.history",
    result == "кэп: го в доту\nбоцман: го",
)
check("rollout=100 -> archive-поиск вызван ровно один раз", len(db.calls) == 1)


# 2c) Флаг включён, rollout=0 -> НИКОГДА не уходит в архив, несмотря на флаг.
core_r0 = _reload_core_with_env({
    "DOG_BOT_HISTORY_ARCHIVE_ENABLED": "1",
    "DOG_BOT_HISTORY_ARCHIVE_ROLLOUT_PERCENT": "0",
})
db = FakeDB(responses=[[{"sender_nick": "x", "body": "y"}]])
bot = make_bot(core_r0, db)
result = run(bot.get_recent_history_context())
check(
    "ENABLED=1 но ROLLOUT_PERCENT=0 -> всё ещё self.history-fallback "
    "(постепенный rollout, не replace-in-place)",
    result == EXPECTED_FALLBACK,
)
check("ROLLOUT_PERCENT=0 -> archive-поиск не вызывается", db.calls == [])


# 2d) Архив падает с исключением (БД недоступна и т.п.) -> тихая деградация
#     на self.history, без исключения наружу (критичный путь не должен падать).
core_on = _reload_core_with_env({
    "DOG_BOT_HISTORY_ARCHIVE_ENABLED": "1",
    "DOG_BOT_HISTORY_ARCHIVE_ROLLOUT_PERCENT": "100",
})
db = FakeDB(raise_exc=RuntimeError("БД недоступна"))
bot = make_bot(core_on, db)
try:
    result = run(bot.get_recent_history_context())
    check(
        "сбой archive-запроса -> get_recent_history_context не бросает "
        "исключение наружу",
        True,
    )
    check(
        "сбой archive-запроса -> результат = self.history-fallback",
        result == EXPECTED_FALLBACK,
    )
except Exception as exc:
    check(f"сбой archive-запроса не должен пробрасываться наружу ({exc})", False)
    check("результат = self.history при сбое", False)


# 2e) P1-фикс: узкий фильтр по конкретным участникам, который не находит
#     НИЧЕГО свежего, БОЛЬШЕ НЕ расширяется на весь чат комнаты — раньше
#     "Вася ничего не сказал" молча подменялось разговором Пети/Кати/
#     Саши, и LLM могла реконструировать ответ из чужого, не относящегося
#     к вопросу разговора (см. P1 в разборе архитектуры). Теперь 0 строк
#     по участнику = NO_RESULT, а не "покажем что есть" — вызывающая
#     сторона уходит на self.history-fallback, а НЕ на второй запрос
#     без фильтра.
core_on = _reload_core_with_env({
    "DOG_BOT_HISTORY_ARCHIVE_ENABLED": "1",
    "DOG_BOT_HISTORY_ARCHIVE_ROLLOUT_PERCENT": "100",
})
db = FakeDB(responses=[
    [],  # единственный вызов: узко по target_nick, ничего свежего
])
bot = make_bot(core_on, db)
result = run(bot.get_recent_history_context(target_nick="кто-то-неактивный"))
check(
    "узкий фильтр дал 0 строк -> НЕТ повторного запроса без фильтра "
    "по нику (раньше здесь были 2 вызова — второй без nicks)",
    len(db.calls) == 1 and db.calls[0]["nicks"] == ["кто-то-неактивный"],
)
check(
    "в итоге вернулся self.history-fallback (без текущего сообщения), "
    "а не чужой разговор комнаты",
    result == EXPECTED_FALLBACK,
)


print()
print(f"passed={passed} failed={failed}")
sys.exit(1 if failed else 0)
