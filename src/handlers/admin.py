from __future__ import annotations

import io
import sqlite3
from aiogram import F, Router
from aiogram.filters import BaseFilter, Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .. import db
from ..config import Settings
from ..menu_parse import parse_docx_bytes
from ..jobs import send_monthly_report_previous
from ..reports import (
    aggregate_daily_canteen,
    build_canteen_csv_bytes,
    build_canteen_excel_bytes,
    format_canteen_text,
)
from ..timeutil import local_today
from .common import admin_main_kb, is_admin
from .registration import RegStates

router = Router(name="admin")


class IsAdmin(BaseFilter):
    async def __call__(self, message: Message, settings: Settings) -> bool:
        return bool(message.from_user and message.from_user.id in settings.admin_ids)


class IsAdminCb(BaseFilter):
    async def __call__(self, cb: CallbackQuery, settings: Settings) -> bool:
        return bool(cb.from_user and cb.from_user.id in settings.admin_ids)


admin_msg = router.message.filter(IsAdmin())
admin_cb = router.callback_query.filter(IsAdminCb())


@admin_msg(Command("admin"))
async def cmd_admin_panel(message: Message, conn: sqlite3.Connection) -> None:
    uid = message.from_user.id if message.from_user else 0
    emp = db.get_employee_by_tg(conn, uid)
    if emp:
        bind_hint = (
            f"Вы привязаны как {emp.last_name} {emp.first_name}.\n"
            "Заказ оформляется обычными кнопками: «Заказ на сегодня» / «Корзина»."
        )
    else:
        bind_hint = (
            "Для заказа еды сначала добавьте/привяжите себя как сотрудника:\n"
            "1) /add_employee Фамилия Имя\n"
            "2) «Привязка для заказа»."
        )
    await message.answer(
        "Админ-панель открыта.\n\n"
        "Разделы: сотрудники, загрузка меню, сводка столовой, месячный отчёт.\n"
        f"{bind_hint}",
        reply_markup=admin_main_kb(),
    )


@admin_msg(F.text == "Привязка для заказа")
async def admin_bind_for_order(
    message: Message,
    state: FSMContext,
    conn: sqlite3.Connection,
) -> None:
    uid = message.from_user.id if message.from_user else 0
    if db.get_employee_by_tg(conn, uid):
        await message.answer(
            "Telegram уже привязан к сотруднику — заказывайте через «Заказ на сегодня». "
            "Сброс привязки: /unlink_employee Фамилия Имя."
        )
        return
    await state.set_state(RegStates.waiting_name)
    await message.answer(
        "Введите фамилию и имя через пробел (как в списке).\n"
        "Если вас ещё нет в списке — сначала /add_employee Фамилия Имя."
    )


@admin_msg(F.text == "Сотрудники")
async def admin_employees_help(message: Message) -> None:
    await message.answer(
        "Управление сотрудниками (только для админа):\n"
        "• /add_employee Фамилия Имя\n"
        "  (как при первом входе сотрудника в бота)\n"
        "• /list_employees\n"
        "• /unlink_employee Фамилия Имя\n"
        "• /deactivate_employee Фамилия Имя\n",
        reply_markup=admin_main_kb(),
    )


def _parse_fio(text: str) -> tuple[str, str] | None:
    parts = text.split()
    if len(parts) < 2:
        return None
    return parts[0], parts[1]


@admin_msg(Command("add_employee"))
async def cmd_add_employee(message: Message, conn: sqlite3.Connection) -> None:
    raw = message.text or ""
    payload = raw.split(maxsplit=1)[1] if len(raw.split(maxsplit=1)) > 1 else ""
    parsed = _parse_fio(payload.strip())
    if not parsed:
        await message.answer("Формат: /add_employee Фамилия Имя")
        return
    last_name, first_name = parsed
    try:
        eid = db.add_employee(conn, last_name, first_name)
    except sqlite3.IntegrityError:
        await message.answer("Такой сотрудник уже есть (ФИО должно быть уникальным).")
        return
    await message.answer(f"Сотрудник добавлен, id={eid}.")


@admin_msg(Command("list_employees"))
async def cmd_list_employees(message: Message, conn: sqlite3.Connection) -> None:
    rows = db.list_employees(conn, active_only=False)
    if not rows:
        await message.answer("Список пуст.")
        return
    lines = []
    for r in rows:
        tg = r.telegram_user_id or "—"
        st = "активен" if r.active else "отключён"
        pos = (r.position or "").strip()
        name = f"{r.last_name} {r.first_name}"
        if pos:
            name = f"{pos} — {name}"
        lines.append(f"{r.id}. {name} | tg:{tg} | {st}")
    text = "\n".join(lines)
    part = 3500
    for i in range(0, len(text), part):
        await message.answer(text[i : i + part])


