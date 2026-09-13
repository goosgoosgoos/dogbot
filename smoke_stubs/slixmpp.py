"""
Минимальная заглушка slixmpp для изолированных smoke-тестов —
сеть/пакет недоступны в песочнице. Даёт только то, что нужно для
успешного ИМПОРТА пакета dogbot (bot.py собирает DogBot как
наследника slixmpp.ClientXMPP на уровне определения класса — сама
XMPP-логика в smoke-тестах не используется и не вызывается).
"""


class ClientXMPP:
    def __init__(self, *args, **kwargs):
        pass


class JID:
    """
    Минимальный разбор JID вида local@domain/resource — только
    то, что нужно core.py (resolve_quote_author читает .resource
    из <reply to="room@muc.example.com/Ник"/>). Настоящий slixmpp
    делает то же самое куда полнее (валидация, escaping и т.д.),
    для smoke-теста этого достаточно.
    """

    def __init__(self, jid_str):
        jid_str = jid_str or ""

        if "/" in jid_str:
            bare, resource = jid_str.split("/", 1)
        else:
            bare, resource = jid_str, None

        self.bare = bare
        self.resource = resource or None

    def __str__(self):
        if self.resource:
            return f"{self.bare}/{self.resource}"
        return self.bare
