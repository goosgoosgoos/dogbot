"""
Smoke-тест вайтлиста модерации (!вайтлист) без сети.

Проверяет:
  * доступ к команде только по подтверждённому bare JID
    (MODERATION_WHITELIST_ADMIN_JIDS), а не по нику;
  * добавление по готовому JID и по нику (через occupant-метаданные
    комнаты — тем же механизмом, что и остальная модерация);
  * список и удаление;
  * что moderate_incoming_muc пропускает вайтлист-JID даже при
    явном флуд-паттерне, и снимает прошлую фиксацию бота (fixed_bot),
    если её кто-то уже успел получить.
"""
import asyncio
import os
import sys
from collections import deque

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
_STUBS_DIR = os.path.join(_PROJECT_ROOT, "smoke_stubs")
sys.path.insert(0, _STUBS_DIR)
sys.path.insert(0, _PROJECT_ROOT)

from dogbot import moderation as moderation_module
from dogbot.moderation import DogModerationMixin

# Тестовый allowlist: мутируем тот же set-объект, что импортирован
# в moderation.py из config.py (см. MODERATION_WHITELIST_ADMIN_JIDS).
moderation_module.MODERATION_WHITELIST_ADMIN_JIDS.add("admin@example.org")


class FakeXep45:
    """
    occupants: nick -> bare jid (без resource), включая саму Пса
    под ролью модератора.
    """

    def __init__(self, occupants=None):
        self.occupants = occupants or {}
        self.occupants.setdefault("Пёс", None)

    def get_jid_property(self, room, nick, prop):
        if nick == "Пёс" and prop == "role":
            return "moderator"
        if nick == "Пёс" and prop == "affiliation":
            return "none"
        if prop == "jid":
            jid = self.occupants.get(nick)
            return f"{jid}/resource" if jid else None
        return None


class FakeXep425:
    def __init__(self):
        self.calls = []

    async def moderate(self, room, stanza_id, reason=""):
        self.calls.append((str(room), stanza_id, reason))


class FakeDB:
    """
    In-memory замена moderation_whitelist_add/remove/list —
    те же сигнатуры, что в database.py::UserFactsDB.
    """

    def __init__(self):
        self._rows = {}
        self._t = 0.0

    async def moderation_whitelist_add(self, jid, added_by):
        jid = str(jid).strip().casefold()
        self._t += 1
        self._rows[jid] = {
            "jid": jid,
            "added_by": str(added_by or "").casefold(),
            "added_at": self._t,
        }

    async def moderation_whitelist_remove(self, jid):
        jid = str(jid).strip().casefold()
        return self._rows.pop(jid, None) is not None

    async def moderation_whitelist_list(self):
        return sorted(
            self._rows.values(), key=lambda r: r["added_at"]
        )


class FakeBot(DogModerationMixin):
    def __init__(self, occupants=None):
        self.nick = "Пёс"
        self.quote_lookback = deque(maxlen=60)
        self.xep45 = FakeXep45(occupants)
        self.xep425 = FakeXep425()
        self.plugin = {
            "xep_0045": self.xep45,
            "xep_0425": self.xep425,
        }
        self.sent = []
        self.db = FakeDB()
        self.moderation_init()

    def extract_stanza_id(self, msg):
        return msg.get("id")

    def send_message(self, **kwargs):
        self.sent.append(kwargs)

    async def classify_possible_bot(self, *a, **kw):
        # LLM недоступна/не настроена в тесте — как и llm.py в
        # реальности при ошибке, возвращаем None (не мешает
        # детерминированным эвристикам работать самостоятельно).
        return None

    def last_reply(self):
        return self.sent[-1]["mbody"] if self.sent else None


