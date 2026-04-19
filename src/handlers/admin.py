from __future__ import annotations

import asyncio
import io
import sqlite3

from aiogram import F, Router
from aiogram.filters import BaseFilter, Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .. import db
from ..config import Settings
from ..jobs import (
    send_monthly_report_previous,
    test_broadcast_menu_now,
    test_broadcast_orders_closed,
)
from ..imap_client import imap_diagnose_connection
from ..menu_parse import parse_docx_bytes
from ..timeutil import (
    local_today,
    set_test_deadline_override,
    set_test_weekday_override,
)
from .common import admin_main_kb, is_admin

router = Router(name="admin")

# Список сотрудников в админке: столько кнопок на странице (плюс ряд навигации).
EMP_LIST_PAGE_SIZE = 8
# Текст на inline-кнопке — лимит Telegram.
_EMP_BTN_MAX = 58


def _emp_list_button_label(r: db.EmployeeRow) -> str:
    st = "✓" if r.active else "✗"
    name = f"{r.last_name} {r.first_name}".strip()
    line = f"{st} {name}"
    if len(line) <= _EMP_BTN_MAX:
        return line
    room = _EMP_BTN_MAX - len(st) - 2
    if room < 6:
        return line[:_EMP_BTN_MAX]
    return f"{st} {name[:room]}…"


def _format_employee_card(r: db.EmployeeRow) -> str:
    tg = str(r.telegram_user_id) if r.telegram_user_id is not None else "—"
    tg_user = f"@{r.telegram_username}" if r.telegram_username else "—"
    st = "активен" if r.active else "отключён"
    pos = (r.position or "").strip()
    head = f"Сотрудник #{r.id} — {st}"
    lines = [head, f"ФИО: {r.last_name} {r.first_name}"]
    if pos:
        lines.append(f"Должность: {pos}")
    lines.append(f"Telegram ID: {tg}")
    lines.append(f"Username: {tg_user}")
    return "\n".join(lines)


def _employee_list_text_and_kb(
    conn: sqlite3.Connection, *, page: int
) -> tuple[str, InlineKeyboardMarkup]:
    total = db.count_employees(conn, active_only=False)
    if total == 0:
        return (
            "Список сотрудников пуст.",
            InlineKeyboardMarkup(inline_keyboard=[]),
        )
    pages = max(1, (total + EMP_LIST_PAGE_SIZE - 1) // EMP_LIST_PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    offset = page * EMP_LIST_PAGE_SIZE
    rows = db.list_employees_page(conn, limit=EMP_LIST_PAGE_SIZE, offset=offset, active_only=False)
    text = (
        f"Сотрудники (всего {total}), страница {page + 1} из {pages}.\n"
        "Нажмите на строку, чтобы открыть карточку и действия."
    )
    keyboard: list[list[InlineKeyboardButton]] = []
    for r in rows:
        keyboard.append(
            [InlineKeyboardButton(text=_emp_list_button_label(r), callback_data=f"emp:vw:{r.id}")]
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"emp:pg:{page - 1}"))
    nav.append(
        InlineKeyboardButton(
            text=f"{page + 1}/{pages}",
            callback_data=f"emp:hi:{page}:{pages}",
        )
    )
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"emp:pg:{page + 1}"))
    keyboard.append(nav)
    return text, InlineKeyboardMarkup(inline_keyboard=keyboard)


