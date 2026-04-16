from __future__ import annotations

from aiogram.filters import BaseFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, KeyboardButton, Message, ReplyKeyboardMarkup

from ..config import Settings


def is_admin(user_id: int, settings: Settings) -> bool:
    return user_id in settings.admin_ids


class OrderUiNotBlocked(BaseFilter):
    """Не открывать заказ, пока идёт ввод ФИО (регистрация или админ-формы)."""

    async def __call__(self, event: Message | CallbackQuery, state: FSMContext) -> bool:
        s = await state.get_state()
        if s is None:
            return True
        ss = str(s)
        return not (ss.startswith("RegStates:") or ss.startswith("AdminStates:"))


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
        [
            KeyboardButton(text="Добавить сотрудника"),
            KeyboardButton(text="Список сотрудников"),
        ],
        [
            KeyboardButton(text="Снять привязку"),
            KeyboardButton(text="Отключить сотрудника"),
        ],
        [
            KeyboardButton(text="Загрузить меню"),
            KeyboardButton(text="Сводка столовой"),
        ],
        [KeyboardButton(text="Месячный отчёт")],
    ]
    rows.extend(
        [
            [KeyboardButton(text="Заказ на сегодня")],
            [
                KeyboardButton(text="Корзина"),
                KeyboardButton(text="Помощь"),
            ],
            [KeyboardButton(text="Привязка для заказа")],
        ]
    )
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)
