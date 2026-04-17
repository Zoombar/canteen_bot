from __future__ import annotations

import asyncio
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramNetworkError
from aiogram.fsm.storage.memory import MemoryStorage

from .config import load_settings
from .db import connect, init_schema
from .handlers import admin, employee_order, fallback, registration
from .jobs import setup_scheduler
from .middleware import InjectMiddleware

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_MAX_BYTES = 5 * 1024 * 1024  # 5 MiB
_BACKUP_COUNT = 4  # текущий + 4 ротации = 5 файлов всего


def setup_logging() -> None:
    """Файлы: logs/bot.log, ротация 5×5 МиБ; дублирование в stderr для journalctl."""
    root_dir = Path(__file__).resolve().parent.parent
    log_dir = root_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "bot.log"

    fmt = logging.Formatter(_LOG_FORMAT)

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(fmt)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(stream_handler)

    logging.getLogger(__name__).info(
        "Логи: %s (ротация: до %s файлов по %s МиБ)",
        log_path.resolve(),
        _BACKUP_COUNT + 1,
        _MAX_BYTES // (1024 * 1024),
    )


logger = logging.getLogger(__name__)

# Таймаут HTTP к api.telegram.org (сек). На части хостингов дефолт aiogram мал — бывает timeout.
_TELEGRAM_HTTP_TIMEOUT = 120
# Повторы при сетевых таймаутах (не поможет, если 443 к Telegram заблокирован у провайдера).
_TELEGRAM_CONNECT_ATTEMPTS = 5


async def _ensure_telegram_api(bot: Bot) -> None:
    """getMe + проверка webhook; повторы при TelegramNetworkError."""
    last: BaseException | None = None
    for attempt in range(1, _TELEGRAM_CONNECT_ATTEMPTS + 1):
        try:
            me = await bot.get_me(request_timeout=_TELEGRAM_HTTP_TIMEOUT)
            logger.info(
                "Telegram: бот @%s (id=%s) — токен валиден, API доступен",
                me.username or "без_username",
                me.id,
            )
            wh = await bot.get_webhook_info(request_timeout=_TELEGRAM_HTTP_TIMEOUT)
            if wh.url:
                logger.warning(
                    "Настроен webhook %s — при polling апдейты могут не приходить. Сбрасываю webhook.",
                    wh.url,
                )
                await bot.delete_webhook(
                    drop_pending_updates=False, request_timeout=_TELEGRAM_HTTP_TIMEOUT
                )
                logger.info("Webhook сброшен, используется long polling.")
            else:
                logger.info("Webhook не задан — режим long polling.")
            return
        except TelegramNetworkError as e:
            last = e
            logger.warning(
                "Нет ответа от api.telegram.org (попытка %s/%s, timeout=%ss): %s",
                attempt,
                _TELEGRAM_CONNECT_ATTEMPTS,
                _TELEGRAM_HTTP_TIMEOUT,
                e,
            )
        except asyncio.TimeoutError as e:
            last = e
            logger.warning(
                "Таймаут при обращении к Telegram (попытка %s/%s): %s",
                attempt,
                _TELEGRAM_CONNECT_ATTEMPTS,
                e,
            )
        if attempt < _TELEGRAM_CONNECT_ATTEMPTS:
            await asyncio.sleep(min(30.0, 5.0 * attempt))

    logger.error(
        "Не удалось достучаться до Telegram API после %s попыток. "
        "Это почти всегда сеть/фаервол хостинга (исходящий HTTPS на api.telegram.org:443). "
        "Проверка с сервера: curl -v --connect-timeout 15 "
        "'https://api.telegram.org/bot<TOKEN>/getMe' — должен вернуться JSON, не таймаут.",
        _TELEGRAM_CONNECT_ATTEMPTS,
    )
    try:
        await bot.session.close()
    except Exception:
        pass
    if last is not None:
        raise last
    raise RuntimeError("Telegram API unreachable")


async def main() -> None:
    settings = load_settings()
    if not settings.bot_token:
        raise SystemExit("Укажите BOT_TOKEN в файле .env (см. .env.example).")
    if not settings.admin_ids:
        logger.warning("ADMIN_IDS пуст — админ-панель и месячные отчёты будут недоступны.")
    if settings.test_mode:
        logger.warning(
            "TEST_MODE=true: заказы в выходные и без дедлайна; тестовые кнопки у админов."
        )

    conn = connect(settings.db_path)
    init_schema(conn)

    bot = Bot(token=settings.bot_token)
    try:
        await _ensure_telegram_api(bot)
    except Exception:
        logger.exception(
            "См. сообщение выше: часто нужен исходящий доступ к api.telegram.org или смена хостинга/VPN."
        )
        raise

    dp = Dispatcher(storage=MemoryStorage())
    dp.update.middleware(InjectMiddleware(conn, settings))

    dp.include_router(registration.router)
    dp.include_router(admin.router)
    dp.include_router(employee_order.router)
    dp.include_router(fallback.router)

    scheduler = setup_scheduler(bot, conn, settings)
    scheduler.start()
    logger.info("Планировщик запущен")

    await dp.start_polling(bot)


def run() -> None:
    setup_logging()
    asyncio.run(main())


if __name__ == "__main__":
    run()
