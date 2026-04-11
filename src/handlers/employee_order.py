from __future__ import annotations

import sqlite3
from datetime import date

from aiogram import F, Router
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .. import db
from ..config import Settings
from ..menu_export import build_menu_txt_bytes
from ..timeutil import is_deadline_passed, is_weekday_effective, local_today
from .common import OrderUiNotBlocked, employee_main_kb

router = Router(name="employee_order")

PAGE_SIZE = 6
MAX_DISH_TYPES = 4
MAX_CAPTION = 1000


def _deadline_blocks(settings: Settings) -> bool:
    if settings.test_mode:
        return False
    return is_deadline_passed(settings.tz, settings.order_deadline_time)


def _emp_id(conn: sqlite3.Connection, uid: int) -> int | None:
    e = db.get_employee_by_tg(conn, uid)
    return e.id if e else None


def _short_btn_label(name: str, max_len: int = 12) -> str:
    n = name.strip()
    if len(n) <= max_len:
        return n
    return n[: max_len - 1] + "…"


def _menu_caption(d: date, page: int, total_pages: int) -> str:
    return (
        f"Меню на {d.isoformat()}. Страница {page + 1}/{total_pages}. "
        "Файл — полный список; ниже выберите количество (+/−)."
    )


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


def _truncate_caption(text: str) -> str:
    if len(text) <= MAX_CAPTION:
        return text
    return text[: MAX_CAPTION - 1] + "…"


def _menu_kb(
    page: int,
    items: list[db.MenuItemRow],
    cart: dict[int, int],
) -> InlineKeyboardMarkup:
    start = page * PAGE_SIZE
    chunk = items[start : start + PAGE_SIZE]
    rows: list[list[InlineKeyboardButton]] = []
    for it in chunk:
        qty = cart.get(it.id, 0)
        rows.append(
            [
                InlineKeyboardButton(
                    text=_short_btn_label(it.dish_name),
                    callback_data=f"n:{it.id}:{page}",
                ),
                InlineKeyboardButton(text="+", callback_data=f"+:{it.id}:{page}"),
                InlineKeyboardButton(text="−", callback_data=f"sub:{it.id}:{page}"),
                InlineKeyboardButton(text=str(qty), callback_data=f"q:{it.id}:{page}"),
            ]
        )
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="«", callback_data=f"m:{page - 1}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="»", callback_data=f"m:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton(text="Корзина", callback_data=f"cart:{page}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _cart_kb(page_back: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Подтвердить заказ", callback_data="cf"),
                InlineKeyboardButton(text="К меню", callback_data=f"m:{page_back}"),
            ]
        ]
    )


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

    oid = db.get_or_create_draft_order(conn, eid, today)
    cart = _load_cart_dict(conn, oid)
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    data = build_menu_txt_bytes(items)
    fname = f"menu_{today.isoformat()}.txt"
    caption = _menu_caption(today, page, pages)
    await message.answer_document(
        document=BufferedInputFile(data, filename=fname),
        caption=caption,
        reply_markup=_menu_kb(page, items, cart),
    )


def _load_cart_dict(conn: sqlite3.Connection, order_id: int) -> dict[int, int]:
    rows = db.list_order_items_with_menu(conn, order_id)
    return {mi.id: q for mi, q in rows}


def _save_cart(conn: sqlite3.Connection, order_id: int, cart: dict[int, int]) -> None:
    lines = [(i, q) for i, q in cart.items() if q > 0]
    db.set_order_items(conn, order_id, lines)


async def _refresh_menu_message(
    cb: CallbackQuery,
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    page: int,
) -> None:
    uid = cb.from_user.id if cb.from_user else 0
    eid = _emp_id(conn, uid)
    if not eid:
        return
    today = local_today(settings.tz)
    mid = db.get_menu_for_date(conn, today)
    if not mid:
        return
    items = db.list_menu_items(conn, mid)
    oid = db.get_or_create_draft_order(conn, eid, today)
    cart = _load_cart_dict(conn, oid)
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    caption = _menu_caption(today, page, pages)
    try:
        if cb.message and cb.message.document:
            await cb.message.edit_caption(caption=caption, reply_markup=_menu_kb(page, items, cart))
        elif cb.message:
            await cb.message.edit_reply_markup(reply_markup=_menu_kb(page, items, cart))
    except Exception:
        pass


