"""
Изолированный smoke-тест для web-парсера (dogbot/web.py) и его
интеграции с роутером (needs_web/web_urls в dogbot/router.py).

Сеть в песочнице недоступна (см. smoke_stubs/), поэтому реальный
HTTP-запрос (fetch_web_page по безопасной ссылке) не тестируется —
это проверяется вручную/в проде. Здесь тестируется вся логика,
которая НЕ требует сети: извлечение URL, SSRF-фильтр, сборка
prompt-блока, ранние return'ы fetch_web_page (выключено в конфиге /
небезопасная ссылка) и детерминированный needs_web в роутере.
"""
import asyncio
import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
_STUBS_DIR = os.path.join(_PROJECT_ROOT, "smoke_stubs")

sys.path.insert(0, _STUBS_DIR)
sys.path.insert(0, _PROJECT_ROOT)

from dogbot import web as web_module  # noqa: E402
from dogbot.web import (  # noqa: E402
    extract_urls,
    fetch_web_page,
    format_web_context,
    _is_private_address,
    _is_safe_url,
)
from dogbot.router import DogRouterMixin  # noqa: E402


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


class FakeRouterBot(DogRouterMixin):
    """Голый носитель DogRouterMixin, без реального DogBot/сети."""

    async def call_openrouter(self, messages, **kwargs):
        raise AssertionError(
            "роутер не должен звать LLM, если URL уже "
            "решил needs_web детерминированно"
        )


# ============================================================
# 1) extract_urls
# ============================================================

check(
    "одна ссылка в тексте извлекается",
    extract_urls("глянь https://example.com/article")
    == ["https://example.com/article"],
)

check(
    "хвостовая пунктуация обрезается",
    extract_urls("смотри (https://example.com/x).")
    == ["https://example.com/x"],
)

check(
    "несколько разных ссылок, без дублей",
    extract_urls(
        "https://a.com/1 и https://b.com/2 и снова "
        "https://a.com/1"
    )
    == ["https://a.com/1", "https://b.com/2"],
)

check(
    "лимит на количество ссылок соблюдается",
    len(
        extract_urls(
            "https://a.com https://b.com https://c.com",
            limit=2,
        )
    )
    == 2,
)

check(
    "текст без ссылок -> пустой список",
    extract_urls("просто разговор без урлов") == [],
)

check(
    "пустой/None текст -> пустой список, без исключений",
    extract_urls("") == [] and extract_urls(None) == [],
)

check(
    "ftp:// и другие не-http(s) ссылки не извлекаются",
    extract_urls("держи ftp://internal/share") == [],
)


# ============================================================
# 2) SSRF-фильтр: _is_private_address / _is_safe_url
# ============================================================

check(
    "127.0.0.1 -> приватный/loopback",
    _is_private_address("127.0.0.1") is True,
)

check(
    "169.254.169.254 (облачный metadata) -> приватный",
    _is_private_address("169.254.169.254") is True,
)

check(
    "10.0.0.5 -> приватный",
    _is_private_address("10.0.0.5") is True,
)

check(
    "192.168.1.1 -> приватный",
    _is_private_address("192.168.1.1") is True,
)

check(
    "8.8.8.8 -> публичный, не приватный",
    _is_private_address("8.8.8.8") is False,
)

check(
    "не-IP строка -> считаем небезопасным (True)",
    _is_private_address("не-ip-мусор") is True,
)

safe, reason = _is_safe_url("http://127.0.0.1/admin")
check(
    "http://127.0.0.1/... -> небезопасно (SSRF)",
    safe is False and bool(reason),
)

safe, reason = _is_safe_url("file:///etc/passwd")
check(
    "file:// -> схема не разрешена",
    safe is False and "схема" in reason,
)

safe, reason = _is_safe_url("https://[::1]/x")
check(
    "https://[::1]/... (IPv6 loopback) -> небезопасно",
    safe is False,
)


# ============================================================
# 3) fetch_web_page: ранние return'ы без реальной сети
# ============================================================

_orig_web_enabled = web_module.WEB_ENABLED
web_module.WEB_ENABLED = False

result = run(fetch_web_page("https://example.com/"))

check(
    "WEB_ENABLED=False -> fetch_web_page не лезет в сеть, error задан",
    result["error"] == "веб-чтение выключено в конфиге"
    and result["text"] == "",
)

web_module.WEB_ENABLED = _orig_web_enabled

