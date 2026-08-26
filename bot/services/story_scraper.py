"""Скрапер Instagram через анонимные viewer-сайты (Playwright).

Аккаунты Instagram тут не нужны: сайт держит свой пул на бэкенде, а мы для него
обычный посетитель. Работает это так: открываем страницу в headless-браузере,
её же JS подписывает запрос (алгоритм подписи обфусцирован и меняется — руками
такое повторять бессмысленно), и через fetch получаем JSON.

Важно: DOM мы не парсим. Вёрстка на таких сайтах меняется постоянно, а вот
структура ответа API совпадает с private API Instagram — те же image_versions2,
video_versions, pk. Поэтому дальше работает тот же разбор, что и для аккаунтов,
а сами медиафайлы качаются напрямую с CDN Instagram обычным aiohttp.
"""
import asyncio
import logging
from urllib.parse import parse_qs, urlparse

from bot.config import settings

logger = logging.getLogger(__name__)


class ScraperError(RuntimeError):
    """Ни одно зеркало не смогло отдать данные"""


# Зеркала одного и того же движка. Домен API берём не отсюда, а из самой
# страницы (msc.workerHubDomain) — если они его сменят, подхватим автоматически.
SCRAPER_SITES = (
    "https://anonyig.com/en2/",
    "https://fastdl.app/en",
    "https://igram.world/en",
)

# сколько ждём загрузки страницы и ответа API
_PAGE_TIMEOUT_MS = 30_000
_API_TIMEOUT_MS = 25_000

# JS, который выполняется в контексте страницы: берёт контроллер сайта,
# подписывает тело его же функцией и дёргает нужный эндпоинт
_CALL_API_JS = """
async ({ endpoint, payload }) => {
  const root = document.querySelector('[data-v-app], #app');
  const app = root && root.__vue_app__;
  const msc = app && app.config && app.config.globalProperties
    && app.config.globalProperties.$mediaSearchController;
  if (!msc) throw new Error('контроллер сайта не найден — вёрстка изменилась');

  const body = await msc.subscribeSignedRequestBody(payload);
  const host = msc.workerHubDomain || 'api-wh.anonyig.com';
  const resp = await fetch(`https://${host}/api/v1/instagram/${endpoint}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'accept': 'application/json, text/plain, */*' },
    body: JSON.stringify(body),
  });
  const text = await resp.text();
  if (!resp.ok) throw new Error(`API вернул ${resp.status}: ${text.slice(0, 200)}`);
  return JSON.parse(text);
}
"""

# Эндпоинт /api/convert принимает ссылку на пост. Важно: URL передаётся строкой —
# функция подписи сама завернёт его в sf_url. Если передать объектом, подпись не сойдётся.
_CONVERT_JS = """
async ({ url }) => {
  const root = document.querySelector('[data-v-app], #app');
  const app = root && root.__vue_app__;
  const msc = app && app.config && app.config.globalProperties
    && app.config.globalProperties.$mediaSearchController;
  if (!msc) throw new Error('контроллер сайта не найден — вёрстка изменилась');

  const body = await msc.subscribeSignedRequestBody(url);
  const host = msc.workerHubDomain || 'api-wh.anonyig.com';
  const resp = await fetch(`https://${host}/api/convert`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'accept': 'application/json, text/plain, */*' },
    body: JSON.stringify(body),
  });
  const text = await resp.text();
  if (!resp.ok) throw new Error(`convert вернул ${resp.status}: ${text.slice(0, 200)}`);
  return JSON.parse(text);
}
"""


# Playwright синхронизируем: один браузер на процесс, запросы по очереди —
# параллельный обстрел с одного IP эти сайты режут так же, как Instagram
_lock = asyncio.Lock()
_playwright = None
_browser = None
_page = None
_page_site: str | None = None


async def _ensure_page(site: str):
    """Открывает (или переиспользует) страницу зеркала. Держим одну на процесс —
    открытие занимает несколько секунд, каждый раз это расточительно.
    """
    global _playwright, _browser, _page, _page_site

    from playwright.async_api import async_playwright

    if _browser is None:
        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        logger.info("Playwright: браузер запущен")

    if _page is not None and _page_site == site and not _page.is_closed():
        return _page

    if _page is not None and not _page.is_closed():
        await _page.close()

    context = await _browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
        ),
        locale="en-US",
        viewport={"width": 1280, "height": 800},
        proxy={"server": settings.scraper_proxy} if settings.scraper_proxy else None,
    )
    page = await context.new_page()

    # картинки и шрифты не нужны — нам нужен только JS, который считает подпись
    await page.route(
        "**/*",
        lambda route: asyncio.ensure_future(
            route.abort()
            if route.request.resource_type in ("image", "media", "font")
            else route.continue_()
        ),
    )

    await page.goto(site, wait_until="domcontentloaded", timeout=_PAGE_TIMEOUT_MS)
    # ждём, пока поднимется Vue-приложение с контроллером
    await page.wait_for_function(
        "() => { const r = document.querySelector('[data-v-app], #app');"
        " return !!(r && r.__vue_app__ && r.__vue_app__.config.globalProperties.$mediaSearchController); }",
        timeout=_PAGE_TIMEOUT_MS,
    )

    _page, _page_site = page, site
    logger.info(f"Playwright: страница {site} готова")
    return page