@router.message(OrderUiNotBlocked(), F.text == "Заказ на сегодня")
async def text_order_today(message: Message, conn: sqlite3.Connection, settings: Settings) -> None:
    await _open_menu(message, conn, settings, page=0)


@router.message(OrderUiNotBlocked(), F.text == "Корзина")
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

    text = _truncate_caption(_cart_text(conn, oid))
    kb = _cart_kb(0)
    await message.answer(text, reply_markup=kb)


@router.message(OrderUiNotBlocked(), F.text == "Помощь")
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


@router.callback_query(OrderUiNotBlocked(), F.data.startswith("m:"))
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
    oid = db.get_or_create_draft_order(conn, eid, today)
    cart = _load_cart_dict(conn, oid)
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    caption = _menu_caption(today, page, pages)
    try:
        if cb.message and cb.message.document:
            await cb.message.edit_caption(caption=caption, reply_markup=_menu_kb(page, items, cart))
        elif cb.message:
            await cb.message.edit_text(
                text=caption,
                reply_markup=_menu_kb(page, items, cart),
            )
    except Exception:
        pass
    await cb.answer()


@router.callback_query(OrderUiNotBlocked(), F.data.startswith("n:"))
async def cb_name_info(cb: CallbackQuery, conn: sqlite3.Connection) -> None:
    parts = cb.data.split(":")
    if len(parts) < 2:
        await cb.answer()
        return
    item_id = int(parts[1])
    row = db.get_menu_item(conn, item_id)
    if not row:
        await cb.answer("Позиция не найдена.", show_alert=True)
        return
    await cb.answer(f"{row.dish_name} — {row.price:.2f} руб.", show_alert=True)


@router.callback_query(OrderUiNotBlocked(), F.data.startswith("q:"))
async def cb_qty_info(cb: CallbackQuery, conn: sqlite3.Connection, settings: Settings) -> None:
    parts = cb.data.split(":")
    if len(parts) < 3:
        await cb.answer()
        return
    item_id = int(parts[1])
    uid = cb.from_user.id if cb.from_user else 0
    eid = _emp_id(conn, uid)
    if not eid:
        await cb.answer("Нет привязки.", show_alert=True)
        return
    today = local_today(settings.tz)
    oid = db.get_or_create_draft_order(conn, eid, today)
    cart = _load_cart_dict(conn, oid)
    qty = cart.get(item_id, 0)
    row = db.get_menu_item(conn, item_id)
    name = row.dish_name if row else "Блюдо"
    await cb.answer(f"{name}: {qty} шт.", show_alert=True)


@router.callback_query(OrderUiNotBlocked(), F.data.startswith("+:"))
async def cb_add(cb: CallbackQuery, conn: sqlite3.Connection, settings: Settings) -> None:
    parts = cb.data.split(":")
    item_id = int(parts[1])
    page = int(parts[2]) if len(parts) > 2 else 0
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
    await _refresh_menu_message(cb, conn, settings, page=page)
    await cb.answer("Добавлено")


@router.callback_query(OrderUiNotBlocked(), F.data.startswith("sub:"))
async def cb_sub(cb: CallbackQuery, conn: sqlite3.Connection, settings: Settings) -> None:
    parts = cb.data.split(":")
    item_id = int(parts[1])
    page = int(parts[2]) if len(parts) > 2 else 0
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
    await _refresh_menu_message(cb, conn, settings, page=page)
    await cb.answer("Убрано")


@router.callback_query(OrderUiNotBlocked(), F.data.startswith("cart:"))
async def cb_cart(cb: CallbackQuery, conn: sqlite3.Connection, settings: Settings) -> None:
    page_back = int(cb.data.split(":", 1)[1])
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
    text = _truncate_caption(_cart_text(conn, oid))
    kb = _cart_kb(page_back)
    try:
        if cb.message and cb.message.document:
            await cb.message.edit_caption(caption=text, reply_markup=kb)
        elif cb.message:
            await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass
    await cb.answer()


@router.callback_query(OrderUiNotBlocked(), F.data == "cf")
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
    done = _truncate_caption("Заказ подтверждён. Спасибо!\n\n" + _cart_text(conn, oid))
    try:
        if cb.message and cb.message.document:
            await cb.message.edit_caption(caption=done, reply_markup=None)
        elif cb.message:
            await cb.message.edit_text(done, reply_markup=None)
    except Exception:
        pass
    await cb.answer("Готово")
