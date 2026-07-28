"""Пул Instagram-аккаунтов на instagrapi: ротация + кулдаун.

Аккаунт — это JSON-файл сессии в settings.instagram_sessions_dir, создаётся
командой `python -m bot.login_account`. Внутри лежит device-профиль (android_id,
uuid, user-agent) и cookies. Device никогда не меняется — для Instagram это один
и тот же телефон, а не новый вход с нового устройства при каждом рестарте бота.
Раньше мы брали sessionid из браузера и стучали им в мобильный API — вот из-за
этого рассинхрона аккаунты и горели за часы.

Активный аккаунт работает, пока не словит challenge/feedback/login_required —
тогда уходит в кулдаун, а пул переключается на следующий живой.
"""
import asyncio
import functools
import logging
import time
from pathlib import Path
from typing import Awaitable, Callable, TypeVar

from instagrapi import Client
from instagrapi.exceptions import (
    AccountSuspended,
    CaptchaChallengeRequired,
    ChallengeRequired,
    ClientForbiddenError,
    ClientLoginRequired,
    ClientThrottledError,
    ClientUnauthorizedError,
    FeedbackRequired,
    LoginRequired,
    PleaseWaitFewMinutes,
    ProxyAddressIsBlocked,
    RateLimitError,
    RecaptchaChallengeForm,
    SelectContactPointRecoveryForm,
    SentryBlock,
    TwoFactorRequired,
)

from bot.config import settings

logger = logging.getLogger(__name__)

T = TypeVar("T")

# аккаунт выгорел: Instagram требует подтвердить личность или заблокировал
_BURNED = (
    LoginRequired,
    ClientLoginRequired,
    ClientUnauthorizedError,
    ChallengeRequired,
    CaptchaChallengeRequired,
    RecaptchaChallengeForm,
    SelectContactPointRecoveryForm,
    TwoFactorRequired,
    FeedbackRequired,
    SentryBlock,
    AccountSuspended,
    ClientForbiddenError,
    ProxyAddressIsBlocked,
)
# 6 часов — раньше Instagram всё равно не отпускает
_BURNED_COOLDOWN = 6 * 60 * 60

# временный троттлинг: аккаунт живой, просто просят подождать
_THROTTLED = (PleaseWaitFewMinutes, RateLimitError, ClientThrottledError)
_THROTTLED_COOLDOWN = 15 * 60

# таймаут HTTP-запросов к Instagram (сек)
_HTTP_TIMEOUT = 15

# пауза между запросами внутри клиента — instagrapi спит случайное время из диапазона,
# чтобы запросы не шли ровной очередью как у робота
_DELAY_RANGE = [1, 3]


class SessionExpiredError(RuntimeError):
    """Активный аккаунт заблокирован — уведён в кулдаун"""


class AllSessionsExpiredError(SessionExpiredError):
    """Все аккаунты выгорели (в кулдауне) — нужен перелогин"""


# {имя аккаунта: клиент} — клиенты живут между запросами, чтобы не перечитывать файл
_clients: dict[str, Client] = {}
# {имя аккаунта: timestamp когда кулдаун закончится}
_cooldown: dict[str, float] = {}


def _sessions_dir() -> Path:
    return Path(settings.instagram_sessions_dir)


def proxy_kind() -> str:
    """Тип прокси для логов: socks5, http или нет"""
    proxy_url = settings.instagram_proxy or None
    if not proxy_url:
        return "нет"
    if proxy_url.startswith("socks"):
        return proxy_url.split("://")[0]
    return "http"


def list_accounts() -> list[str]:
    """Имена аккаунтов — это имена файлов сессий без .json, по алфавиту"""
    directory = _sessions_dir()
    if not directory.is_dir():
        return []
    return sorted(p.stem for p in directory.glob("*.json"))


def has_any_session() -> bool:
    """Есть ли вообще хоть один сохранённый аккаунт"""
    return bool(list_accounts())


def _alive_accounts() -> list[str]:
    """Аккаунты не в кулдауне"""
    now = time.time()
    return [name for name in list_accounts() if _cooldown.get(name, 0) <= now]


