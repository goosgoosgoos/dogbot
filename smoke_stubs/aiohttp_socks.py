"""
Минимальная заглушка aiohttp_socks — только ProxyConnector.from_url,
на который ссылается dogbot/llm.py.
"""


class ProxyConnector:
    @classmethod
    def from_url(cls, *args, **kwargs):
        return cls()
