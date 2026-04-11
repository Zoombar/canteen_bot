from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from ..config import Settings


def is_admin(user_id: int, settings: Settings) -> bool:
    return user_id in settings.admin_ids


def employee_main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Заказ на сегодня")],
            [KeyboardButton(text="Корзина"), KeyboardButton(text="Помощь")],
        ],
        resize_keyboard=True,
    )


def admin_main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Сотрудники"), KeyboardButton(text="Загрузить меню")],
            [KeyboardButton(text="Сводка столовой"), KeyboardButton(text="Месячный отчёт")],
            [KeyboardButton(text="Заказ на сегодня")],
            [KeyboardButton(text="Корзина"), KeyboardButton(text="Помощь")],
            [KeyboardButton(text="Привязка для заказа")],
        ],
        resize_keyboard=True,
    )