def _build_client(name: str) -> Client:
    """Поднимает клиента из сохранённой сессии — device и cookies уже внутри файла"""
    client = Client()
    client.load_settings(_sessions_dir() / f"{name}.json")

    if settings.instagram_proxy:
        client.set_proxy(settings.instagram_proxy)

    client.delay_range = list(_DELAY_RANGE)
    # requests не умеет дефолтный таймаут на всю сессию — подставляем его через
    # partial, иначе запрос к Instagram может висеть бесконечно
    client.private.request = functools.partial(
        client.private.request, timeout=_HTTP_TIMEOUT
    )

    logger.info(f"Аккаунт @{name} загружен (прокси: {proxy_kind()})")
    return client


def get_client() -> tuple[str, Client]:
    """Возвращает (имя, клиент) активного аккаунта.
    Бросает AllSessionsExpiredError если все выгорели или ни одного не сохранено.
    """
    if not list_accounts():
        raise AllSessionsExpiredError(
            f"Нет сохранённых аккаунтов в {_sessions_dir()} — "
            "запусти python -m bot.login_account"
        )

    alive = _alive_accounts()
    if not alive:
        raise AllSessionsExpiredError("Все Instagram-аккаунты в кулдауне")

    # sticky: держимся первого живого, чтобы не размазывать нагрузку по всем сразу
    name = alive[0]
    if name not in _clients:
        _clients[name] = _build_client(name)
    return name, _clients[name]


def report_bad(name: str, reason: str, seconds: int = _BURNED_COOLDOWN) -> None:
    """Помечает аккаунт как выгоревший — уводит в кулдаун"""
    _cooldown[name] = time.time() + seconds
    # клиента с мёртвой сессией переиспользовать нельзя
    _clients.pop(name, None)
    logger.warning(
        f"Аккаунт @{name} в кулдаун на {seconds // 60} мин: {reason}. "
        f"Живых аккаунтов осталось: {len(_alive_accounts())}"
    )


def _handle_error(name: str, exc: Exception) -> None:
    """Если ошибка означает блокировку аккаунта — уводит его в кулдаун и бросает
    SessionExpiredError. Остальные ошибки (нет юзера, сеть) не трогает —
    вызывающий пробросит их сам.
    """
    if isinstance(exc, _BURNED):
        report_bad(name, f"{type(exc).__name__}: {exc}")
        raise SessionExpiredError(
            f"Аккаунт @{name} заблокирован ({type(exc).__name__})"
        ) from exc

    if isinstance(exc, _THROTTLED):
        report_bad(name, type(exc).__name__, _THROTTLED_COOLDOWN)
        raise SessionExpiredError(
            f"Аккаунт @{name} под троттлингом ({type(exc).__name__})"
        ) from exc


async def run(fn: Callable[[Client], T]) -> T:
    """Выполняет операцию активным клиентом в отдельном потоке (instagrapi синхронная).
    Блокировку аккаунта превращает в SessionExpiredError и уводит его в кулдаун.
    Переключением на следующий аккаунт занимается with_rotation снаружи.
    """
    name, client = get_client()
    try:
        return await asyncio.to_thread(fn, client)
    except Exception as e:
        _handle_error(name, e)
        raise


async def private_request(endpoint: str, params: dict | None = None) -> dict:
    """Сырой запрос к private API — когда нужен полный JSON, а не модель instagrapi
    (например все версии видео, чтобы выбрать подходящую по размеру)
    """
    return await run(lambda cl: cl.private_request(endpoint, params=params))


# сериализует запросы к private API: параллельный долбёж одним аккаунтом выглядит
# для Instagram как бот. К тому же не даёт наплыву запросов пройти по уже
# выгорающему аккаунту до выставления кулдауна.
_rotation_lock = asyncio.Lock()


async def with_rotation(op: Callable[[], Awaitable[T]]) -> T:
    """Выполняет операцию, при выгорании активного аккаунта переключается на
    следующий и повторяет. op — функция без аргументов, берущая клиента через
    run()/private_request() внутри. Запросы сериализованы — одновременно идёт
    только одна операция.
    """
    attempts = max(1, len(list_accounts()))
    last_err: SessionExpiredError | None = None

    async with _rotation_lock:
        for _ in range(attempts):
            try:
                return await op()
            except AllSessionsExpiredError:
                raise  # живых аккаунтов нет — повторять нечем
            except SessionExpiredError as e:
                last_err = e
                # аккаунт уже уведён в кулдаун внутри run(); пробуем следующий
                if not _alive_accounts():
                    break

    # попытки исчерпаны — все аккаунты выгорели
    raise last_err or AllSessionsExpiredError("Все Instagram-аккаунты выгорели")
