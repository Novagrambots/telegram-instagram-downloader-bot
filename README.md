# 🎬 Instagram Downloader Bot

Telegram-бот для скачивания видео, Reels, фото, каруселей и Stories из Instagram.

---

## ✨ Возможности

| Функция | Описание |
|---------|----------|
| 📥 Скачивание | Видео, Reels, фото, карусели (альбомом) и Stories по ссылке |
| 📦 Большие файлы | До 2 ГБ через Local Bot API |
| ⚡ Кэширование | Повторные запросы — мгновенно (file_id, TTL 30 дней) |
| 🌐 Мультиязычность | Русский / Узбекский / English + автоопределение |
| 🔔 Подписка на каналы | Обязательная подписка для использования |
| 🔄 Pending URL | Автоскачивание ссылки после подписки |
| 🚦 Rate limit | 5 запросов в минуту на пользователя |
| 📣 Массовая рассылка | Рассылка по всем юзерам из админки |
| 🔧 Админ-панель | Статистика, управление каналами, рассылка |
| 🎨 UI | Premium custom эмодзи, нативное меню Telegram |

## 🛠 Стек технологий

- **Python 3.12** + **aiogram 3** — асинхронный Telegram Bot API
- **PostgreSQL 16** + **SQLAlchemy 2.0** (async, asyncpg) — база данных
- **Cobalt API** (Docker) — движок скачивания видео/Reels/фото
- **Playwright** — анонимные viewer-сайты: Stories и аватарки без Instagram-аккаунтов
- **instagrapi** — Instagram Private API, резервный путь (умеет закрытые профили)
- **Telegram Local Bot API** — поддержка файлов до 2 ГБ
- **aiohttp** + **aiohttp-socks** — HTTP-клиент с поддержкой HTTP/HTTPS/SOCKS4/SOCKS5 прокси
- **pydantic-settings** — конфигурация

## 📁 Структура проекта

```
bot_4_insta/
├── bot/
│   ├── config.py              # Настройки из .env
│   ├── i18n.py                # Переводы (ru/uz/en)
│   ├── emojis.py              # Premium custom эмодзи
│   ├── main.py                # Точка входа
│   ├── login_account.py       # Логин Instagram-аккаунта (вручную)
│   ├── check_session.py       # Проверка живости аккаунтов
│   ├── database/
│   │   ├── __init__.py        # Подключение к БД
│   │   ├── models.py          # User, Channel, Download
│   │   └── crud.py            # CRUD операции
│   ├── handlers/
│   │   ├── start.py           # /start, меню, язык, подписка
│   │   ├── download.py        # Скачивание по ссылке
│   │   ├── inline.py          # Inline-режим (@бот ссылка)
│   │   └── admin.py           # Админ-панель + рассылка
│   ├── keyboards/
│   │   ├── inline.py          # Основные кнопки
│   │   └── admin.py           # Кнопки админки
│   ├── middlewares/
│   │   ├── rate_limit.py      # 5 запросов в минуту
│   │   └── subscription.py    # Проверка подписки
│   ├── services/
│   │   ├── instagram.py       # Cobalt API клиент
│   │   ├── story_scraper.py   # Анонимные viewer-сайты через Playwright
│   │   ├── session_pool.py    # Пул Instagram-аккаунтов + ротация
│   │   ├── stories.py         # Stories: скрапер → аккаунты
│   │   ├── posts.py           # Посты через private API (fallback Cobalt)
│   │   └── profile.py         # Аватарки профилей
│   └── utils/
│       └── helpers.py         # URL валидация
├── sessions/                  # Файлы сессий Instagram (не в git)
├── docker-compose.yml         # Бот + БД + Cobalt + Local Bot API
├── Dockerfile
├── .env.example               # Шаблон переменных
├── requirements.txt           # Зависимости
└── README.md
```

## ⚡ Быстрый старт (Docker Compose)

```bash
# 1. Клонирование
git clone <repo-url> && cd bot_4_insta

# 2. Настройки
cp .env.example .env
# заполнить .env (BOT_TOKEN, ADMIN_IDS, API_ID, API_HASH)

# 3. Запуск (поднимает бот, PostgreSQL, Cobalt, Local Bot API)
docker compose up -d --build

# 4. Логи
docker compose logs -f bot

# 5. Обновление
git pull && docker compose up -d --build
```

