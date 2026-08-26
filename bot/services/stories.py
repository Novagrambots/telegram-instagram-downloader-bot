"""Сервис скачивания Instagram Stories.

Два источника, в порядке приоритета:
1. Анонимные viewer-сайты через Playwright (story_scraper) — аккаунты не нужны,
   работает для любых публичных профилей;
2. private API через пул аккаунтов — если аккаунты заведены. Он умеет то, чего
   не умеют viewer-сайты: закрытые профили, на которые подписан аккаунт.

Оба источника отдают элементы одного формата (image_versions2/video_versions/pk),
поэтому разбор и скачивание общие. Медиа качаем напрямую с CDN Instagram —
там авторизация не нужна.
"""
import logging
import os
import re
import time

import aiohttp
from aiohttp_socks import ProxyConnector

from bot.config import settings
from bot.services import session_pool as pool
from bot.services import story_scraper
from bot.services.session_pool import (
    AllSessionsExpiredError,
    SessionExpiredError,
    has_any_session,
    proxy_kind,
    with_rotation,
)
from bot.services.story_scraper import ScraperError

logger = logging.getLogger(__name__)

# SessionExpiredError реэкспортируется из session_pool — старые импорты
# `from bot.services.stories import SessionExpiredError` продолжают работать
__all__ = ["SessionExpiredError", "AllSessionsExpiredError", "ScraperError"]

# что сказать, когда не сработал ни один источник
NO_ACCOUNTS_ERROR = (
    "Не удалось получить контент.\n"
    "Анонимные сервисы недоступны, а Instagram-аккаунтов нет. "
    "Добавить: docker compose exec bot python -m bot.login_account"
)

# лента историй живёт 24 часа, за 10 минут там почти ничего не меняется.
# Кэш нужен чтобы пачка запросов по разным историям одного блогера
# стоила один поход наружу, а не десять
_STORY_FEED_TTL = 10 * 60
# {username: (когда положили, список историй)}
_reel_cache: dict[str, tuple[float, list[dict]]] = {}

# кэш user_id чтобы не запрашивать повторно
_user_id_cache: dict[str, str] = {}


def parse_story_url(url: str) -> tuple[str, str]:
    """Извлекает username и story_id из URL истории
    URL формат: https://www.instagram.com/stories/username/story_id/
    """
    match = re.search(r"stories/([^/]+)/(\d+)", url)
    if not match:
        raise ValueError(f"Не удалось распарсить URL истории: {url}")
    return match.group(1), match.group(2)


def is_story_url(url: str) -> bool:
    """Проверяет, является ли URL ссылкой на историю"""
    return bool(re.search(r"instagram\.com/stories/[^/]+/\d+", url))


def create_session(use_proxy: bool = True) -> aiohttp.ClientSession:
    """ClientSession для запросов к CDN. ProxyConnector работает с любым
    протоколом (HTTP/HTTPS/SOCKS4/SOCKS5), включая user:pass в URL.
    """
    proxy_url = settings.instagram_proxy if use_proxy else None
    if not proxy_url:
        return aiohttp.ClientSession()
    connector = ProxyConnector.from_url(proxy_url)
    return aiohttp.ClientSession(connector=connector)


async def fetch_media(url: str, timeout: int = 60) -> bytes:
    """Скачивает медиафайл по CDN-ссылке.

    Прокси пробуем первым, если он задан, но ссылки CDN публичные и в нём не
    нуждаются. Поэтому упавший прокси не должен ронять скачивание — при отказе
    просто идём напрямую.
    """
    attempts = [True, False] if settings.instagram_proxy else [False]
    last_err: Exception | None = None

    for use_proxy in attempts:
        try:
            async with create_session(use_proxy) as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=timeout)
                ) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"Не удалось скачать медиа: HTTP {resp.status}")
                    return await resp.read()
        except Exception as e:
            last_err = e
            if use_proxy:
                logger.warning(f"Прокси не отдал медиа ({type(e).__name__}), качаем напрямую")

    raise last_err or RuntimeError("Не удалось скачать медиа")


async def get_user_id(username: str) -> str:
    """Получает user_id по username через private API (с кэшем в памяти)"""
    if username in _user_id_cache:
        logger.info(f"@{username} → user_id={_user_id_cache[username]} (кэш)")
        return _user_id_cache[username]

    user_id = str(await pool.run(lambda cl: cl.user_id_from_username(username)))

    _user_id_cache[username] = user_id
    logger.info(f"@{username} → user_id={user_id}")
    return user_id


async def _fetch_items_via_api(username: str) -> list[dict]:
    """Лента историй через private API — сырым JSON, чтобы получить все версии видео"""
    user_id = await get_user_id(username)
    data = await pool.private_request(
        "feed/reels_media/", params={"reel_ids": user_id}
    )
    reel = data.get("reels", {}).get(user_id, {})
    return reel.get("items", [])


