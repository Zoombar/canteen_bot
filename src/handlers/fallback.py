from __future__ import annotations

import sqlite3

from aiogram import F, Router
from aiogram.types import Message

from .. import db
from ..config import Settings
from .common import OrderUiNotBlocked, admin_main_kb, employee_main_kb, is_admin

router = Router(name="fallback")


@router.message(OrderUiNotBlocked(), F.text)
async def recover_keyboard_for_known_user(
    message: Message,
    conn: sqlite3.Connection,
    settings: Settings,
) -> None:
    """
    Fallback для случаев после деплоя/обновления:
    если пользователь пишет любое сообщение, но кнопки не отображаются,
    возвращаем подходящую клавиатуру без обязательного /start.
    """
    uid = message.from_user.id if message.from_user else 0
    emp = db.get_employee_by_tg(conn, uid)

    if is_admin(uid, settings):
        await message.answer(
            "Клавиатура обновлена. Откройте «Админ-панель».",
            reply_markup=admin_main_kb(settings),
        )
        return

    if emp:
        await message.answer(
            "Клавиатура обновлена.",
            reply_markup=employee_main_kb(),
        )
        return

    await message.answer(
        "Похоже, вы ещё не привязаны к сотруднику. Выполните /start.",
        reply_markup=None,
    )

