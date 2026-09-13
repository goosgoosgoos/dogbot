"""
Минимальная заглушка aiohttp — только то, на что ссылается
dogbot/config.py и dogbot/llm.py (по имени, без реального HTTP).
"""


class ClientTimeout:
    def __init__(self, *args, **kwargs):
        pass


class ClientError(Exception):
    pass


class ClientSession:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, *args, **kwargs):
        raise NotImplementedError(
            "aiohttp-стаб: реальный HTTP недоступен в песочнице"
        )