result = run(fetch_web_page("http://127.0.0.1:8080/secret"))

check(
    "небезопасная (SSRF) ссылка -> fetch_web_page отказывает "
    "ДО сетевого запроса",
    result["error"] == "ссылка недоступна для чтения"
    and result["text"] == "",
)


# ============================================================
# 4) format_web_context: prompt-injection guard + обработка ошибок
# ============================================================

ok_block = format_web_context(
    [
        {
            "url": "https://example.com/a",
            "title": "Заголовок",
            "text": "Основной текст статьи.",
            "description": "",
            "domain": "example.com",
            "error": None,
            "truncated": False,
        }
    ]
)

check(
    "успешная страница попадает в блок с URL/заголовком/текстом",
    "https://example.com/a" in ok_block
    and "Заголовок" in ok_block
    and "Основной текст статьи." in ok_block,
)

check(
    "блок содержит явный запрет исполнять инструкции со страницы",
    "не инструкция" in ok_block.lower()
    and "не выполняй" in ok_block.lower(),
)

err_block = format_web_context(
    [
        {
            "url": "https://dead-link.example/",
            "title": "",
            "text": "",
            "description": "",
            "domain": "dead-link.example",
            "error": "HTTP 404",
            "truncated": False,
        }
    ]
)

check(
    "страница с ошибкой -> честно сообщает о неудаче, а не "
    "молчит/пустой текст",
    "не удалось прочитать" in err_block.lower()
    and "не выдумывай" in err_block.lower(),
)

check(
    "пустой список страниц -> пустая строка (ничего лишнего "
    "в промпт)",
    format_web_context([]) == "",
)


# ============================================================
# 5) Интеграция с роутером: URL решает needs_web/web_urls
#    детерминированно, БЕЗ обращения к LLM-роутеру.
# ============================================================

bot = FakeRouterBot()

precheck = bot.context_precheck(
    "гляньте https://example.com/news/123",
    mention_russoturisto=False,
    is_direct_to_me=False,
    target_nick=None,
)

check(
    "URL в тексте -> needs_web=True в precheck (без LLM)",
    precheck.get("needs_web") is True,
)

check(
    "URL в тексте -> web_urls содержит именно эту ссылку",
    precheck.get("web_urls") == ["https://example.com/news/123"],
)

resolved = run(
    bot.resolve_context_needs(
        "гляньте https://example.com/news/123",
        mention_russoturisto=False,
        is_direct_to_me=False,
        target_nick=None,
    )
)

check(
    "resolve_context_needs прокидывает web_urls дальше "
    "(для ask_llm)",
    resolved.get("web_urls") == ["https://example.com/news/123"],
)

check(
    "resolve_context_needs: needs_web=True без похода в "
    "call_openrouter (см. FakeRouterBot.call_openrouter -> "
    "AssertionError, если бы дошло до LLM)",
    resolved.get("needs_web") is True,
)

resolved_no_url = run(
    bot.resolve_context_needs(
        "просто болтаем без ссылок",
        mention_russoturisto=False,
        is_direct_to_me=False,
        target_nick=None,
    )
)

check(
    "без URL -> web_urls пустой список, а не None/отсутствует",
    resolved_no_url.get("web_urls") == [],
)

check(
    "без URL, роутер-LLM недоступен (FakeRouterBot кидает "
    "исключение) -> needs_web падает на консервативный "
    "failsafe False, ошибка не прорывается наружу",
    resolved_no_url.get("needs_web") is False,
)


# ============================================================
# 6) VISION: is_image_url / split_web_urls — детерминированное
#    отличие картинки от обычной страницы, по расширению ссылки.
# ============================================================

from dogbot.web import is_image_url, split_web_urls  # noqa: E402
from dogbot.web import fetch_image_bytes  # noqa: E402

check(
    "ссылка на .jpg -> картинка",
    is_image_url("https://upload.example.com/abc/photo.jpg")
    is True,
)

check(
    "ссылка на .PNG (регистр не важен) -> картинка",
    is_image_url("https://x.example/img/pic.PNG") is True,
)

check(
    "картинка с query-параметрами после расширения -> картинка",
    is_image_url("https://x.example/pic.webp?size=large") is True,
)

check(
    "обычная статья без расширения картинки -> не картинка",
    is_image_url("https://example.com/article/123") is False,
)

check(
    ".html страница -> не картинка",
    is_image_url("https://example.com/page.html") is False,
)

