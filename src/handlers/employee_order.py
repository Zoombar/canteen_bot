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
from ..menu_parse import sanitize_dish_name
from ..reports import CONTAINER_PRICE_RUB, OrderLine, count_containers_for_order
from ..timeutil import is_deadline_passed, is_weekday_effective, local_today
from .common import OrderUiNotBlocked, employee_main_kb

router = Router(name="employee_order")

MAX_DISH_TYPES = 6
MAX_CAPTION = 1000
MENU_PAGE_SIZE = 10
# Telegram: текст на кнопке не длиннее 64 символов
TG_BTN_MAX = 64


def _deadline_blocks(settings: Settings) -> bool:
    if settings.test_mode:
        return False
    return is_deadline_passed(settings.tz, settings.order_deadline_time)


def _emp_id(conn: sqlite3.Connection, uid: int) -> int | None:
    e = db.get_employee_by_tg(conn, uid)
    return e.id if e else None


def _dish_title_button(it: db.MenuItemRow, ordinal: int) -> str:
    """Одна строка: номер, название и цена (до 64 символов)."""
    suffix = f" — {it.price:.2f} ₽"
    prefix = f"{ordinal}. "
    name = sanitize_dish_name(it.dish_name).strip()
    if len(prefix) + len(name) + len(suffix) <= TG_BTN_MAX:
        return prefix + name + suffix
    room = TG_BTN_MAX - len(prefix) - len(suffix) - 1
    if room < 4:
        return (prefix + name + suffix)[:TG_BTN_MAX]
    return prefix + name[:room] + "…" + suffix


def _qty_stepper_middle(qty: int) -> str:
    """Середина ряда: явно «в заказе» или нет."""
    if qty <= 0:
        return "0 шт · не в заказе"
    return f"{qty} шт · в заказе"


def _menu_caption(d: date) -> str:
    return (
        f"Меню на {d.isoformat()}.\n"
        "У каждой позиции две строки: сверху — блюдо и цена, снизу — убрать порцию, "
        "сколько шт, добавить порцию. Файл во вложении — полный список."
    )


def _cart_text(conn: sqlite3.Connection, order_id: int) -> str:
    rows = db.list_order_items_with_menu(conn, order_id)
    if not rows:
        return "Корзина пуста."
    lines = ["Ваш заказ:"]
    total = 0.0
    order_lines: list[OrderLine] = []
    for mi, q in rows:
        line_sum = mi.price * q
        total += line_sum
        title = sanitize_dish_name(mi.dish_name)
        lines.append(f"• {title} × {q} = {line_sum:.2f} руб.")
        order_lines.append(
            OrderLine(
                menu_item_id=mi.id,
                dish_name=mi.dish_name,
                dish_kind=mi.dish_kind,
                quantity=q,
            )
        )
    containers = count_containers_for_order(order_lines)
    containers_sum = containers * CONTAINER_PRICE_RUB
    if containers > 0:
        lines.append(f"• Контейнеры × {containers} = {containers_sum:.2f} руб.")
    total_with_containers = total + containers_sum
    lines.append(f"\nИтого: {total_with_containers:.2f} руб.")
    return "\n".join(lines)


def _truncate_caption(text: str) -> str:
    if len(text) <= MAX_CAPTION:
        return text
    return text[: MAX_CAPTION - 1] + "…"


