from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from .config import load_settings
from .db import connect, init_schema
from .handlers import admin, employee_order, registration
from .jobs import setup_scheduler
from .middleware import InjectMiddleware

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
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
    asyncio.run(main())


if __name__ == "__main__":
    run()