check(
    "пустая/None ссылка -> не картинка, без исключений",
    is_image_url("") is False and is_image_url(None) is False,
)

page_urls, image_urls = split_web_urls(
    [
        "https://example.com/article/123",
        "https://upload.example.com/photo.png",
        "https://example.com/news/456",
    ]
)

check(
    "split_web_urls: страницы и картинки разделяются корректно",
    page_urls
    == [
        "https://example.com/article/123",
        "https://example.com/news/456",
    ]
    and image_urls == ["https://upload.example.com/photo.png"],
)

check(
    "split_web_urls на пустом списке -> два пустых списка",
    split_web_urls([]) == ([], []),
)


# ============================================================
# 7) fetch_image_bytes: ранние return'ы без реальной сети
# ============================================================

_orig_vision_enabled = web_module.VISION_ENABLED
web_module.VISION_ENABLED = False

result = run(fetch_image_bytes("https://example.com/pic.jpg"))

check(
    "VISION_ENABLED=False -> fetch_image_bytes не лезет в сеть",
    result["error"] == "распознавание картинок выключено в конфиге"
    and result["data_b64"] == "",
)

web_module.VISION_ENABLED = _orig_vision_enabled

result = run(
    fetch_image_bytes("http://127.0.0.1:8080/secret.jpg")
)

check(
    "SSRF-ссылка на картинку -> fetch_image_bytes отказывает "
    "ДО сетевого запроса",
    result["error"] == "ссылка недоступна для чтения"
    and result["data_b64"] == "",
)


# ============================================================
# 8) Интеграция с роутером: image-ссылка решает needs_vision/
#    image_urls детерминированно, отдельно от needs_web/web_urls.
# ============================================================

precheck_img = bot.context_precheck(
    "гляньте https://upload.example.com/photo.jpg",
    mention_russoturisto=False,
    is_direct_to_me=False,
    target_nick=None,
)

check(
    "картинка в тексте -> needs_vision=True, needs_web НЕ "
    "выставляется (это не обычная страница)",
    precheck_img.get("needs_vision") is True
    and not precheck_img.get("needs_web"),
)

check(
    "картинка в тексте -> image_urls содержит именно эту ссылку",
    precheck_img.get("image_urls")
    == ["https://upload.example.com/photo.jpg"],
)

precheck_mixed = bot.context_precheck(
    "статья https://example.com/a и фото "
    "https://upload.example.com/b.png",
    mention_russoturisto=False,
    is_direct_to_me=False,
    target_nick=None,
)

check(
    "смешанное сообщение (страница + картинка) -> оба флага "
    "выставлены, каждый со своим списком",
    precheck_mixed.get("needs_web") is True
    and precheck_mixed.get("web_urls")
    == ["https://example.com/a"]
    and precheck_mixed.get("needs_vision") is True
    and precheck_mixed.get("image_urls")
    == ["https://upload.example.com/b.png"],
)

resolved_img = run(
    bot.resolve_context_needs(
        "гляньте https://upload.example.com/photo.jpg",
        mention_russoturisto=False,
        is_direct_to_me=False,
        target_nick=None,
    )
)

check(
    "resolve_context_needs прокидывает needs_vision/image_urls "
    "дальше (для ask_llm), без похода в LLM-роутер",
    resolved_img.get("needs_vision") is True
    and resolved_img.get("image_urls")
    == ["https://upload.example.com/photo.jpg"],
)

check(
    "картинка без URL в сообщении -> needs_vision=False, "
    "image_urls пустой",
    resolved_no_url.get("needs_vision") is False
    and resolved_no_url.get("image_urls") == [],
)


# ============================================================
# 9) Клирнет-фолбэк при сбое Tor (_fetch_bytes) — сценарии без
#    реальной сети: подменяем aiohttp.ClientSession на скрипт из
#    заранее заданных ответов/исключений и проверяем порядок
#    попыток (Tor первым, клирнет — только при сбое/блокировке).
# ============================================================

from dogbot.web import fetch_web_page  # noqa: E402


class _ScriptedContent:

    def __init__(self, body):
        self._body = body

    async def _gen(self):
        yield self._body

    def iter_chunked(self, size):
        return self._gen()


