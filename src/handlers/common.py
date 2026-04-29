from __future__ import annotations

from aiogram.filters import BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, KeyboardButton, Message, ReplyKeyboardMarkup

from ..config import Settings


def is_admin(user_id: int, settings: Settings) -> bool:
    return user_id in settings.admin_ids


class OrderUiNotBlocked(BaseFilter):
    """Не открывать заказ, пока идёт ввод ФИО при регистрации (/start)."""

    async def __call__(self, event: Message | CallbackQuery, state: FSMContext) -> bool:
        s = await state.get_state()
        if s is None:
            return True
        ss = str(s)
        return not ss.startswith("RegStates:")


def employee_main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Заказ на сегодня")],
            [KeyboardButton(text="Корзина"), KeyboardButton(text="Помощь")],
        ],
        resize_keyboard=True,
    )


def admin_main_kb(settings: Settings) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = [
        [KeyboardButton(text="Админ-панель")],
        [KeyboardButton(text="Настройки уведомлений")],
        [KeyboardButton(text="Список сотрудников")],
        [KeyboardButton(text="Загрузить меню")],
        [KeyboardButton(text="Месячный отчёт")],
        [KeyboardButton(text="Сводка в столовую")],
    ]
    if settings.test_mode:
        rows.extend(
            [
                [
                    KeyboardButton(text="Тест: меню всем"),
                    KeyboardButton(text="Тест: меню мне"),
                ],
                [
                    KeyboardButton(text="Тест: закрыть заказы"),
                    KeyboardButton(text="Тест: открыть заказы"),
                ],
                [
                    KeyboardButton(text="Тест: будний день"),
                    KeyboardButton(text="Тест: выходной"),
                ],
                [KeyboardButton(text="Тест: сброс")],
                [KeyboardButton(text="Тест: проверить IMAP")],
            ]
        )
    rows.extend(
        [
            [KeyboardButton(text="Заказ на сегодня")],
            [
                KeyboardButton(text="Корзина"),
                KeyboardButton(text="Помощь"),
            ],
        ]
    )
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)
