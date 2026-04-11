from __future__ import annotations

import sqlite3
from typing import Any

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import db
from ..config import Settings
from ..timeutil import is_deadline_passed, is_weekday_effective, local_today
from .common import employee_main_kb

router = Router(name="employee_order")

PAGE_SIZE = 8
MAX_DISH_TYPES = 4


def _deadline_blocks(settings: Settings) -> bool:
    """True — дедлайн заказа на сегодня считается наступившим (заказ нельзя менять)."""
    if settings.test_mode:
        return False
    return is_deadline_passed(settings.tz, settings.order_deadline_time)


def _emp_id(conn: sqlite3.Connection, uid: int) -> int | None:
    e = db.get_employee_by_tg(conn, uid)
    return e.id if e else None


def _menu_lines(items: list[db.MenuItemRow]) -> str:
    lines = ["Меню на сегодня:"]
    for it in items:
        lines.append(f"• {it.dish_name} — {it.price:.2f} руб.")
    return "\n".join(lines)


def _cart_text(conn: sqlite3.Connection, order_id: int) -> str:
    rows = db.list_order_items_with_menu(conn, order_id)
    if not rows:
        return "Корзина пуста."
    lines = ["Ваш заказ:"]
    total = 0.0
    for mi, q in rows:
        line_sum = mi.price * q
        total += line_sum
        lines.append(f"• {mi.dish_name} × {q} = {line_sum:.2f} руб.")
    lines.append(f"\nИтого: {total:.2f} руб.")
    return "\n".join(lines)


def _menu_kb(page: int, items: list[db.MenuItemRow]) -> InlineKeyboardMarkup:
    start = page * PAGE_SIZE
    chunk = items[start : start + PAGE_SIZE]
    rows: list[list[InlineKeyboardButton]] = []
    for it in chunk:
        short = it.dish_name if len(it.dish_name) <= 18 else it.dish_name[:17] + "…"
        rows.append(
            [
                InlineKeyboardButton(text=f"{short} ＋", callback_data=f"+:{it.id}"),
                InlineKeyboardButton(text="－", callback_data=f"-:{it.id}"),
            ]
        )
    nav: list[InlineKeyboardButton] = []
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    if page > 0:
        nav.append(InlineKeyboardButton(text="«", callback_data=f"m:{page-1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="»", callback_data=f"m:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="Корзина", callback_data="cart")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _open_menu(
    message: Message,
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    page: int = 0,
) -> None:
    uid = message.from_user.id if message.from_user else 0
    eid = _emp_id(conn, uid)
    if not eid:
        await message.answer("Сначала выполните /start и привязку ФИО.")
        return
    if not settings.test_mode and not is_weekday_effective(settings.tz):
        await message.answer("Сегодня выходной — заказ не принимается.")
        return
    today = local_today(settings.tz)
    mid = db.get_menu_for_date(conn, today)
    if not mid:
        await message.answer("Меню на сегодня ещё не загружено.")
        return
    items = db.list_menu_items(conn, mid)
    if not items:
        await message.answer("Меню пустое.")
        return

    od = db.get_order_for_employee_date(conn, eid, today)
    if od and od[1] == "confirmed":
        oid = od[0]
        await message.answer(
            "Ваш заказ уже подтверждён и не может быть изменён.\n\n" + _cart_text(conn, oid),
            reply_markup=employee_main_kb(),
        )
        return

    if _deadline_blocks(settings):
        await message.answer(
            "Время приёма заказов на сегодня истекло.",
            reply_markup=employee_main_kb(),
        )
        return

    text = _menu_lines(items)
    await message.answer(text, reply_markup=_menu_kb(page, items))


@router.message(F.text == "Заказ на сегодня")
async def text_order_today(message: Message, conn: sqlite3.Connection, settings: Settings) -> None:
    await _open_menu(message, conn, settings, page=0)


@router.message(F.text == "Корзина")
async def text_cart(message: Message, conn: sqlite3.Connection, settings: Settings) -> None:
    uid = message.from_user.id if message.from_user else 0
    eid = _emp_id(conn, uid)
    if not eid:
        await message.answer("Сначала выполните /start и привязку ФИО.")
        return
    today = local_today(settings.tz)
    mid = db.get_menu_for_date(conn, today)
    if not mid:
        await message.answer("Меню на сегодня ещё не загружено.")
        return
    od = db.get_order_for_employee_date(conn, eid, today)
    if not od:
        oid = db.get_or_create_draft_order(conn, eid, today)
    else:
        oid = od[0]
    st = od[1] if od else "draft"
    if st == "confirmed":
        await message.answer("Заказ уже подтверждён.\n\n" + _cart_text(conn, oid))
        return
    if _deadline_blocks(settings):
        await message.answer("Время приёма заказов на сегодня истекло.")
        return

    text = _cart_text(conn, oid)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Подтвердить заказ", callback_data="cf"),
                InlineKeyboardButton(text="К меню", callback_data="m:0"),
            ]
        ]
    )
    await message.answer(text, reply_markup=kb)