## 🖥 Локальный запуск (для разработки)

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# нужны внешние сервисы: PostgreSQL + Cobalt
python -m bot.main
```

## ⚙️ Переменные окружения

| Переменная | Описание |
|-----------|----------|
| `BOT_TOKEN` | Токен от @BotFather |
| `ADMIN_IDS` | Telegram ID админов (через запятую) |
| `INSTAGRAM_SESSIONS_DIR` | Папка с файлами сессий Instagram (по умолчанию `sessions`) |
| `INSTAGRAM_PROXY` | URL прокси для private API (резидентный, sticky) |
| `SCRAPER_PROXY` | Прокси для viewer-сайтов, по умолчанию пусто (напрямую) |
| `INSTAGRAM_LOCALE`, `INSTAGRAM_COUNTRY`, `INSTAGRAM_TZ_OFFSET` | Гео устройства — под гео прокси |
| `COBALT_API_URL` | URL Cobalt API |
| `LOCAL_BOT_API_URL` | URL Local Bot API (для файлов >50 МБ) |
| `API_ID`, `API_HASH` | Telegram API credentials для Local Bot API |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | Параметры PostgreSQL |
| `CACHE_TTL_DAYS` | TTL кэша скачиваний (по умолчанию 30) |

## 📸 Источники контента

| Что качаем | Основной путь | Резерв | Нужен аккаунт |
|-----------|---------------|--------|---------------|
| Reels, фото, карусели | Cobalt API | — | нет |
| Одиночные посты и Reels (если Cobalt не смог) | viewer-сайты (Playwright) | private API | нет |
| **Stories** | viewer-сайты (Playwright) | private API | **нет** |
| **Аватарки** | viewer-сайты (Playwright) | private API | **нет** |
| Карусели (если Cobalt не смог), закрытые профили | private API | — | да |

Анонимные viewer-сайты не отдают карусели — на пост с несколькими медиа приходит
пустой ответ. Обычно это не мешает: карусели забирает Cobalt. Аккаунт понадобится
только если Cobalt не справился именно с каруселью.

Основной путь для Stories — анонимные viewer-сайты: браузер открывает страницу,
её JS подписывает запрос, а мы забираем JSON. Структура ответа совпадает с private
API, медиа качается напрямую с CDN Instagram. Аккаунты для этого не нужны.

Instagram-аккаунты остаются резервом. Они умеют то, чего анонимные сервисы не
могут: закрытые профили, на которые подписан аккаунт. Без аккаунтов бот работает,
просто закрытые профили будут недоступны.

```bash
# добавить аккаунт (спросит логин и пароль, при необходимости — код с почты)
docker compose exec bot python -m bot.login_account

# быстрый старт по cookie из браузера, без пароля (живёт меньше)
docker compose exec bot python -m bot.login_account --sessionid

# проверить какие аккаунты живы
docker compose exec bot python -m bot.check_session
```

Каждый аккаунт сохраняется в `sessions/<username>.json` вместе с device-профилем
(модель телефона, android_id, user-agent). Этот профиль неизменен — для Instagram
аккаунт выглядит как один и тот же телефон, а не как новый вход при каждом рестарте.

Бот сам ротирует аккаунты: поймал challenge или блокировку — уводит в кулдаун на
6 часов и переключается на следующий. Когда живых не остаётся, админам приходит алерт.

**Правила, без которых аккаунты горят:**

- **Не использовать личный аккаунт.** Только расходники.
- **Прокси резидентный и sticky.** Если IP меняется на каждый запрос, аккаунт
  «прыгает» между городами — это бан. Логиниться нужно через тот же прокси,
  с которого потом работает бот.
- **Прогрев 3-7 дней.** Свежерег, который сразу лезет в private API, живёт часы:
  аватарка, пара постов, подписки, ручной вход с телефона.
- **Гео устройства = гео прокси** (`INSTAGRAM_LOCALE`, `INSTAGRAM_COUNTRY`, `INSTAGRAM_TZ_OFFSET`).
- **Не логиниться заново без нужды.** Каждый новый вход — сигнал для Instagram.
  Файлы `sessions/` лежат в volume и переживают пересборку контейнера.

## 👨‍💻 Админ-панель

Доступ: кнопка «Админ-панель» в меню (только для `ADMIN_IDS`).

- 📊 **Статистика** — юзеры, скачивания, каналы
- 📢 **Каналы** — добавить/удалить каналы обязательной подписки (FSM)
- 📣 **Рассылка** — массовое сообщение всем юзерам (FSM с подтверждением)
- 🏠 **Главное меню** — вернуться в бота

Также приходят алерты, когда все Instagram-аккаунты уходят в кулдаун.

## 📄 Лицензия

MIT
