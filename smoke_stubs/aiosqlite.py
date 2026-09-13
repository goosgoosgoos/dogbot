"""
Минимальная заглушка aiosqlite — только имя Row, на которое
ссылается dogbot/database.py при настройке row_factory. Полноценных
запросов эта заглушка не поддерживает (smoke-тесты роутера БД не
трогают).
"""


class Row:
    pass


async def connect(*args, **kwargs):
    raise NotImplementedError(
        "aiosqlite-стаб: реальная БД недоступна в песочнице"
    )