@router.message(F.text == "Помощь")
async def text_help(message: Message, settings: Settings) -> None:
    line_menu = (
        f"• В будни меню рассылается около {settings.menu_broadcast_time}.\n"
        if not settings.test_mode
        else f"• Меню по расписанию в будни около {settings.menu_broadcast_time}; в режиме TEST_MODE заказы и в выходные.\n"
    )
    line_deadline = (
        f"• Заказ можно оформить до {settings.order_deadline_time}.\n"
        if not settings.test_mode
        else "• Режим TEST_MODE: заказ в любое время суток, дедлайн не действует.\n"
    )
    await message.answer(
        "Бот заказа питания.\n"
        + line_menu
        + line_deadline
        + "• Не более 4 разных блюд в одном заказе.\n"
        "• Перед подтверждением откройте «Корзину» и проверьте список.\n"
        "• Команда /start — привязка ФИО.",
    )


def _load_cart_dict(conn: sqlite3.Connection, order_id: int) -> dict[int, int]:
    rows = db.list_order_items_with_menu(conn, order_id)
    return {mi.id: q for mi, q in rows}


def _save_cart(conn: sqlite3.Connection, order_id: int, cart: dict[int, int]) -> None:
    lines = [(i, q) for i, q in cart.items() if q > 0]
    db.set_order_items(conn, order_id, lines)


@router.callback_query(F.data.startswith("m:"))
async def cb_menu_page(cb: CallbackQuery, conn: sqlite3.Connection, settings: Settings) -> None:
    page = int(cb.data.split(":", 1)[1])
    uid = cb.from_user.id if cb.from_user else 0
    eid = _emp_id(conn, uid)
    if not eid:
        await cb.answer("Нет привязки.", show_alert=True)
        return
    today = local_today(settings.tz)
    mid = db.get_menu_for_date(conn, today)
    if not mid:
        await cb.answer("Нет меню.", show_alert=True)
        return
    items = db.list_menu_items(conn, mid)
    od = db.get_order_for_employee_date(conn, eid, today)
    if od and od[1] == "confirmed":
        await cb.answer("Уже подтверждено.", show_alert=True)
        return
    if _deadline_blocks(settings):
        await cb.answer("Дедлайн прошёл.", show_alert=True)
        return
    text = _menu_lines(items)
    await cb.message.edit_text(text, reply_markup=_menu_kb(page, items))
    await cb.answer()