@admin_msg(Command("unlink_employee"))
async def cmd_unlink(message: Message, conn: sqlite3.Connection) -> None:
    raw = message.text or ""
    payload = raw.split(maxsplit=1)[1] if len(raw.split(maxsplit=1)) > 1 else ""
    parts = payload.split()
    if len(parts) < 2:
        await message.answer("Формат: /unlink_employee Фамилия Имя")
        return
    emp = db.find_employee_by_name_admin(conn, parts[0], parts[1])
    if not emp:
        await message.answer("Не найден.")
        return
    db.unlink_employee_telegram(conn, emp.id)
    await message.answer("Привязка Telegram сброшена.")


@admin_msg(Command("deactivate_employee"))
async def cmd_deactivate(message: Message, conn: sqlite3.Connection) -> None:
    raw = message.text or ""
    payload = raw.split(maxsplit=1)[1] if len(raw.split(maxsplit=1)) > 1 else ""
    parts = payload.split()
    if len(parts) < 2:
        await message.answer("Формат: /deactivate_employee Фамилия Имя")
        return
    emp = db.find_employee_by_name_admin(conn, parts[0], parts[1])
    if not emp:
        await message.answer("Не найден.")
        return
    db.deactivate_employee(conn, emp.id)
    await message.answer("Сотрудник отключён.")


@admin_msg(F.text == "Загрузить меню")
async def admin_upload_hint(message: Message) -> None:
    await message.answer("Пришлите файл .docx с меню на сегодня сообщением-документом.")


@admin_msg(F.document)
async def admin_upload_docx(message: Message, conn: sqlite3.Connection, settings: Settings) -> None:
    if not is_admin(message.from_user.id, settings):
        return
    doc = message.document
    if not doc:
        return
    fn = doc.file_name or ""
    if not fn.lower().endswith(".docx"):
        await message.answer("Нужен файл .docx")
        return
    bot = message.bot
    f = await bot.get_file(doc.file_id)
    buf = io.BytesIO()
    await bot.download_file(f.file_path, buf)
    data = buf.getvalue()
    try:
        items = parse_docx_bytes(data)
    except Exception as e:  # noqa: BLE001
        await message.answer(f"Не удалось разобрать файл: {e}")
        return
    if not items:
        await message.answer("Не найдено ни одной строки меню (название + цена).")
        return
    today = local_today(settings.tz)
    db.create_menu(conn, today, "manual", items)
    await message.answer(f"Меню на {today.isoformat()} загружено, позиций: {len(items)}.")


@admin_msg(F.text == "Сводка столовой")
async def admin_canteen_choose(message: Message) -> None:
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Excel (.xlsx)", callback_data="can:xlsx"),
                InlineKeyboardButton(text="CSV", callback_data="can:csv"),
            ],
            [InlineKeyboardButton(text="Текстом", callback_data="can:txt")],
        ]
    )
    await message.answer("Как отправить сводку в чат столовой?", reply_markup=kb)


@admin_cb(F.data.startswith("can:"))
async def admin_canteen_send(cb: CallbackQuery, conn: sqlite3.Connection, settings: Settings) -> None:
    fmt = cb.data.split(":", 1)[1]
    today = local_today(settings.tz)
    pairs, block2 = aggregate_daily_canteen(conn, today)
    chat_id = settings.canteen_chat_id
    if not chat_id:
        await cb.answer("CANTEEN_CHAT_ID не задан.", show_alert=True)
        return

    caption = f"Сводка на {today.isoformat()}"
    bot = cb.bot
    if fmt == "txt":
        text = format_canteen_text(pairs, block2)
        chunk = 3800
        for i in range(0, len(text), chunk):
            await bot.send_message(chat_id, text[i : i + chunk])
        await cb.message.answer("Сводка отправлена текстом.")
        await cb.answer()
        return
    if fmt == "xlsx":
        data = build_canteen_excel_bytes(pairs, block2)
        fname = f"canteen_{today.isoformat()}.xlsx"
        await bot.send_document(
            chat_id,
            document=BufferedInputFile(data, filename=fname),
            caption=caption,
        )
    elif fmt == "csv":
        data = build_canteen_csv_bytes(pairs, block2)
        fname = f"canteen_{today.isoformat()}.csv"
        await bot.send_document(
            chat_id,
            document=BufferedInputFile(data, filename=fname),
            caption=caption,
        )
    else:
        await cb.answer("Неизвестный формат", show_alert=True)
        return
    await cb.message.answer("Сводка отправлена файлом.")
    await cb.answer()


@admin_msg(F.text == "Месячный отчёт")
async def admin_monthly_manual(message: Message, conn: sqlite3.Connection, settings: Settings) -> None:
    if not settings.admin_ids:
        await message.answer("ADMIN_IDS не задан в .env.")
        return
    await send_monthly_report_previous(message.bot, conn, settings, mark_sent=False)
    await message.answer("Отчёт отправлен администраторам (внеочередной, без блокировки авто-отчёта).")