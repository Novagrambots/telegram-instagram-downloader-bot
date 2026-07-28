"""Первичный логин Instagram-аккаунта — создаёт файл сессии для пула.

Запускать ВРУЧНУЮ и по одному разу на аккаунт. Instagram может попросить код
подтверждения с почты — ответить на это может только человек, поэтому автоматом
в проде такое не делается.

Логиниться нужно ЧЕРЕЗ ТОТ ЖЕ ПРОКСИ, с которого потом будет работать бот:
если сессия родилась на одном IP, а запросы пойдут с другого, Instagram увидит
скачок гео и попросит подтвердить личность.

Запуск:
    docker compose exec bot python -m bot.login_account              # по паролю
    docker compose exec bot python -m bot.login_account --sessionid  # по cookie из браузера
"""
import sys
from getpass import getpass
from pathlib import Path

from instagrapi import Client
from instagrapi.exceptions import TwoFactorRequired

from bot.config import settings
from bot.services.session_pool import proxy_kind


def _code_handler(username: str, choice) -> str:
    """Instagram прислал код на почту/SMS — спрашиваем его у человека"""
    print(f"\nInstagram запросил подтверждение для @{username} (способ: {choice})")
    return input("Введи код из письма/SMS: ").strip()


def _make_client() -> Client:
    """Новый клиент со свежим device-профилем и нашим прокси"""
    client = Client()

    if settings.instagram_proxy:
        client.set_proxy(settings.instagram_proxy)

    # гео устройства должно совпадать с гео прокси, иначе это ещё один сигнал бота.
    # Меняется только здесь, при рождении сессии: у живого аккаунта локаль не скачет
    client.set_locale(settings.instagram_locale)
    client.set_country(settings.instagram_country)
    client.set_timezone_offset(settings.instagram_tz_offset)

    client.delay_range = [1, 3]
    return client


def _save(client: Client) -> Path:
    """Сохраняет сессию в файл пула"""
    directory = Path(settings.instagram_sessions_dir)
    directory.mkdir(parents=True, exist_ok=True)

    path = directory / f"{client.username}.json"
    client.dump_settings(path)
    return path


def _verify(client: Client) -> None:
    """Проверяем что сессия реально работает — сразу, а не в проде под нагрузкой"""
    info = client.user_info_by_username(client.username)
    print(f"Проверка: @{info.username} ({info.follower_count} подписчиков) — сессия живая")


def login_by_password() -> None:
    """Полноценный логин — instagrapi заходит как мобильное приложение"""
    username = input("Username (без @): ").strip().lstrip("@")
    password = getpass("Пароль (ввод не отображается): ").strip()

    client = _make_client()
    client.challenge_code_handler = _code_handler

    try:
        client.login(username, password)
    except TwoFactorRequired:
        code = input("Код двухфакторной аутентификации: ").strip()
        client.login(username, password, verification_code=code)

    _verify(client)
    path = _save(client)
    print(f"\nГотово: {path}")


def login_by_sessionid() -> None:
    """Быстрый старт по cookie из браузера — без пароля.

    Компромисс: сессия остаётся браузерной, а запросы пойдут с мобильным
    device-профилем. Это лучше, чем было (device хотя бы перестанет меняться
    при каждом рестарте), но живёт заметно меньше полноценного логина.
    """
    sessionid = input("sessionid из cookies браузера: ").strip()

    client = _make_client()
    client.login_by_sessionid(sessionid)

    _verify(client)
    path = _save(client)
    print(f"\nГотово: {path}")
    print("Совет: как появится возможность — перелогинься по паролю, сессия проживёт дольше")


def main() -> None:
    print(f"Прокси: {settings.instagram_proxy or 'НЕТ (прямое подключение)'} ({proxy_kind()})")
    print(f"Папка сессий: {settings.instagram_sessions_dir}")
    print(f"Гео устройства: {settings.instagram_locale} / {settings.instagram_country}\n")

    if not settings.instagram_proxy:
        print("Внимание: прокси не задан. Сессия родится на IP этой машины —")
        print("если бот работает с другого IP, Instagram попросит подтверждение.\n")

    try:
        if "--sessionid" in sys.argv:
            login_by_sessionid()
        else:
            login_by_password()
    except KeyboardInterrupt:
        print("\nОтменено")
    except Exception as e:
        print(f"\nНе получилось: {type(e).__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
