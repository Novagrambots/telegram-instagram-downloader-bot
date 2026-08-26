"""Сервис скачивания фото профиля (аватарки) Instagram.

Источники те же, что у Stories: сначала анонимные viewer-сайты (аккаунты не
нужны), потом private API через пул аккаунтов. Оба отдают
hd_profile_pic_url_info — аватарку в максимальном разрешении.
"""
import logging
import os
import uuid

from bot.services import session_pool as pool
from bot.services import story_scraper
from bot.services.session_pool import has_any_session, with_rotation
from bot.services.stories import NO_ACCOUNTS_ERROR, fetch_media, get_user_id

logger = logging.getLogger(__name__)


def _pick_pic_url(user: dict) -> str | None:
    """Достаёт из профиля ссылку на аватарку, начиная с самого большого размера"""
    return (
        (user.get("hd_profile_pic_url_info") or {}).get("url")
        or user.get("profile_pic_url_hd")
        or user.get("profile_pic_url")
    )


async def _avatar_via_api(username: str) -> str:
    """Аватарка через private API — users/{pk}/info отдаёт максимальное разрешение"""
    pk = await get_user_id(username)
    data = await pool.private_request(f"users/{pk}/info/")
    pic_url = _pick_pic_url(data.get("user", {}))
    if not pic_url:
        raise RuntimeError("У профиля нет фото")
    return pic_url


async def get_avatar_url(username: str) -> str:
    """Возвращает прямую ссылку на HD-аватарку БЕЗ скачивания (для inline-режима)"""
    try:
        user = await story_scraper.fetch_user_info(username)
        pic_url = _pick_pic_url(user)
        if pic_url:
            logger.info(f"Аватарка @{username} получена анонимно")
            return pic_url
        raise RuntimeError(f"У профиля @{username} нет фото")
    except Exception as e:
        logger.warning(f"Анонимный источник не дал аватарку @{username}: {e}")

    if not has_any_session():
        raise RuntimeError(NO_ACCOUNTS_ERROR)

    return await with_rotation(lambda: _avatar_via_api(username))


async def download_profile_photo(username: str, download_dir: str) -> dict:
    """Скачивает HD-аватарку профиля.
    Возвращает {file_path, media_type, title} — как download_story.
    """
    pic_url = await get_avatar_url(username)

    # уникальный суффикс — чтобы параллельные запросы не затирали файлы друг друга
    uid = uuid.uuid4().hex[:8]
    file_path = os.path.join(download_dir, f"avatar_{username}_{uid}.jpg")

    content = await fetch_media(pic_url, timeout=30)
    with open(file_path, "wb") as f:
        f.write(content)

    size_kb = len(content) / 1024
    logger.info(f"Аватарка скачана: {file_path} ({size_kb:.0f} КБ)")

    return {
        "file_path": file_path,
        "media_type": "photo",
        "title": f"@{username}",
    }
