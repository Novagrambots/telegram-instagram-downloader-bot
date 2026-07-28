"""Сервис скачивания фото профиля (аватарки) Instagram.
Основной путь: users/{pk}/info — отдаёт аватарку в максимальном разрешении.
Fallback: user_info_by_username (публичный путь, но картинка мельче).

Раньше pk искали через users/search — этот эндпоинт почти не используют живые
люди, зато обожают скраперы, поэтому он ускорял бан. Теперь pk берём тем же
способом, что и для Stories.
"""
import logging
import os
import uuid

import aiohttp

from bot.services import session_pool as pool
from bot.services.session_pool import (
    SessionExpiredError,
    has_any_session,
    with_rotation,
)
from bot.services.stories import NO_ACCOUNTS_ERROR, create_session, get_user_id

logger = logging.getLogger(__name__)


async def _fetch_hd_avatar_url(pk: str) -> str:
    """Получает URL аватарки в максимальном разрешении через users/{pk}/info"""
    data = await pool.private_request(f"users/{pk}/info/")

    user = data.get("user", {})
    pic_url = (
        user.get("hd_profile_pic_url_info", {}).get("url")
        or user.get("profile_pic_url")
    )
    if not pic_url:
        raise RuntimeError("У профиля нет фото")
    return pic_url


async def _get_avatar_url(username: str) -> str:
    """Достаёт URL аватарки: сначала HD через pk, потом обычный профиль"""
    try:
        pk = await get_user_id(username)
        return await _fetch_hd_avatar_url(pk)
    except SessionExpiredError:
        raise  # аккаунт выгорел — fallback тем же аккаунтом не поможет
    except Exception as e:
        logger.warning(f"HD-аватарка не вышла для @{username}, пробуем обычную: {e}")

    user = await pool.run(lambda cl: cl.user_info_by_username(username))
    pic_url = user.profile_pic_url_hd or user.profile_pic_url
    if not pic_url:
        raise RuntimeError(f"У профиля @{username} нет фото")
    return str(pic_url)


async def get_avatar_url(username: str) -> str:
    """Возвращает прямую ссылку на HD-аватарку БЕЗ скачивания (для inline-режима)"""
    if not has_any_session():
        raise RuntimeError(NO_ACCOUNTS_ERROR)
    return await with_rotation(lambda: _get_avatar_url(username))


async def download_profile_photo(username: str, download_dir: str) -> dict:
    """Скачивает HD-аватарку профиля.
    Возвращает {file_path, media_type, title} — как download_story.
    """
    if not has_any_session():
        raise RuntimeError(NO_ACCOUNTS_ERROR)
    return await with_rotation(lambda: _download_profile_photo_impl(username, download_dir))


async def _download_profile_photo_impl(username: str, download_dir: str) -> dict:
    pic_url = await _get_avatar_url(username)

    # уникальный суффикс — чтобы параллельные запросы не затирали файлы друг друга
    uid = uuid.uuid4().hex[:8]
    file_path = os.path.join(download_dir, f"avatar_{username}_{uid}.jpg")

    async with create_session() as session:
        async with session.get(
            pic_url, timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Не удалось скачать аватарку: HTTP {resp.status}")
            content = await resp.read()

            with open(file_path, "wb") as f:
                f.write(content)

    size_kb = len(content) / 1024
    logger.info(f"Аватарка скачана: {file_path} ({size_kb:.0f} КБ)")

    return {
        "file_path": file_path,
        "media_type": "photo",
        "title": f"@{username}",
    }
