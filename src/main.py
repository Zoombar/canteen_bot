from __future__ import annotations

import asyncio
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from .config import load_settings
from .db import connect, init_schema
from .handlers import admin, employee_order, registration
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
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.middleware(InjectMiddleware(conn, settings))

    dp.include_router(registration.router)
    dp.include_router(admin.router)
    dp.include_router(employee_order.router)

    scheduler = setup_scheduler(bot, conn, settings)
    scheduler.start()
    logger.info("Планировщик запущен")

    await dp.start_polling(bot)


def run() -> None:
    setup_logging()
    asyncio.run(main())


if __name__ == "__main__":
    run()
