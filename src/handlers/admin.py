from __future__ import annotations

import io
import sqlite3

from aiogram import F, Router
from aiogram.filters import BaseFilter, Command, StateFilter
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
from ..jobs import (
    send_monthly_report_previous,
    test_broadcast_menu_now,
    test_broadcast_orders_closed,
)
from ..menu_parse import parse_docx_bytes
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
from .states import AdminStates

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


def _parse_fio(text: str) -> tuple[str, str] | None:
    parts = text.split()
    if len(parts) < 2:
        return None
    return parts[0], parts[1]


def _admin_panel_text(conn: sqlite3.Connection, uid: int, settings: Settings) -> str:
    emp = db.get_employee_by_tg(conn, uid)
    if emp:
        bind_hint = (
            f"Вы привязаны как {emp.last_name} {emp.first_name}.\n"
            "Заказ — кнопками «Заказ на сегодня» и «Корзина»."
        )
    else:
        bind_hint = (
            "Для заказа еды нажмите /start и введите фамилию и имя — запись создастся сама "
            "или привяжется к уже существующей. Либо «Привязка для заказа»."
        )
    test_block = ""
    if settings.test_mode:
        test_block = (
            "\nРежим TEST_MODE: заказы в выходные и в любое время; дедлайн не действует.\n"
        )
    return (
        "Админ-панель.\n\n"
        "Сотрудники и меню — через кнопки ниже."
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
            "Снять привязку можно кнопкой «Снять привязку»."
        )
        return
    await state.set_state(AdminStates.waiting_bind_fio)
    await message.answer(
        "Введите фамилию и имя через пробел.\n"
        "Если записи ещё нет — она создастся автоматически (как при /start)."
    )


@router.message(IsAdmin(), StateFilter(AdminStates.waiting_bind_fio), F.text, ~F.text.startswith("/"))
async def admin_do_bind_for_order(
    message: Message,
    state: FSMContext,
    conn: sqlite3.Connection,
    settings: Settings,
) -> None:
    raw = (message.text or "").strip()
    parsed = _parse_fio(raw)
    if not parsed:
        await message.answer("Нужно два слова: Фамилия Имя.")
        return
    uid = message.from_user.id if message.from_user else 0
    username = message.from_user.username if message.from_user else None
    last_name, first_name = parsed
    emp = db.find_employee_by_name(conn, last_name, first_name)
    if not emp:
        # Для админа в этом сценарии разрешаем создать себя в списке сотрудников.
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
    if emp.telegram_user_id is not None and emp.telegram_user_id != uid:
        await message.answer("Этот сотрудник уже привязан к другому Telegram-аккаунту.")
        return
    try:
        db.link_employee_telegram(conn, emp.id, uid, telegram_username=username)
    except sqlite3.IntegrityError:
        await message.answer("Этот Telegram уже привязан к другой записи. Обратитесь к администратору.")
        return
    await state.clear()
    await message.answer(
        "Привязка выполнена. Заказ — «Заказ на сегодня» или «Корзина».",
        reply_markup=admin_main_kb(settings),
    )


@router.message(IsAdmin(), F.text == "Добавить сотрудника")
async def admin_prompt_add(message: Message, state: FSMContext, settings: Settings) -> None:
    await state.set_state(AdminStates.waiting_add_fio)
    await message.answer(
        "Введите фамилию и имя нового сотрудника через пробел.\n"
        "Пример: Иванов Иван",
        reply_markup=admin_main_kb(settings),
    )


@router.message(IsAdmin(), StateFilter(AdminStates.waiting_add_fio), F.text, ~F.text.startswith("/"))
async def admin_do_add(
    message: Message,
    state: FSMContext,
    conn: sqlite3.Connection,
    settings: Settings,
) -> None:
    parsed = _parse_fio((message.text or "").strip())
    if not parsed:
        await message.answer("Нужно два слова: Фамилия Имя.")
        return
    last_name, first_name = parsed
    try:
        eid = db.add_employee(conn, last_name, first_name)
    except sqlite3.IntegrityError:
        await message.answer("Такой сотрудник уже есть (ФИО должно быть уникальным).")
        return
    await state.clear()
    await message.answer(f"Сотрудник добавлен, id={eid}.", reply_markup=admin_main_kb(settings))


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


@router.message(IsAdmin(), F.text == "Снять привязку")
async def admin_prompt_unlink(message: Message, state: FSMContext, settings: Settings) -> None:
    await state.set_state(AdminStates.waiting_unlink_fio)
    await message.answer(
        "Введите фамилию и имя сотрудника, у которого сбросить привязку Telegram.",
        reply_markup=admin_main_kb(settings),
    )


@router.message(IsAdmin(), StateFilter(AdminStates.waiting_unlink_fio), F.text, ~F.text.startswith("/"))
async def admin_do_unlink(
    message: Message,
    state: FSMContext,
    conn: sqlite3.Connection,
    settings: Settings,
) -> None:
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Нужно: Фамилия Имя.")
        return
    emp = db.find_employee_by_name_admin(conn, parts[0], parts[1])
    if not emp:
        await message.answer("Не найден.")
        return
    db.unlink_employee_telegram(conn, emp.id)
    await state.clear()
    await message.answer("Привязка Telegram сброшена.", reply_markup=admin_main_kb(settings))


@router.message(IsAdmin(), F.text == "Отключить сотрудника")
async def admin_prompt_deactivate(message: Message, state: FSMContext, settings: Settings) -> None:
    await state.set_state(AdminStates.waiting_deactivate_fio)
    await message.answer(
        "Введите фамилию и имя сотрудника для отключения.",
        reply_markup=admin_main_kb(settings),
    )


