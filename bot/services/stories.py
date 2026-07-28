"""Сервис скачивания Instagram Stories — через private API (instagrapi).

Запросы к API идут через пул аккаунтов (session_pool), сами медиафайлы качаем
обычным aiohttp: ссылки CDN авторизации не требуют.
"""
import logging
import os
import re
import time

import aiohttp
from aiohttp_socks import ProxyConnector

from bot.config import settings
from bot.services import session_pool as pool
from bot.services.session_pool import (
    AllSessionsExpiredError,
    SessionExpiredError,
    has_any_session,
    proxy_kind,
    with_rotation,
)

logger = logging.getLogger(__name__)

# SessionExpiredError реэкспортируется из session_pool — старые импорты
# `from bot.services.stories import SessionExpiredError` продолжают работать
__all__ = ["SessionExpiredError", "AllSessionsExpiredError"]

# что сказать юзеру/админу, когда аккаунтов нет вообще
NO_ACCOUNTS_ERROR = (
    "Нет ни одного Instagram-аккаунта.\n"
    "Добавь через: docker compose exec bot python -m bot.login_account"
)

# лента историй живёт 24 часа, за 10 минут там почти ничего не меняется.
# Кэш нужен чтобы пачка запросов по разным историям одного блогера
# стоила один поход в Instagram, а не десять
_STORY_FEED_TTL = 10 * 60
# {user_id: (когда положили, список историй)}
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


def create_session() -> aiohttp.ClientSession:
    """ClientSession для скачивания медиа с CDN — через тот же прокси, что и API.
    ProxyConnector работает с любым протоколом (HTTP/HTTPS/SOCKS4/SOCKS5),
    включая user:pass в URL.
    """
    proxy_url = settings.instagram_proxy or None
    if not proxy_url:
        return aiohttp.ClientSession()
    connector = ProxyConnector.from_url(proxy_url)
    return aiohttp.ClientSession(connector=connector)


async def get_user_id(username: str) -> str:
    """Получает user_id по username (с кэшем в памяти)"""
    if username in _user_id_cache:
        logger.info(f"@{username} → user_id={_user_id_cache[username]} (кэш)")
        return _user_id_cache[username]

    user_id = str(await pool.run(lambda cl: cl.user_id_from_username(username)))

    _user_id_cache[username] = user_id
    logger.info(f"@{username} → user_id={user_id}")
    return user_id


async def _fetch_story_items(user_id: str, force: bool = False) -> list[dict]:
    """Лента историй юзера сырым JSON (нужны все версии видео, а не только лучшая).
    force=True — игнорировать кэш.
    """
    cached = _reel_cache.get(user_id)
    if cached and not force and time.time() - cached[0] < _STORY_FEED_TTL:
        logger.info(f"Лента историй user_id={user_id} из кэша")
        return cached[1]

    data = await pool.private_request(
        "feed/reels_media/", params={"reel_ids": user_id}
    )

    reel = data.get("reels", {}).get(user_id, {})
    items = reel.get("items", [])

    if not items:
        raise RuntimeError("Истории не найдены или уже истекли (24 часа)")

    _reel_cache[user_id] = (time.time(), items)
    return items


def _find_story(items: list[dict], story_id: str) -> dict | None:
    """Ищет конкретную историю по story_id из ссылки"""
    for item in items:
        if str(item.get("pk")) == story_id or str(item.get("id", "")).startswith(story_id):
            return item
    return None


async def get_story_media(user_id: str, story_id: str) -> dict:
    """Получает медиа конкретной истории"""
    items = await _fetch_story_items(user_id)
    item = _find_story(items, story_id)

    # не нашли — может быть свежая история, а у нас старый кэш. Перезапрашиваем
    if item is None and user_id in _reel_cache:
        items = await _fetch_story_items(user_id, force=True)
        item = _find_story(items, story_id)

    if item is None:
        # фоллбэк — последняя история
        logger.warning(f"Story {story_id} не найден, отправляем последнюю")
        return items[-1]

    return item


# лимит Telegram на скачивание файла по URL (inline-режим)
_INLINE_VIDEO_LIMIT = 19 * 1024 * 1024


async def pick_inline_video_url(
    session: aiohttp.ClientSession, item: dict
) -> str:
    """Выбирает версию видео до 20 МБ (Telegram не заберёт по URL больше).
    Проверяем размер HEAD-запросом, если все большие — берём самую маленькую.
    """
    versions = item.get("video_versions", [])
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


async def _get_story_media_urls_impl(url: str) -> dict:
    username, story_id = parse_story_url(url)

    user_id = await get_user_id(username)
    item = await get_story_media(user_id, story_id)

    candidates = item.get("image_versions2", {}).get("candidates", [])
    thumb = candidates[-1]["url"] if candidates else None

    async with create_session() as session:
        if item.get("video_versions"):
            media_url = await pick_inline_video_url(session, item)
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


async def get_story_media_urls(url: str) -> dict:
    """Возвращает прямую CDN-ссылку истории БЕЗ скачивания (для inline-режима).
    Формат: {url, media_type, thumb, username}
    """
    if not has_any_session():
        raise RuntimeError(NO_ACCOUNTS_ERROR)
    return await with_rotation(lambda: _get_story_media_urls_impl(url))


async def _download_story_impl(url: str, download_dir: str) -> dict:
    username, story_id = parse_story_url(url)
    logger.info(f"Stories: user=@{username}, proxy={proxy_kind()}")

    user_id = await get_user_id(username)
    item = await get_story_media(user_id, story_id)

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

    async with create_session() as session:
        async with session.get(
            media_url, timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Не удалось скачать медиа: HTTP {resp.status}")
            content = await resp.read()

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


async def download_story(url: str, download_dir: str) -> dict:
    """Скачивает историю и возвращает {file_path, media_type, title}"""
    if not has_any_session():
        raise RuntimeError(NO_ACCOUNTS_ERROR)
    return await with_rotation(lambda: _download_story_impl(url, download_dir))
