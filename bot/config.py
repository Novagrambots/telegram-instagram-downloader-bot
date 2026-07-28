"""Конфигурация бота — все настройки из .env"""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Токен бота
    bot_token: str

    # Username бота (для рекламной подписи под медиа)
    bot_username: str = "InstaLoaderBot"

    # PostgreSQL
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "bot_4_insta"
    db_user: str = "postgres"
    db_password: str = ""

    # Админы бота (через запятую в .env, например: 123,456)
    admin_ids: str = ""
    admin_username: str = "admin"  # username для 'По вопросам' в помощи

    # Кэш скачиваний (дни)
    cache_ttl_days: int = 30

    # Cobalt API (для скачивания фото и видео)
    cobalt_api_url: str = "http://localhost:9000"

    # Папка с файлами сессий Instagram (создаются через python -m bot.login_account)
    instagram_sessions_dir: str = "sessions"

    # Гео устройства — должно совпадать с гео прокси, иначе Instagram видит рассинхрон.
    # Применяется только при логине нового аккаунта, у живого локаль не меняется
    instagram_locale: str = "en_US"
    instagram_country: str = "US"
    instagram_tz_offset: int = 0  # смещение от UTC в секундах (напр. UZT +5ч = 18000)

    # Прокси для Instagram API (резидентный прокси, желательно sticky)
    instagram_proxy: str = ""

    # Local Bot API (пустая строка = стандартный API Telegram)
    local_bot_api_url: str = ""

    @property
    def admin_id_list(self) -> list[int]:
        """Парсит admin_ids из строки в список int"""
        if not self.admin_ids:
            return []
        return [int(x.strip()) for x in self.admin_ids.split(",") if x.strip()]

    @property
    def db_url(self) -> str:
        """URL для подключения к PostgreSQL через asyncpg"""
        return (
            f"postgresql+asyncpg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    # extra=ignore — в .env есть переменные для других сервисов (API_ID и т.д.)
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}



# глобальный экземпляр настроек
settings = Settings()