@router.message(IsAdmin(), StateFilter(AdminStates.waiting_deactivate_fio), F.text, ~F.text.startswith("/"))
async def admin_do_deactivate(
    message: Message,
    state: FSMContext,
    conn: sqlite3.Connection,
    settings: Settings,
) -> None:
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Нужно: Фамилия Имя.")
        return
    emp = db.find_employee_by_name_admin(conn, parts[0], parts[1])
    if not emp:
        await message.answer("Не найден.")
        return
    db.deactivate_employee(conn, emp.id)
    await state.clear()
    await message.answer("Сотрудник отключён.", reply_markup=admin_main_kb(settings))


@router.message(IsAdmin(), F.text == "Удалить сотрудника")
async def admin_prompt_delete(message: Message, state: FSMContext, settings: Settings) -> None:
    await state.set_state(AdminStates.waiting_delete_fio)
    await message.answer(
        "Введите фамилию и имя сотрудника для ПОЛНОГО удаления из базы.",
        reply_markup=admin_main_kb(settings),
    )


@router.message(IsAdmin(), StateFilter(AdminStates.waiting_delete_fio), F.text, ~F.text.startswith("/"))
async def admin_prepare_delete(
    message: Message,
    state: FSMContext,
    conn: sqlite3.Connection,
    settings: Settings,
) -> None:
    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer("Нужно: Фамилия Имя.")
        return
    emp = db.find_employee_by_name_admin(conn, parts[0], parts[1])
    if not emp:
        await message.answer("Не найден.")
        return
    await state.update_data(delete_employee_id=emp.id, delete_employee_fio=f"{emp.last_name} {emp.first_name}")
    await state.set_state(AdminStates.waiting_delete_confirm)
    await message.answer(
        f"Подтвердите удаление сотрудника: {emp.last_name} {emp.first_name}.\n"
        "Это удалит сотрудника и связанные заказы.\n"
        "Ответьте: ДА (для удаления) или Нет (для отмены).",
        reply_markup=admin_main_kb(settings),
    )


@router.message(IsAdmin(), StateFilter(AdminStates.waiting_delete_confirm), F.text, ~F.text.startswith("/"))
async def admin_confirm_delete(
    message: Message,
    state: FSMContext,
    conn: sqlite3.Connection,
    settings: Settings,
) -> None:
    answer = (message.text or "").strip().casefold()
    if answer in {"нет", "no", "n"}:
        await state.clear()
        await message.answer("Удаление отменено.", reply_markup=admin_main_kb(settings))
        return
    if answer not in {"да", "yes", "y"}:
        await message.answer("Введите ДА для удаления или Нет для отмены.")
        return
    data = await state.get_data()
    emp_id = data.get("delete_employee_id")
    emp_fio = data.get("delete_employee_fio", "сотрудник")
    if not isinstance(emp_id, int):
        await state.clear()
        await message.answer("Состояние удаления потеряно, начните заново.", reply_markup=admin_main_kb(settings))
        return
    db.delete_employee(conn, emp_id)
    await state.clear()
    await message.answer(f"{emp_fio} удалён из базы.", reply_markup=admin_main_kb(settings))


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


@router.message(IsAdmin(), F.text == "Сводка столовой")
async def admin_canteen_choose(message: Message, settings: Settings) -> None:
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Все форматы (Excel+CSV+текст)",
                    callback_data="can:all",
                ),
            ],
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
    items = aggregate_daily_canteen(conn, today)
    chat_id = settings.canteen_chat_id
    if not chat_id:
        await cb.answer("CANTEEN_CHAT_ID не задан.", show_alert=True)
        return

    caption = f"Сводка на {today.isoformat()}"
    bot = cb.bot
    if fmt == "all":
        xlsx_data = build_canteen_excel_bytes(items)
        xlsx_name = f"canteen_{today.isoformat()}.xlsx"
        await bot.send_document(
            chat_id,
            document=BufferedInputFile(xlsx_data, filename=xlsx_name),
            caption=f"{caption} (Excel)",
        )
        csv_data = build_canteen_csv_bytes(items)
        csv_name = f"canteen_{today.isoformat()}.csv"
        await bot.send_document(
            chat_id,
            document=BufferedInputFile(csv_data, filename=csv_name),
            caption=f"{caption} (CSV)",
        )
        text = format_canteen_text(items)
        chunk = 3800
        for i in range(0, len(text), chunk):
            await bot.send_message(chat_id, text[i : i + chunk])
        await cb.message.answer(
            "Сводка отправлена: Excel, CSV и текстом.",
            reply_markup=admin_main_kb(settings),
        )
        await cb.answer()
        return
    if fmt == "txt":
        text = format_canteen_text(items)
        chunk = 3800
        for i in range(0, len(text), chunk):
            await bot.send_message(chat_id, text[i : i + chunk])
        await cb.message.answer("Сводка отправлена текстом.", reply_markup=admin_main_kb(settings))
        await cb.answer()
        return
    if fmt == "xlsx":
        data = build_canteen_excel_bytes(items)
        fname = f"canteen_{today.isoformat()}.xlsx"
        await bot.send_document(
            chat_id,
            document=BufferedInputFile(data, filename=fname),
            caption=caption,
        )
    elif fmt == "csv":
        data = build_canteen_csv_bytes(items)
        fname = f"canteen_{today.isoformat()}.csv"
        await bot.send_document(
            chat_id,
            document=BufferedInputFile(data, filename=fname),
            caption=caption,
        )
    else:
        await cb.answer("Неизвестный формат", show_alert=True)
        return
    await cb.message.answer("Сводка отправлена файлом.", reply_markup=admin_main_kb(settings))
    await cb.answer()


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
