from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

import sqlite3

from .. import db
from ..config import Settings
from .common import employee_main_kb, is_admin

router = Router(name="registration")


class RegStates(StatesGroup):
    waiting_name = State()


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext, conn: sqlite3.Connection, settings: Settings) -> None:
    await state.clear()
    uid = message.from_user.id if message.from_user else 0

    emp = db.get_employee_by_tg(conn, uid)
    if emp:
        text = (
            f"Здравствуйте, {emp.first_name} {emp.last_name}!\n"
            "Меню приходит утром в будни. Заказ — «Заказ на сегодня»."
        )
        if is_admin(uid, settings):
            text += "\nАдмин-панель открывается командой /admin."
        await message.answer(text, reply_markup=employee_main_kb())
        return

    await state.set_state(RegStates.waiting_name)
    text = "Введите фамилию и имя через пробел, как в списке сотрудников.\nПример: Иванов Иван"
    if is_admin(uid, settings):
        text += "\nЕсли нужно открыть админку: /admin."
    await message.answer(text, reply_markup=None)


@router.message(RegStates.waiting_name, F.text)
async def process_name(message: Message, state: FSMContext, conn: sqlite3.Connection, settings: Settings) -> None:
    raw = (message.text or "").strip()
    blocked = {
        "Заказ на сегодня",
        "Корзина",
        "Помощь",
        "Сотрудники",
        "Загрузить меню",
        "Сводка столовой",
        "Месячный отчёт",
        "Привязка для заказа",
    }
    if raw in blocked:
        await message.answer("Сначала введите фамилию и имя для привязки к списку сотрудников.")
        return
    uid = message.from_user.id if message.from_user else 0
    parts = raw.split()
    if len(parts) < 2:
        await message.answer("Нужно минимум два слова: Фамилия Имя.")
        return
    last_name, first_name = parts[0], parts[1]
    emp = db.find_employee_by_name(conn, last_name, first_name)
    if not emp:
        await message.answer("Сотрудник не найден. Проверьте написание или обратитесь к администратору.")
        return
    if emp.telegram_user_id is not None and emp.telegram_user_id != uid:
        await message.answer("Этот сотрудник уже привязан к другому Telegram-аккаунту.")
        return
    try:
        db.link_employee_telegram(conn, emp.id, uid)
    except sqlite3.IntegrityError:
        await message.answer("Этот Telegram уже привязан к другой записи. Обратитесь к администратору.")
        return

    await state.clear()
    if is_admin(uid, settings):
        await message.answer(
            "Привязка выполнена. Заказ — «Заказ на сегодня» или «Корзина».",
            reply_markup=employee_main_kb(),
        )
    else:
        await message.answer(
            f"Готово, {emp.first_name}! Меню приходит в будни около 8:30 (Омск).",
            reply_markup=employee_main_kb(),
        )
