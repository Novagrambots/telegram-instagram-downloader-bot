"""Сервис скачивания постов Instagram через private API (instagrapi).
Fallback на случай когда Cobalt не смог (error.api.fetch.empty и т.д.).
Поддерживает: фото, видео, карусели.
"""
import logging
import os
import re
import uuid

import aiohttp
from instagrapi.exceptions import ClientNotFoundError, MediaNotFound

from bot.services import session_pool as pool
from bot.services.session_pool import has_any_session, with_rotation
from bot.services.stories import (
    NO_ACCOUNTS_ERROR,
    create_session,
    pick_inline_video_url,
)

logger = logging.getLogger(__name__)

# алфавит shortcode Instagram (base64url)
_SHORTCODE_ALPHABET = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


def is_post_url(url: str) -> bool:
    """Проверяет, является ли URL ссылкой на пост/Reels"""
    return bool(re.search(r"instagram\.com/(p|reel|reels|tv)/[\w\-]+", url))


def parse_post_shortcode(url: str) -> str:
    """Извлекает shortcode из URL поста
    URL формат: https://www.instagram.com/p/DaUZM67CK8c/
    """
    match = re.search(r"instagram\.com/(?:p|reel|reels|tv)/([\w\-]+)", url)
    if not match:
        raise ValueError(f"Не удалось распарсить URL поста: {url}")
    return match.group(1)


def shortcode_to_media_pk(shortcode: str) -> int:
    """Конвертирует shortcode в media_pk (число для private API)"""
    pk = 0
    for ch in shortcode:
        pk = pk * 64 + _SHORTCODE_ALPHABET.index(ch)
    return pk


async def get_media_info(media_pk: int) -> dict:
    """Получает инфо о посте сырым JSON — нужны все версии видео, а не только лучшая"""
    try:
        data = await pool.private_request(f"media/{media_pk}/info/")
    except (MediaNotFound, ClientNotFoundError) as e:
        raise RuntimeError("Пост не найден (404) — удалён или ссылка неправильная") from e

    items = data.get("items", [])
    if not items:
        raise RuntimeError("Пост не найден — Instagram вернул пустой ответ")

    return items[0]


def _pick_media_url(item: dict) -> tuple[str, str]:
    """Достаёт из элемента поста URL медиа и тип: (url, media_type)"""
    if item.get("video_versions"):
        return item["video_versions"][0]["url"], "video"

    candidates = item.get("image_versions2", {}).get("candidates", [])
    if not candidates:
        raise RuntimeError("Не удалось найти медиа в посте")
    return candidates[0]["url"], "photo"


async def _download_file(
    session: aiohttp.ClientSession, media_url: str, file_path: str
) -> None:
    """Скачивает файл по URL на диск"""
    async with session.get(
        media_url, timeout=aiohttp.ClientTimeout(total=60)
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"Не удалось скачать медиа: HTTP {resp.status}")
        content = await resp.read()

        if len(content) > 2000 * 1024 * 1024:
            raise RuntimeError("Файл больше 2 ГБ — лимит локального Telegram API")

        with open(file_path, "wb") as f:
            f.write(content)

    size_mb = len(content) / (1024 * 1024)
    logger.info(f"Скачано: {file_path} ({size_mb:.1f} МБ)")


async def get_post_media_urls(url: str) -> list[dict]:
    """Возвращает прямые CDN-ссылки медиа поста БЕЗ скачивания (для inline-режима).
    Каждый элемент: {url, media_type, thumb}
    """
    if not has_any_session():
        raise RuntimeError(NO_ACCOUNTS_ERROR)
    return await with_rotation(lambda: _get_post_media_urls_impl(url))


async def _get_post_media_urls_impl(url: str) -> list[dict]:
    shortcode = parse_post_shortcode(url)
    media_pk = shortcode_to_media_pk(shortcode)

    post = await get_media_info(media_pk)
    medias = post.get("carousel_media") or [post]

    results = []
    async with create_session() as session:
        for item in medias:
            # превью всегда есть в image_versions2 (у видео это обложка)
            candidates = item.get("image_versions2", {}).get("candidates", [])
            thumb = candidates[-1]["url"] if candidates else None

            if item.get("video_versions"):
                media_url = await pick_inline_video_url(session, item)
                media_type = "video"
            else:
                if not candidates:
                    continue  # нет ни видео, ни фото — пропускаем элемент
                media_url = candidates[0]["url"]
                media_type = "photo"

            results.append({
                "url": media_url,
                "media_type": media_type,
                "thumb": thumb,
            })

    if not results:
        raise RuntimeError("Не удалось найти медиа в посте")

    return results


async def download_post(url: str, download_dir: str) -> list[dict]:
    """Скачивает пост (фото/видео/карусель) через private API.
    Возвращает список {file_path, media_type, title} — как download_story.
    """
    if not has_any_session():
        raise RuntimeError(NO_ACCOUNTS_ERROR)
    return await with_rotation(lambda: _download_post_impl(url, download_dir))


async def _download_post_impl(url: str, download_dir: str) -> list[dict]:
    shortcode = parse_post_shortcode(url)
    media_pk = shortcode_to_media_pk(shortcode)
    logger.info(f"Post fallback: shortcode={shortcode}, pk={media_pk}")

    post = await get_media_info(media_pk)

    # карусель — несколько элементов, иначе один
    medias = post.get("carousel_media") or [post]

    # уникальный суффикс — чтобы параллельные запросы не затирали файлы друг друга
    uid = uuid.uuid4().hex[:8]

    results = []
    async with create_session() as session:
        try:
            for i, item in enumerate(medias):
                media_url, media_type = _pick_media_url(item)
                ext = ".mp4" if media_type == "video" else ".jpg"
                file_path = os.path.join(
                    download_dir, f"post_{shortcode}_{uid}_{i}{ext}"
                )
                await _download_file(session, media_url, file_path)

                title = "Instagram Reels" if media_type == "video" else "Instagram Фото"
                results.append({
                    "file_path": file_path,
                    "media_type": media_type,
                    "title": title,
                })
        except Exception:
            # не бросаем недокачанную карусель на диске — чистим за собой
            for r in results:
                try:
                    os.remove(r["file_path"])
                except OSError:
                    pass
            raise

    return results
