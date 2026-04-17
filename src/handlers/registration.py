from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

import sqlite3

from .. import db
from ..config import Settings
from .common import admin_main_kb, employee_main_kb, is_admin

router = Router(name="registration")


class RegStates(StatesGroup):
    waiting_name = State()


@router.message(Command("myid"))
async def cmd_myid(message: Message) -> None:
    uid = message.from_user.id if message.from_user else 0
    username = message.from_user.username if message.from_user else None
    username_text = f"@{username}" if username else "(не задан)"
    await message.answer(
        f"Ваш Telegram ID: {uid}\n"
        f"Username: {username_text}"
    )


@router.message(Command("debug_admin"))
async def cmd_debug_admin(message: Message, state: FSMContext, settings: Settings) -> None:
    uid = message.from_user.id if message.from_user else 0
    username = message.from_user.username if message.from_user else None
    admin = is_admin(uid, settings)
    if not admin:
        await message.answer(
            "Команда только для админа.\n"
            f"Ваш ID: {uid}\n"
            "Проверьте, что он добавлен в ADMIN_IDS и перезапустите бота."
        )
        return

    current_state = await state.get_state()
    state_text = current_state if current_state is not None else "(нет активного состояния)"
    await message.answer(
        "Диагностика админа:\n"
        f"- ваш id: {uid}\n"
        f"- is_admin: {admin}\n"
        f"- ADMIN_IDS: {', '.join(str(x) for x in settings.admin_ids)}\n"
        f"- текущее состояние FSM: {state_text}\n"
        f"- test_mode: {settings.test_mode}"
    )


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
            text += "\nАдмин-панель — кнопка «Админ-панель»."
        await message.answer(
            text,
            reply_markup=admin_main_kb(settings) if is_admin(uid, settings) else employee_main_kb(),
        )
        return

    await state.set_state(RegStates.waiting_name)
    text = "Введите фамилию и имя через пробел, как в списке сотрудников.\nПример: Иванов Иван"
    if is_admin(uid, settings):
        text += "\nАдминка — кнопка «Админ-панель» после привязки."
    await message.answer(text, reply_markup=None)


# Не перехватывать команды — иначе они не дойдут до других роутеров
@router.message(RegStates.waiting_name, F.text, ~F.text.startswith("/"))
async def process_name(message: Message, state: FSMContext, conn: sqlite3.Connection, settings: Settings) -> None:
    raw = (message.text or "").strip()
    uid = message.from_user.id if message.from_user else 0
    admin = is_admin(uid, settings)

    if raw == "Админ-панель" and admin:
        await state.clear()
        await message.answer("Открываю админ-панель.", reply_markup=admin_main_kb(settings))
        return

    blocked = {
        "Заказ на сегодня",
        "Корзина",
        "Помощь",
        "Админ-панель",
        "Добавить сотрудника",
        "Список сотрудников",
        "Снять привязку",
        "Отключить сотрудника",
        "Удалить сотрудника",
        "Загрузить меню",
        "Сводка столовой",
        "Месячный отчёт",
        "Привязка для заказа",
        "Тест: меню всем",
        "Тест: меню мне",
        "Тест: закрыть заказы",
        "Тест: открыть заказы",
        "Тест: будний день",
        "Тест: выходной",
        "Тест: сброс",
    }
    if raw in blocked:
        await message.answer("Сначала введите фамилию и имя для привязки к списку сотрудников.")
        return

    parts = raw.split()
    if len(parts) < 2:
        await message.answer("Нужно минимум два слова: Фамилия Имя.")
        return
    last_name, first_name = parts[0], parts[1]
    emp = db.find_employee_by_name(conn, last_name, first_name)
    if not emp:
        if admin:
            # Для админа разрешаем саморегистрацию: создаём запись сотрудника при первом вводе ФИО.
            try:
                new_id = db.add_employee(conn, last_name, first_name)
            except sqlite3.IntegrityError:
                existing = db.find_employee_by_name_admin(conn, last_name, first_name)
                if not existing:
                    await message.answer("Не удалось создать сотрудника, попробуйте ещё раз.")
                    return
                if not existing.active:
                    db.activate_employee(conn, existing.id)
                emp = db.find_employee_by_name(conn, last_name, first_name)
                if not emp:
                    await message.answer("Не удалось активировать сотрудника, попробуйте ещё раз.")
                    return
            else:
                emp = db.find_employee_by_name(conn, last_name, first_name)
                if not emp:
                    await message.answer(f"Сотрудник создан (id={new_id}), но привязка не удалась. Повторите /start.")
                    return
        else:
            await message.answer("Сотрудник не найден. Проверьте написание или обратитесь к администратору.")
            return
    if emp.telegram_user_id is not None and emp.telegram_user_id != uid:
        await message.answer("Этот сотрудник уже привязан к другому Telegram-аккаунту.")
        return
    try:
        db.link_employee_telegram(conn, emp.id, uid, telegram_username=username)
    except sqlite3.IntegrityError:
        await message.answer("Этот Telegram уже привязан к другой записи. Обратитесь к администратору.")
        return

    await state.clear()
    if admin:
        await message.answer(
            "Привязка выполнена. Заказ — «Заказ на сегодня» или «Корзина».",
            reply_markup=admin_main_kb(settings),
        )
    else:
        await message.answer(
            f"Готово, {emp.first_name}! Меню приходит в будни около {settings.menu_broadcast_time}.",
            reply_markup=employee_main_kb(),
        )
