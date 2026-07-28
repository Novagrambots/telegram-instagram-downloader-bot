"""Проверка сохранённых Instagram-аккаунтов ЧЕРЕЗ ТОТ ЖЕ прокси, что у бота.

Важно: запускать на сервере (в контейнере бота), чтобы запрос шёл через
INSTAGRAM_PROXY — сессия, живая с твоего IP, может давать ошибку из-под
серверного прокси из-за скачка гео. Проверяем ровно тот путь, что у бота.

Запуск:
    docker compose exec bot python -m bot.check_session          # все аккаунты
    docker compose exec bot python -m bot.check_session username # конкретный
"""
import asyncio
import sys

from bot.config import settings
from bot.services.session_pool import (
    _build_client,
    list_accounts,
    proxy_kind,
)


async def check_one(name: str) -> tuple[bool, str]:
    """Проверяет один аккаунт. Возвращает (жив, сообщение)"""
    try:
        client = await asyncio.to_thread(_build_client, name)
        # account_info — то же, что приложение спрашивает при старте:
        # для валидной сессии это обычный запрос, для мёртвой — ошибка
        account = await asyncio.to_thread(client.account_info)
        return True, f"валидна (@{account.username})"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


async def main() -> None:
    print(f"Прокси: {settings.instagram_proxy or 'НЕТ (прямое подключение)'} ({proxy_kind()})")
    print(f"Папка сессий: {settings.instagram_sessions_dir}\n")

    names = sys.argv[1:] or list_accounts()
    if not names:
        print("Нет сохранённых аккаунтов — запусти python -m bot.login_account")
        return

    alive = 0
    for i, name in enumerate(names, 1):
        ok, msg = await check_one(name)
        alive += ok
        print(f"#{i} @{name} → {'✅' if ok else '❌'} {msg}")

    print(f"\nЖивых: {alive} из {len(names)}")


if __name__ == "__main__":
    asyncio.run(main())