def _employee_view_kb(r: db.EmployeeRow, *, deleting: bool = False) -> InlineKeyboardMarkup:
    if deleting:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Да, удалить", callback_data=f"emp:ok:{r.id}"),
                    InlineKeyboardButton(text="Отмена", callback_data=f"emp:can:{r.id}"),
                ],
            ]
        )
    row_actions: list[InlineKeyboardButton] = []
    if r.telegram_user_id is not None:
        row_actions.append(InlineKeyboardButton(text="Снять привязку", callback_data=f"emp:ul:{r.id}"))
    if r.active:
        row_actions.append(InlineKeyboardButton(text="Отключить", callback_data=f"emp:off:{r.id}"))
    else:
        row_actions.append(InlineKeyboardButton(text="Включить", callback_data=f"emp:on:{r.id}"))
    rows: list[list[InlineKeyboardButton]] = []
    if row_actions:
        rows.append(row_actions)
    rows.append([InlineKeyboardButton(text="Удалить из базы", callback_data=f"emp:del:{r.id}")])
    rows.append([InlineKeyboardButton(text="◀️ К списку", callback_data="emp:pg:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


class IsAdmin(BaseFilter):
    async def __call__(self, message: Message, settings: Settings) -> bool:
        return bool(message.from_user and message.from_user.id in settings.admin_ids)


class IsAdminCb(BaseFilter):
    async def __call__(self, cb: CallbackQuery, settings: Settings) -> bool:
        return bool(cb.from_user and cb.from_user.id in settings.admin_ids)


class IsTestMode(BaseFilter):
    async def __call__(self, message: Message, settings: Settings) -> bool:
        return settings.test_mode


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


def _admin_panel_text(conn: sqlite3.Connection, uid: int, settings: Settings) -> str:
    emp = db.get_employee_by_tg(conn, uid)
    if emp:
        bind_hint = (
            f"Вы привязаны как {emp.last_name} {emp.first_name}.\n"
            "Заказ — кнопками «Заказ на сегодня» и «Корзина».\n"
            "Снять свою привязку: «Список сотрудников» → ваша карточка → «Снять привязку»."
        )
    else:
        bind_hint = (
            "Для заказа еды нажмите /start и введите фамилию и имя — запись создастся сама "
            "или привяжется к уже существующей.\n"
            "Снять привязку с аккаунта — «Список сотрудников», откройте свою карточку."
        )
    test_block = ""
    if settings.test_mode:
        test_block = (
            "\nРежим TEST_MODE: заказы в выходные и в любое время; дедлайн не действует.\n"
        )
    return (
        "Админ-панель.\n\n"
        "Управление сотрудниками — «Список сотрудников» (карточка записи). Меню и отчёты — кнопками ниже.\n"
        f"Сводка для столовой уходит в чат (CANTEEN_CHAT_ID в .env) автоматически в {settings.order_deadline_time} "
        "в будни, в момент дедлайна заказов."
        f"{test_block}\n"
        f"{bind_hint}"
    )


@router.message(IsAdmin(), F.text == "Админ-панель")
async def admin_panel(message: Message, conn: sqlite3.Connection, settings: Settings, state: FSMContext) -> None:
    await state.clear()
    uid = message.from_user.id if message.from_user else 0
    await message.answer(
        _admin_panel_text(conn, uid, settings),
        reply_markup=admin_main_kb(settings),
    )


@router.message(IsAdmin(), F.text == "Список сотрудников")
async def admin_list_employees(message: Message, conn: sqlite3.Connection, settings: Settings) -> None:
    text, kb = _employee_list_text_and_kb(conn, page=0)
    await message.answer(text, reply_markup=kb)


@router.callback_query(IsAdminCb(), F.data.startswith("emp:"))
async def admin_employees_ui_cb(
    cb: CallbackQuery, conn: sqlite3.Connection, settings: Settings
) -> None:
    raw = cb.data or ""
    parts = raw.split(":")
    if len(parts) < 2 or parts[0] != "emp":
        await cb.answer()
        return

    action = parts[1]

    if action == "hi" and len(parts) == 4:
        try:
            p = int(parts[2])
            ps = int(parts[3])
        except ValueError:
            await cb.answer()
            return
        await cb.answer(f"Страница {p + 1} из {ps}", show_alert=False)
        return

    def _int_tail(idx: int) -> int | None:
        if len(parts) <= idx:
            return None
        try:
            return int(parts[idx])
        except ValueError:
            return None

    if action == "pg":
        page = _int_tail(2)
        if page is None:
            await cb.answer()
            return
        text, kb = _employee_list_text_and_kb(conn, page=page)
        await cb.message.edit_text(text, reply_markup=kb)
        await cb.answer()
        return

    emp_id = _int_tail(2)
    if emp_id is None and action not in {"hi"}:
        await cb.answer()
        return

    if action == "vw":
        r = db.get_employee_by_id(conn, emp_id)
        if not r:
            await cb.answer("Запись не найдена", show_alert=True)
            return
        await cb.message.edit_text(
            _format_employee_card(r),
            reply_markup=_employee_view_kb(r),
        )
        await cb.answer()
        return

    if action == "ul":
        r = db.get_employee_by_id(conn, emp_id)
        if not r:
            await cb.answer("Не найдено", show_alert=True)
            return
        db.unlink_employee_telegram(conn, r.id)
        r2 = db.get_employee_by_id(conn, emp_id)
        if r2:
            await cb.message.edit_text(
                _format_employee_card(r2) + "\n\nПривязка Telegram снята.",
                reply_markup=_employee_view_kb(r2),
            )
        await cb.answer("Готово")
        return

    if action == "off":
        r = db.get_employee_by_id(conn, emp_id)
        if not r:
            await cb.answer("Не найдено", show_alert=True)
            return
        db.deactivate_employee(conn, r.id)
        r2 = db.get_employee_by_id(conn, emp_id)
        if r2:
            await cb.message.edit_text(
                _format_employee_card(r2) + "\n\nСотрудник отключён.",
                reply_markup=_employee_view_kb(r2),
            )
        await cb.answer("Отключён")
        return

    if action == "on":
        r = db.get_employee_by_id(conn, emp_id)
        if not r:
            await cb.answer("Не найдено", show_alert=True)
            return
        db.activate_employee(conn, r.id)
        r2 = db.get_employee_by_id(conn, emp_id)
        if r2:
            await cb.message.edit_text(
                _format_employee_card(r2) + "\n\nСотрудник снова активен.",
                reply_markup=_employee_view_kb(r2),
            )
        await cb.answer("Включён")
        return

    if action == "del":
        r = db.get_employee_by_id(conn, emp_id)
        if not r:
            await cb.answer("Не найдено", show_alert=True)
            return
        await cb.message.edit_text(
            _format_employee_card(r)
            + "\n\nУдалить сотрудника и все связанные заказы? Это необратимо.",
            reply_markup=_employee_view_kb(r, deleting=True),
        )
        await cb.answer()
        return

    if action == "can":
        r = db.get_employee_by_id(conn, emp_id)
        if not r:
            text, kb = _employee_list_text_and_kb(conn, page=0)
            await cb.message.edit_text(text, reply_markup=kb)
            await cb.answer("Отменено")
            return
        await cb.message.edit_text(
            _format_employee_card(r),
            reply_markup=_employee_view_kb(r),
        )
        await cb.answer("Отменено")
        return

    if action == "ok":
        r = db.get_employee_by_id(conn, emp_id)
        if not r:
            await cb.answer("Уже удалён", show_alert=True)
            text, kb = _employee_list_text_and_kb(conn, page=0)
            await cb.message.edit_text(text, reply_markup=kb)
            return
        db.delete_employee(conn, emp_id)
        text, kb = _employee_list_text_and_kb(conn, page=0)
        await cb.message.edit_text(
            f"Сотрудник {r.last_name} {r.first_name} удалён из базы.\n\n{text}",
            reply_markup=kb,
        )
        await cb.answer("Удалено")
        return

    await cb.answer()


@router.message(IsAdmin(), IsTestMode(), F.text == "Тест: меню всем")
async def btn_test_menu_all(message: Message, conn: sqlite3.Connection, settings: Settings) -> None:
    report = await test_broadcast_menu_now(message.bot, conn, settings, only_user_id=None)
    await message.answer(report, reply_markup=admin_main_kb(settings))


@router.message(IsAdmin(), IsTestMode(), F.text == "Тест: меню мне")
async def btn_test_menu_me(message: Message, conn: sqlite3.Connection, settings: Settings) -> None:
    uid = message.from_user.id if message.from_user else 0
    report = await test_broadcast_menu_now(message.bot, conn, settings, only_user_id=uid)
    await message.answer(report, reply_markup=admin_main_kb(settings))


@router.message(IsAdmin(), IsTestMode(), F.text == "Тест: закрыть заказы")
async def btn_test_closed(message: Message, conn: sqlite3.Connection, settings: Settings) -> None:
    set_test_deadline_override(True)
    report = await test_broadcast_orders_closed(message.bot, conn, settings)
    await message.answer(
        "Режим теста: дедлайн для заказов считается пройденным.\n" + report,
        reply_markup=admin_main_kb(settings),
    )


@router.message(IsAdmin(), IsTestMode(), F.text == "Тест: открыть заказы")
async def btn_test_open(message: Message, settings: Settings) -> None:
    set_test_deadline_override(False)
    await message.answer(
        "Режим теста: дедлайн считается НЕ наступившим — можно оформлять заказ (до «Тест: сброс»).",
        reply_markup=admin_main_kb(settings),
    )


@router.message(IsAdmin(), IsTestMode(), F.text == "Тест: будний день")
async def btn_test_weekday_on(message: Message, settings: Settings) -> None:
    set_test_weekday_override(True)
    await message.answer(
        "Режим теста: «сегодня будний день» для заказов (до «Тест: сброс»).",
        reply_markup=admin_main_kb(settings),
    )


@router.message(IsAdmin(), IsTestMode(), F.text == "Тест: выходной")
async def btn_test_weekday_off(message: Message, settings: Settings) -> None:
    set_test_weekday_override(False)
    await message.answer(
        "Режим теста: «сегодня выходной» для заказов (до «Тест: сброс»).",
        reply_markup=admin_main_kb(settings),
    )


@router.message(IsAdmin(), IsTestMode(), F.text == "Тест: сброс")
async def btn_test_reset(message: Message, conn: sqlite3.Connection, settings: Settings) -> None:
    set_test_deadline_override(None)
    set_test_weekday_override(None)
    db.reset_all_runtime_data(conn)
    await message.answer(
        "Тестовый сброс выполнен.\n"
        "Удалены сотрудники, меню, заказы и служебные отметки.\n"
        "Бот приведён к состоянию 'как новый'.",
        reply_markup=admin_main_kb(settings),
    )


@router.message(IsAdmin(), IsTestMode(), F.text == "Тест: проверить IMAP")
async def btn_test_imap(message: Message, settings: Settings) -> None:
    if not (settings.imap_host and settings.imap_user and settings.imap_password):
        await message.answer(
            "IMAP не настроен: в .env нужны IMAP_HOST, IMAP_USER, IMAP_PASSWORD.",
            reply_markup=admin_main_kb(settings),
        )
        return
    await message.answer("Проверяю IMAP (несколько секунд)…", reply_markup=admin_main_kb(settings))
    text = await asyncio.to_thread(
        imap_diagnose_connection,
        settings.imap_host,
        settings.imap_port,
        settings.imap_user,
        settings.imap_password,
        sender_filter=settings.imap_sender_filter,
        only_unseen=settings.imap_only_unseen,
    )
    part = 3800
    for i in range(0, len(text), part):
        await message.answer(
            text[i : i + part],
            reply_markup=admin_main_kb(settings) if i + part >= len(text) else None,
        )


@router.message(IsAdmin(), F.text == "Загрузить меню")
async def admin_upload_hint(message: Message, settings: Settings) -> None:
    await message.answer(
        "Пришлите файл .docx с меню на сегодня сообщением-документом.",
        reply_markup=admin_main_kb(settings),
    )


@router.message(IsAdmin(), F.document)
async def admin_upload_docx(message: Message, conn: sqlite3.Connection, settings: Settings) -> None:
    if not is_admin(message.from_user.id, settings):
        return
    doc = message.document
    if not doc:
        return
    fn = doc.file_name or ""
    if not fn.lower().endswith(".docx"):
        await message.answer("Нужен файл .docx", reply_markup=admin_main_kb(settings))
        return
    bot = message.bot
    f = await bot.get_file(doc.file_id)
    buf = io.BytesIO()
    await bot.download_file(f.file_path, buf)
    data = buf.getvalue()
    try:
        items = parse_docx_bytes(data)
    except Exception as e:  # noqa: BLE001
        await message.answer(f"Не удалось разобрать файл: {e}", reply_markup=admin_main_kb(settings))
        return
    if not items:
        await message.answer(
            "Не найдено ни одной строки меню (название + цена).",
            reply_markup=admin_main_kb(settings),
        )
        return
    today = local_today(settings.tz)
    db.create_menu(conn, today, "manual", items)
    await message.answer(
        f"Меню на {today.isoformat()} загружено, позиций: {len(items)}.",
        reply_markup=admin_main_kb(settings),
    )


@router.message(IsAdmin(), F.text == "Месячный отчёт")
async def admin_monthly_manual(message: Message, conn: sqlite3.Connection, settings: Settings) -> None:
    if not settings.admin_ids:
        await message.answer("ADMIN_IDS не задан в .env.", reply_markup=admin_main_kb(settings))
        return
    await send_monthly_report_previous(message.bot, conn, settings, mark_sent=False)
    await message.answer(
        "Отчёт отправлен администраторам (внеочередной, без блокировки авто-отчёта).",
        reply_markup=admin_main_kb(settings),
    )