async def _fetch_story_items(username: str, force: bool = False) -> list[dict]:
    """Лента историй: сначала анонимные сервисы, потом свои аккаунты.
    force=True — игнорировать кэш.
    """
    cached = _reel_cache.get(username)
    if cached and not force and time.time() - cached[0] < _STORY_FEED_TTL:
        logger.info(f"Лента историй @{username} из кэша")
        return cached[1]

    items: list[dict] = []
    scraper_err: Exception | None = None

    try:
        items = await story_scraper.fetch_stories(username)
        logger.info(f"Истории @{username} получены анонимно ({len(items)} шт.)")
    except Exception as e:
        scraper_err = e
        logger.warning(f"Анонимный источник не сработал для @{username}: {e}")

        # свои аккаунты — запасной путь. Он умеет закрытые профили,
        # на которые подписан аккаунт
        if has_any_session():
            items = await with_rotation(lambda: _fetch_items_via_api(username))
            logger.info(f"Истории @{username} получены через аккаунт ({len(items)} шт.)")

    if not items:
        if scraper_err and not has_any_session():
            raise RuntimeError(NO_ACCOUNTS_ERROR)
        raise RuntimeError("Истории не найдены или уже истекли (24 часа)")

    _reel_cache[username] = (time.time(), items)
    return items


def _find_story(items: list[dict], story_id: str) -> dict | None:
    """Ищет конкретную историю по story_id из ссылки"""
    for item in items:
        if str(item.get("pk")) == story_id or str(item.get("id", "")).startswith(story_id):
            return item
    return None


async def get_story_media(username: str, story_id: str) -> dict:
    """Получает медиа конкретной истории"""
    items = await _fetch_story_items(username)
    item = _find_story(items, story_id)

    # не нашли — может быть свежая история, а у нас старый кэш. Перезапрашиваем
    if item is None and username in _reel_cache:
        items = await _fetch_story_items(username, force=True)
        item = _find_story(items, story_id)

    if item is None:
        # фоллбэк — последняя история
        logger.warning(f"Story {story_id} не найден, отправляем последнюю")
        return items[-1]

    return item


# лимит Telegram на скачивание файла по URL (inline-режим)
_INLINE_VIDEO_LIMIT = 19 * 1024 * 1024


async def pick_inline_video_url(item: dict) -> str:
    """Выбирает версию видео до 20 МБ (Telegram не заберёт по URL больше).
    Проверяем размер HEAD-запросом, если все большие — берём самую маленькую.
    """
    versions = item.get("video_versions", [])
    async with create_session(use_proxy=False) as session:
        for v in versions:
            try:
                async with session.head(
                    v["url"], timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    size = int(resp.headers.get("Content-Length", 0))
                    if 0 < size < _INLINE_VIDEO_LIMIT:
                        return v["url"]
            except Exception:
                continue
    # все версии большие или HEAD не сработал — берём последнюю (самое низкое качество)
    return versions[-1]["url"]


async def get_story_media_urls(url: str) -> dict:
    """Возвращает прямую CDN-ссылку истории БЕЗ скачивания (для inline-режима).
    Формат: {url, media_type, thumb, username}
    """
    username, story_id = parse_story_url(url)
    item = await get_story_media(username, story_id)

    candidates = item.get("image_versions2", {}).get("candidates", [])
    thumb = candidates[-1]["url"] if candidates else None

    if item.get("video_versions"):
        media_url = await pick_inline_video_url(item)
        media_type = "video"
    else:
        if not candidates:
            raise RuntimeError("Не удалось найти медиа в истории")
        media_url = candidates[0]["url"]
        media_type = "photo"

    return {
        "url": media_url,
        "media_type": media_type,
        "thumb": thumb,
        "username": username,
    }


async def download_story(url: str, download_dir: str) -> dict:
    """Скачивает историю и возвращает {file_path, media_type, title}"""
    username, story_id = parse_story_url(url)
    logger.info(f"Stories: user=@{username}, proxy={proxy_kind()}")

    item = await get_story_media(username, story_id)

    # определяем тип и URL медиа
    media_type = "video" if item.get("video_versions") else "photo"

    if media_type == "video":
        media_url = item["video_versions"][0]["url"]
        ext = ".mp4"
    else:
        candidates = item.get("image_versions2", {}).get("candidates", [])
        if not candidates:
            raise RuntimeError("Не удалось найти фото в истории")
        media_url = candidates[0]["url"]
        ext = ".jpg"

    file_path = os.path.join(download_dir, f"story_{username}_{story_id}{ext}")

    content = await fetch_media(media_url, timeout=30)

    if len(content) > 2000 * 1024 * 1024:
        raise RuntimeError("Файл больше 2 ГБ — лимит локального Telegram API")

    with open(file_path, "wb") as f:
        f.write(content)

    size_mb = len(content) / (1024 * 1024)
    logger.info(f"Story скачана: {file_path} ({size_mb:.1f} МБ, {media_type})")

    return {
        "file_path": file_path,
        "media_type": media_type,
        "title": f"Story @{username}",
    }
