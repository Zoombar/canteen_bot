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
from ..jobs import (
    send_monthly_report_previous,
    test_broadcast_menu_now,
    test_broadcast_orders_closed,
)
from ..reports import (
    aggregate_daily_canteen,
    build_canteen_csv_bytes,
    build_canteen_excel_bytes,
    format_canteen_text,
)
from ..timeutil import (
    local_today,
    set_test_deadline_override,
    set_test_weekday_override,
)
from .common import admin_main_kb, is_admin
from .registration import RegStates

router = Router(name="admin")


class IsAdmin(BaseFilter):
    async def __call__(self, message: Message, settings: Settings) -> bool:
        return bool(message.from_user and message.from_user.id in settings.admin_ids)


class IsAdminCb(BaseFilter):
    async def __call__(self, cb: CallbackQuery, settings: Settings) -> bool:
        return bool(cb.from_user and cb.from_user.id in settings.admin_ids)


class IsTestMode(BaseFilter):
    async def __call__(self, message: Message, settings: Settings) -> bool:
        return settings.test_mode


@router.message(IsAdmin(), IsTestMode(), Command("test_menu"))
async def cmd_test_menu(message: Message, conn: sqlite3.Connection, settings: Settings) -> None:
    """Тест: разослать меню на сегодня всем (как утренняя рассылка), без ожидания cron."""
    report = await test_broadcast_menu_now(message.bot, conn, settings, only_user_id=None)
    await message.answer(report)


@router.message(IsAdmin(), IsTestMode(), Command("test_menu_me"))
async def cmd_test_menu_me(message: Message, conn: sqlite3.Connection, settings: Settings) -> None:
    """Тест: прислать меню только вам."""
    uid = message.from_user.id if message.from_user else 0
    report = await test_broadcast_menu_now(message.bot, conn, settings, only_user_id=uid)
    await message.answer(report)


@router.message(IsAdmin(), IsTestMode(), Command("test_closed"))
async def cmd_test_closed(message: Message, conn: sqlite3.Connection, settings: Settings) -> None:
    """Тест: считать дедлайн прошедшим + разослать сообщение «заказы закрыты»."""
    set_test_deadline_override(True)
    report = await test_broadcast_orders_closed(message.bot, conn, settings)
    await message.answer(
        "Режим теста: дедлайн для заказов считается пройденным.\n" + report
    )


@router.message(IsAdmin(), IsTestMode(), Command("test_open"))
async def cmd_test_open(message: Message) -> None:
    """Тест: принудительно разрешить оформлять заказ (игнорировать дедлайн по времени)."""
    set_test_deadline_override(False)
    await message.answer(
        "Режим теста: дедлайн считается НЕ наступившим — можно оформлять заказ (до /test_reset)."
    )


@router.message(IsAdmin(), IsTestMode(), Command("test_weekday_on"))
async def cmd_test_weekday_on(message: Message) -> None:
    """Тест в выходной: вести себя как в будний день для приёма заказов."""
    set_test_weekday_override(True)
    await message.answer("Режим теста: «сегодня будний день» для заказов (до /test_reset).")


@router.message(IsAdmin(), IsTestMode(), Command("test_weekday_off"))
async def cmd_test_weekday_off(message: Message) -> None:
    """Тест: считать сегодня выходным (заказы недоступны)."""
    set_test_weekday_override(False)
    await message.answer("Режим теста: «сегодня выходной» для заказов (до /test_reset).")


@router.message(IsAdmin(), IsTestMode(), Command("test_reset"))
async def cmd_test_reset(message: Message) -> None:
    """Сбросить тестовые режимы дедлайна и буднего дня — снова реальное время."""
    set_test_deadline_override(None)
    set_test_weekday_override(None)
    await message.answer("Тестовые переопределения сброшены: дедлайн и день недели снова по часам и календарю.")


class IsNotTestMode(BaseFilter):
    async def __call__(self, message: Message, settings: Settings) -> bool:
        return not settings.test_mode


_TEST_COMMANDS = (
    "test_menu",
    "test_menu_me",
    "test_closed",
    "test_open",
    "test_weekday_on",
    "test_weekday_off",
    "test_reset",
)


@router.message(IsAdmin(), IsNotTestMode(), Command(commands=list(_TEST_COMMANDS)))
async def test_commands_disabled_hint(message: Message) -> None:
    await message.answer(
        "Тестовые команды выключены. В .env установите TEST_MODE=true и перезапустите бота."
    )


@router.message(IsAdmin(), Command("admin"))
async def cmd_admin_panel(message: Message, conn: sqlite3.Connection, settings: Settings) -> None:
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
    test_block = ""
    if settings.test_mode:
        test_block = (
            "\nРежим TEST_MODE: заказы в выходные и в любое время; команды "
            "/test_menu, /test_menu_me, /test_closed, /test_open, "
            "/test_weekday_on, /test_weekday_off, /test_reset.\n"
        )
    await message.answer(
        "Админ-панель открыта.\n\n"
        "Разделы: сотрудники, загрузка меню, сводка столовой, месячный отчёт."
        f"{test_block}\n"
        f"{bind_hint}",
        reply_markup=admin_main_kb(),
    )


@router.message(IsAdmin(), F.text == "Привязка для заказа")
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


@router.message(IsAdmin(), F.text == "Сотрудники")
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


@router.message(IsAdmin(), Command("add_employee"))
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


@router.message(IsAdmin(), Command("list_employees"))
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


@router.message(IsAdmin(), Command("unlink_employee"))
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


@router.message(IsAdmin(), Command("deactivate_employee"))
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


@router.message(IsAdmin(), F.text == "Загрузить меню")
async def admin_upload_hint(message: Message) -> None:
    await message.answer("Пришлите файл .docx с меню на сегодня сообщением-документом.")


@router.message(IsAdmin(), F.document)
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


@router.message(IsAdmin(), F.text == "Сводка столовой")
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


@router.callback_query(IsAdminCb(), F.data.startswith("can:"))
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


@router.message(IsAdmin(), F.text == "Месячный отчёт")
async def admin_monthly_manual(message: Message, conn: sqlite3.Connection, settings: Settings) -> None:
    if not settings.admin_ids:
        await message.answer("ADMIN_IDS не задан в .env.")
        return
    await send_monthly_report_previous(message.bot, conn, settings, mark_sent=False)
    await message.answer("Отчёт отправлен администраторам (внеочередной, без блокировки авто-отчёта).")