async def main():
    # ------------------------------------------------------
    # Доступ по JID, не по нику
    # ------------------------------------------------------
    bot = FakeBot(
        occupants={
            "russoturisto": "admin@example.org",
            "impostor": "not-admin@example.org",
            "goos": "goos@example.org",
        }
    )

    # "goos" — тоже BOT_ADMIN_NICKS по нику в другой части бота, но
    # НЕ в MODERATION_WHITELIST_ADMIN_JIDS => команда должна отказать.
    await bot.handle_moderation_whitelist_command(
        "goos", "!вайтлист добавить goos", "room@chat.example"
    )
    assert "❌" in bot.last_reply(), "не-JID-админ не должен пройти"

    # Ник "russoturisto" без реального совпадения по JID (кто-то
    # присвоил себе такой же ник) — тоже отказ.
    await bot.handle_moderation_whitelist_command(
        "impostor", "!вайтлист добавить goos", "room@chat.example"
    )
    assert "❌" in bot.last_reply(), "чужой JID под тем же ником не должен пройти"

    print("OK: доступ к !вайтлист только по подтверждённому bare JID")

    # ------------------------------------------------------
    # Настоящий администратор (jid=admin@example.org) — добавление
    # по нику (occupant online) и по готовому JID
    # ------------------------------------------------------
    await bot.handle_moderation_whitelist_command(
        "russoturisto",
        "!вайтлист добавить goos",
        "room@chat.example",
    )
    assert "goos@example.org" in bot.mod_whitelist_jids
    assert "✅" in bot.last_reply()

    await bot.handle_moderation_whitelist_command(
        "russoturisto",
        "!вайтлист добавить service-bridge@relay.example",
        "room@chat.example",
    )
    assert "service-bridge@relay.example" in bot.mod_whitelist_jids

    # Ник, которого сейчас нет в комнате -> явная ошибка, не тихий сбой.
    await bot.handle_moderation_whitelist_command(
        "russoturisto",
        "!вайтлист добавить offline_nick",
        "room@chat.example",
    )
    assert "❌" in bot.last_reply()

    print("OK: добавление и по нику (occupant), и по готовому JID")

    # ------------------------------------------------------
    # Список и удаление
    # ------------------------------------------------------
    await bot.handle_moderation_whitelist_command(
        "russoturisto", "!вайтлист", "room@chat.example"
    )
    listing = bot.last_reply()
    assert "goos@example.org" in listing
    assert "service-bridge@relay.example" in listing

    await bot.handle_moderation_whitelist_command(
        "russoturisto",
        "!вайтлист убрать goos",
        "room@chat.example",
    )
    assert "goos@example.org" not in bot.mod_whitelist_jids
    assert "✅" in bot.last_reply()

    print("OK: список и удаление работают")

    # ------------------------------------------------------
    # moderate_incoming_muc пропускает вайтлист-JID даже при явном
    # флуде, и снимает прошлую фиксацию бота
    # ------------------------------------------------------
    bot2 = FakeBot(
        occupants={"russoturisto": "admin@example.org", "Служба": "svc@relay.example"}
    )

    # Сначала без вайтлиста — быстрый повторяющийся флуд фиксирует бота.
    for i in range(8):
        await bot2.moderate_incoming_muc(
            {"id": f"svc-{i}"},
            "Служба",
            "same spam",
            "room@chat.example",
        )
    assert "svc@relay.example" in bot2.mod_fixed_jids

    # Администратор вносит его в вайтлист -> прошлая фиксация должна
    # сняться немедленно.
    await bot2.handle_moderation_whitelist_command(
        "russoturisto",
        "!вайтлист добавить Служба",
        "room@chat.example",
    )
    assert "svc@relay.example" in bot2.mod_whitelist_jids
    assert "svc@relay.example" not in bot2.mod_fixed_jids

    calls_before = len(bot2.xep425.calls)
    handled = await bot2.moderate_incoming_muc(
        {"id": "svc-after"},
        "Служба",
        "same spam",
        "room@chat.example",
    )
    assert handled is False, "вайтлист-JID не должен обрабатываться модерацией"
    assert len(bot2.xep425.calls) == calls_before, "не должно быть новых retract"

    print("OK: вайтлист снимает бан и освобождает от дальнейшей модерации")
    print("OK moderation whitelist smoke test")


if __name__ == "__main__":
    asyncio.run(main())