class _ScriptedResponse:

    def __init__(
        self, status=200, body=b"", headers=None, charset="utf-8"
    ):
        self.status = status
        self.headers = headers or {}
        self.charset = charset
        self.content = _ScriptedContent(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _ScriptedSession:
    """
    Замена aiohttp.ClientSession на время теста: каждая попытка
    _fetch_bytes создаёт новую сессию (это соответствует реальному
    коду), поэтому N элементов в `script` = N ожидаемых попыток
    транспорта (Tor, затем клирнет).
    """

    script = []
    calls = []

    def __init__(self, connector=None, timeout=None):
        type(self).calls.append(connector)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _next(self):
        item = type(self).script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, **kwargs):
        return self._next()

    def post(self, url, **kwargs):
        return self._next()


_orig_client_session = web_module.aiohttp.ClientSession
_orig_fallback_enabled = web_module.WEB_TOR_FALLBACK_ENABLED

web_module.aiohttp.ClientSession = _ScriptedSession

# --- сценарий А: Tor падает сетевой ошибкой -> клирнет успевает ---
_ScriptedSession.calls = []
_ScriptedSession.script = [
    web_module.aiohttp.ClientError("Tor недоступен"),
    _ScriptedResponse(
        status=200,
        body=b"<html><title>T</title>Body text here</html>",
        headers={"Content-Type": "text/html; charset=utf-8"},
    ),
]

result = run(fetch_web_page("https://example.com/ok"))

check(
    "сбой Tor (сетевая ошибка) -> клирнет-фолбэк даёт страницу "
    "без error",
    result["error"] is None and result["title"] == "T",
)

check(
    "сбой Tor -> via='clearnet' (видно, каким транспортом "
    "реально получили ответ)",
    result["via"] == "clearnet",
)

check(
    "сбой Tor -> ровно 2 попытки транспорта (tor, потом клирнет)",
    len(_ScriptedSession.calls) == 2,
)

# --- сценарий Б: Tor вернул 403 (похоже на блокировку) -> клирнет ---
_ScriptedSession.calls = []
_ScriptedSession.script = [
    _ScriptedResponse(status=403, body=b"blocked"),
    _ScriptedResponse(
        status=200,
        body=b"<html><title>OK</title>text</html>",
        headers={"Content-Type": "text/html"},
    ),
]

result = run(fetch_web_page("https://example.com/blocked-on-tor"))

check(
    "HTTP 403 через Tor (похоже на блокировку exit-узла) -> "
    "тоже запускает клирнет-фолбэк",
    result["error"] is None and result["via"] == "clearnet",
)

# --- сценарий В: обычный 404 -> фолбэк НЕ запускается ---
_ScriptedSession.calls = []
_ScriptedSession.script = [
    _ScriptedResponse(status=404, body=b"not found"),
]

result = run(fetch_web_page("https://example.com/missing"))

check(
    "обычный HTTP 404 через Tor -> НЕ похоже на блокировку, "
    "клирнет-фолбэк не запускается (1 попытка)",
    len(_ScriptedSession.calls) == 1
    and result["error"] == "HTTP 404"
    and result["via"] == "tor",
)

# --- сценарий Г: фолбэк выключен -> сбой Tor так и остаётся сбоем ---
web_module.WEB_TOR_FALLBACK_ENABLED = False
_ScriptedSession.calls = []
_ScriptedSession.script = [
    web_module.aiohttp.ClientError("Tor недоступен"),
]

result = run(fetch_web_page("https://example.com/no-fallback"))

check(
    "WEB_TOR_FALLBACK_ENABLED=False -> сбой Tor не пытается "
    "клирнет, ровно 1 попытка",
    len(_ScriptedSession.calls) == 1
    and result["error"] is not None
    and "сетевая ошибка" in result["error"],
)

web_module.WEB_TOR_FALLBACK_ENABLED = _orig_fallback_enabled

# --- сценарий Д: оба транспорта падают -> честная ошибка, не крэш ---
_ScriptedSession.calls = []
_ScriptedSession.script = [
    web_module.aiohttp.ClientError("Tor недоступен"),
    web_module.aiohttp.ClientError("клирнет тоже недоступен"),
]

result = run(fetch_web_page("https://example.com/both-fail"))

check(
    "оба транспорта сбоят -> честный error, не исключение "
    "наружу",
    result["error"] is not None
    and "сетевая ошибка" in result["error"]
    and len(_ScriptedSession.calls) == 2,
)

web_module.aiohttp.ClientSession = _orig_client_session


print(f"\n{passed} passed, {failed} failed")

if failed:
    sys.exit(1)