@router.callback_query(F.data.startswith("+:"))
async def cb_add(cb: CallbackQuery, conn: sqlite3.Connection, settings: Settings) -> None:
    item_id = int(cb.data.split(":", 1)[1])
    uid = cb.from_user.id if cb.from_user else 0
    eid = _emp_id(conn, uid)
    if not eid:
        await cb.answer("Нет привязки.", show_alert=True)
        return
    today = local_today(settings.tz)
    if _deadline_blocks(settings):
        await cb.answer("Дедлайн прошёл.", show_alert=True)
        return
    od = db.get_order_for_employee_date(conn, eid, today)
    if od and od[1] == "confirmed":
        await cb.answer("Уже подтверждено.", show_alert=True)
        return
    oid = db.get_or_create_draft_order(conn, eid, today)
    cart = _load_cart_dict(conn, oid)
    if item_id not in cart and len(cart) >= MAX_DISH_TYPES:
        await cb.answer("Не более 4 разных блюд.", show_alert=True)
        return
    cart[item_id] = cart.get(item_id, 0) + 1
    _save_cart(conn, oid, cart)
    await cb.answer("Добавлено")


@router.callback_query(F.data.startswith("-:"))
async def cb_sub(cb: CallbackQuery, conn: sqlite3.Connection, settings: Settings) -> None:
    item_id = int(cb.data.split(":", 1)[1])
    uid = cb.from_user.id if cb.from_user else 0
    eid = _emp_id(conn, uid)
    if not eid:
        await cb.answer("Нет привязки.", show_alert=True)
        return
    today = local_today(settings.tz)
    if _deadline_blocks(settings):
        await cb.answer("Дедлайн прошёл.", show_alert=True)
        return
    od = db.get_order_for_employee_date(conn, eid, today)
    if not od:
        await cb.answer()
        return
    oid, st = od
    if st == "confirmed":
        await cb.answer("Уже подтверждено.", show_alert=True)
        return
    cart = _load_cart_dict(conn, oid)
    if item_id not in cart:
        await cb.answer()
        return
    cart[item_id] -= 1
    if cart[item_id] <= 0:
        del cart[item_id]
    _save_cart(conn, oid, cart)
    await cb.answer("Убрано")


@router.callback_query(F.data == "cart")
async def cb_cart(cb: CallbackQuery, conn: sqlite3.Connection, settings: Settings) -> None:
    uid = cb.from_user.id if cb.from_user else 0
    eid = _emp_id(conn, uid)
    if not eid:
        await cb.answer("Нет привязки.", show_alert=True)
        return
    today = local_today(settings.tz)
    od = db.get_order_for_employee_date(conn, eid, today)
    oid = db.get_or_create_draft_order(conn, eid, today) if not od else od[0]
    st = od[1] if od else "draft"
    if st == "confirmed":
        await cb.answer("Уже подтверждено.", show_alert=True)
        return
    if _deadline_blocks(settings):
        await cb.answer("Дедлайн прошёл.", show_alert=True)
        return
    text = _cart_text(conn, oid)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Подтвердить заказ", callback_data="cf"),
                InlineKeyboardButton(text="К меню", callback_data="m:0"),
            ]
        ]
    )
    await cb.message.edit_text(text, reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data == "cf")
async def cb_confirm(cb: CallbackQuery, conn: sqlite3.Connection, settings: Settings) -> None:
    uid = cb.from_user.id if cb.from_user else 0
    eid = _emp_id(conn, uid)
    if not eid:
        await cb.answer("Нет привязки.", show_alert=True)
        return
    today = local_today(settings.tz)
    if _deadline_blocks(settings):
        await cb.answer("Дедлайн прошёл.", show_alert=True)
        return
    od = db.get_order_for_employee_date(conn, eid, today)
    if not od:
        await cb.answer("Корзина пуста.", show_alert=True)
        return
    oid, st = od
    if st == "confirmed":
        await cb.answer("Уже подтверждено.", show_alert=True)
        return
    rows = db.list_order_items_with_menu(conn, oid)
    if not rows:
        await cb.answer("Корзина пуста.", show_alert=True)
        return
    if len(rows) > MAX_DISH_TYPES:
        await cb.answer("Слишком много разных блюд.", show_alert=True)
        return
    db.confirm_order(conn, oid)
    await cb.message.edit_text(
        "Заказ подтверждён. Спасибо!\n\n" + _cart_text(conn, oid),
        reply_markup=None,
    )
    await cb.answer("Готово")