async def _call_on_site(site: str, script: str, arg: dict) -> dict:
    """Один вызов через конкретное зеркало"""
    page = await _ensure_page(site)
    return await asyncio.wait_for(
        page.evaluate(script, arg),
        timeout=_API_TIMEOUT_MS / 1000,
    )


async def _run_with_mirrors(script: str, arg: dict, what: str) -> dict:
    """Гоняет вызов по зеркалам до первого удачного"""
    errors = []

    async with _lock:
        for site in SCRAPER_SITES:
            try:
                return await _call_on_site(site, script, arg)
            except Exception as e:
                logger.warning(f"Скрапер: {site} не сработал на {what} ({type(e).__name__}: {e})")
                errors.append(f"{site}: {type(e).__name__}")
                # страница могла умереть вместе с зеркалом — заставим переоткрыть
                await _reset_page()

    raise ScraperError("Все зеркала недоступны — " + "; ".join(errors))


async def call_api(endpoint: str, payload: dict) -> dict:
    """Дёргает эндпоинт /api/v1/instagram/<endpoint> viewer-сайта"""
    return await _run_with_mirrors(
        _CALL_API_JS, {"endpoint": endpoint, "payload": payload}, endpoint
    )


def _direct_url(url: str) -> str:
    """Вытаскивает прямую ссылку CDN из прокси-ссылки сайта.

    Сайт отдаёт media.anonyig.com/get?...&uri=<ссылка CDN>, и у такой ссылки
    есть срок жизни. Оригинал в параметре uri живёт дольше и не зависит от них.
    """
    try:
        parsed = urlparse(url)
        if "uri=" in (parsed.query or ""):
            uri = parse_qs(parsed.query).get("uri", [""])[0]
            if uri.startswith("http"):
                return uri
    except Exception:
        pass
    return url


async def fetch_post(post_url: str) -> list[dict]:
    """Медиа поста/Reels по ссылке. Возвращает [{url, media_type, thumb}, ...]

    Внимание: карусели этот путь не отдаёт — на пост с несколькими медиа сервис
    стабильно возвращает пустой список (проверено, три попытки подряд). Карусели
    остаются за Cobalt (он их умеет) и private API, сюда они попадают только если
    Cobalt уже не справился — тогда бросаем ScraperError и идём дальше по цепочке.
    """
    data = await _run_with_mirrors(_CONVERT_JS, {"url": post_url}, "convert")

    entries = data.get("url") or []
    if not entries:
        raise ScraperError("Сервис не отдал медиа поста")

    thumb = _direct_url(data.get("thumb") or "") or None

    results = []
    for entry in entries:
        media_url = entry.get("url")
        if not media_url:
            continue
        ext = (entry.get("ext") or entry.get("type") or "").lower()
        results.append({
            "url": _direct_url(media_url),
            "media_type": "video" if ext in ("mp4", "mov", "webm") else "photo",
            "thumb": thumb,
        })

    if not results:
        raise ScraperError("Сервис не отдал медиа поста")
    return results


async def _reset_page() -> None:
    """Закрывает текущую страницу, чтобы следующий заход открыл её заново"""
    global _page, _page_site
    try:
        if _page is not None and not _page.is_closed():
            await _page.close()
    except Exception:
        pass
    _page, _page_site = None, None


async def fetch_stories(username: str) -> list[dict]:
    """Активные истории юзера. Формат элементов — как в private API Instagram"""
    data = await call_api("stories", {"username": username})
    items = data.get("result") or []
    if not items:
        raise ScraperError(f"У @{username} нет активных историй")
    return items


async def fetch_user_info(username: str) -> dict:
    """Профиль юзера (включая hd_profile_pic_url_info для аватарки)"""
    data = await call_api("userInfo", {"username": username})
    result = data.get("result") or []
    user = result[0].get("user") if result else None
    if not user:
        raise ScraperError(f"Профиль @{username} не найден")
    return user


async def shutdown() -> None:
    """Закрывает браузер при остановке бота — иначе Chromium останется висеть"""
    global _playwright, _browser, _page, _page_site
    try:
        if _browser is not None:
            await _browser.close()
        if _playwright is not None:
            await _playwright.stop()
    except Exception as e:
        logger.warning(f"Playwright: не удалось закрыть браузер: {e}")
    finally:
        _playwright = _browser = _page = None
        _page_site = None
        logger.info("Playwright: браузер остановлен")