def _menu_kb(
    items: list[db.MenuItemRow],
    cart: dict[int, int],
    page: int = 0,
) -> InlineKeyboardMarkup:
    total = len(items)
    pages = max(1, (total + MENU_PAGE_SIZE - 1) // MENU_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    start = page * MENU_PAGE_SIZE
    end = min(total, start + MENU_PAGE_SIZE)

    rows: list[list[InlineKeyboardButton]] = []
    for idx in range(start, end):
        it = items[idx]
        qty = cart.get(it.id, 0)
        ordinal = idx + 1
        rows.append(
            [
                InlineKeyboardButton(
                    text=_dish_title_button(it, ordinal),
                    callback_data=f"n:{it.id}:{page}",
                ),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(text="−", callback_data=f"sub:{it.id}:{page}"),
                InlineKeyboardButton(
                    text=_qty_stepper_middle(qty),
                    callback_data=f"q:{it.id}:{page}",
                ),
                InlineKeyboardButton(text="+", callback_data=f"+:{it.id}:{page}"),
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"m:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data=f"m:{page}"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"m:{page + 1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton(text="Корзина", callback_data=f"cart:{page}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _cart_kb(page: int = 0) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Подтвердить заказ", callback_data="cf"),
                InlineKeyboardButton(text="К меню", callback_data=f"m:{page}"),
            ]
        ]
    )


async def _open_menu(
    message: Message,
    conn: sqlite3.Connection,
    settings: Settings,
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

    if _deadline_blocks(settings):
        od = db.get_order_for_employee_date(conn, eid, today)
        if od and od[1] == "confirmed":
            oid = od[0]
            await message.answer(
                "Дедлайн прошёл. Заказ уже зафиксирован.\n\n" + _cart_text(conn, oid),
                reply_markup=employee_main_kb(),
            )
            return
        await message.answer(
            "Время приёма заказов на сегодня истекло.",
            reply_markup=employee_main_kb(),
        )
        return

    oid = db.get_or_create_draft_order(conn, eid, today)
    cart = _load_cart_dict(conn, oid)
    data = build_menu_txt_bytes(items)
    fname = f"menu_{today.isoformat()}.txt"
    caption = _menu_caption(today)
    await message.answer_document(
        document=BufferedInputFile(data, filename=fname),
        caption=caption,
        reply_markup=_menu_kb(items, cart, page=0),
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
    page: int | None = None,
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
    if page is None:
        page = _extract_page(cb.data)
    caption = _menu_caption(today)
    try:
        if cb.message and cb.message.document:
            await cb.message.edit_caption(caption=caption, reply_markup=_menu_kb(items, cart, page=page))
        elif cb.message:
            await cb.message.edit_reply_markup(reply_markup=_menu_kb(items, cart, page=page))
    except Exception:
        pass


def _extract_page(data: str | None) -> int:
    if not data:
        return 0
    parts = data.split(":")
    if len(parts) >= 3 and parts[-1].isdigit():
        return max(0, int(parts[-1]))
    if len(parts) >= 2 and parts[0] in {"m", "cart"} and parts[1].isdigit():
        return max(0, int(parts[1]))
    return 0


@router.message(OrderUiNotBlocked(), F.text == "Заказ на сегодня")
async def text_order_today(message: Message, conn: sqlite3.Connection, settings: Settings) -> None:
    await _open_menu(message, conn, settings)


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
    if _deadline_blocks(settings):
        if st == "confirmed":
            await message.answer("Заказ подтверждён и дедлайн прошёл.\n\n" + _cart_text(conn, oid))
            return
        await message.answer("Время приёма заказов на сегодня истекло.")
        return

    text = _truncate_caption(_cart_text(conn, oid))
    kb = _cart_kb()
    if st == "confirmed":
        text = _truncate_caption(
            "Заказ уже подтверждён, но до дедлайна можно изменить позиции.\n\n" + _cart_text(conn, oid)
        )
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
        + "• Не более 6 разных блюд в одном заказе.\n"
        "• Перед подтверждением откройте «Корзину» и проверьте список.\n"
        "• Команда /start — привязка ФИО.",
    )


@router.callback_query(OrderUiNotBlocked(), F.data.startswith("m:"))
async def cb_menu_page(cb: CallbackQuery, conn: sqlite3.Connection, settings: Settings) -> None:
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
    if _deadline_blocks(settings):
        await cb.answer("Дедлайн прошёл.", show_alert=True)
        return
    page = _extract_page(cb.data)
    oid = db.get_or_create_draft_order(conn, eid, today)
    cart = _load_cart_dict(conn, oid)
    caption = _menu_caption(today)
    try:
        if cb.message and cb.message.document:
            await cb.message.edit_caption(caption=caption, reply_markup=_menu_kb(items, cart, page=page))
        elif cb.message:
            await cb.message.edit_text(
                text=caption,
                reply_markup=_menu_kb(items, cart, page=page),
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
    title = sanitize_dish_name(row.dish_name)
    await cb.answer(f"{title} — {row.price:.2f} руб.", show_alert=True)


@router.callback_query(OrderUiNotBlocked(), F.data.startswith("q:"))
async def cb_qty_info(cb: CallbackQuery, conn: sqlite3.Connection, settings: Settings) -> None:
    parts = cb.data.split(":")
    if len(parts) < 2:
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
    name = sanitize_dish_name(row.dish_name) if row else "Блюдо"
    await cb.answer(f"{name}: {qty} шт.", show_alert=True)


@router.callback_query(OrderUiNotBlocked(), F.data.startswith("+:"))
async def cb_add(cb: CallbackQuery, conn: sqlite3.Connection, settings: Settings) -> None:
    parts = cb.data.split(":")
    item_id = int(parts[1])
    page = _extract_page(cb.data)
    uid = cb.from_user.id if cb.from_user else 0
    eid = _emp_id(conn, uid)
    if not eid:
        await cb.answer("Нет привязки.", show_alert=True)
        return
    today = local_today(settings.tz)
    if _deadline_blocks(settings):
        await cb.answer("Дедлайн прошёл.", show_alert=True)
        return
    oid = db.get_or_create_draft_order(conn, eid, today)
    cart = _load_cart_dict(conn, oid)
    if item_id not in cart and len(cart) >= MAX_DISH_TYPES:
        await cb.answer("Не более 6 разных блюд.", show_alert=True)
        return
    cart[item_id] = cart.get(item_id, 0) + 1
    _save_cart(conn, oid, cart)
    await _refresh_menu_message(cb, conn, settings, page=page)
    await cb.answer("Добавлено")


@router.callback_query(OrderUiNotBlocked(), F.data.startswith("sub:"))
async def cb_sub(cb: CallbackQuery, conn: sqlite3.Connection, settings: Settings) -> None:
    parts = cb.data.split(":")
    item_id = int(parts[1])
    page = _extract_page(cb.data)
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
    oid, _st = od
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


@router.callback_query(OrderUiNotBlocked(), F.data.startswith("cart"))
async def cb_cart(cb: CallbackQuery, conn: sqlite3.Connection, settings: Settings) -> None:
    uid = cb.from_user.id if cb.from_user else 0
    eid = _emp_id(conn, uid)
    if not eid:
        await cb.answer("Нет привязки.", show_alert=True)
        return
    today = local_today(settings.tz)
    od = db.get_order_for_employee_date(conn, eid, today)
    oid = db.get_or_create_draft_order(conn, eid, today) if not od else od[0]
    if _deadline_blocks(settings):
        await cb.answer("Дедлайн прошёл.", show_alert=True)
        return
    page = _extract_page(cb.data)
    text = _truncate_caption(_cart_text(conn, oid))
    kb = _cart_kb(page=page)
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
    rows = db.list_order_items_with_menu(conn, oid)
    if not rows:
        await cb.answer("Корзина пуста.", show_alert=True)
        return
    if len(rows) > MAX_DISH_TYPES:
        await cb.answer("Слишком много разных блюд.", show_alert=True)
        return
    if st != "confirmed":
        db.confirm_order(conn, oid)
        done = _truncate_caption("Заказ подтверждён. Спасибо!\n\n" + _cart_text(conn, oid))
    else:
        done = _truncate_caption("Заказ уже подтверждён. Изменения сохранены.\n\n" + _cart_text(conn, oid))
    try:
        if cb.message and cb.message.document:
            await cb.message.edit_caption(caption=done, reply_markup=None)
        elif cb.message:
            await cb.message.edit_text(done, reply_markup=None)
    except Exception:
        pass
    await cb.answer("Готово")
