from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, time, timedelta
from typing import Literal

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import db
from .config import Settings
from .handlers.common import employee_main_kb
from .imap_client import fetch_latest_docx_attachments
from .menu_export import build_menu_txt_bytes
from .menu_parse import parse_docx_bytes
from .reports import (
    aggregate_daily_canteen,
    build_canteen_csv_bytes,
    build_canteen_excel_bytes,
    build_monthly_xlsx,
    format_canteen_text,
    monthly_totals_by_employee,
)
from .timeutil import (
    cron_hm_before_deadline,
    is_weekday,
    is_weekday_effective,
    local_now,
    local_today,
    parse_hhmm,
    previous_month,
    zone,
)
from aiogram.types import BufferedInputFile, ReplyKeyboardMarkup

log = logging.getLogger(__name__)


def collect_menu_broadcast_recipients(conn: sqlite3.Connection, settings: Settings) -> set[int]:
    recipients: set[int] = set(settings.admin_ids)
    for emp in db.list_employees(conn, active_only=True):
        tid = emp.telegram_user_id
        if tid:
            recipients.add(tid)
    return recipients


def build_menu_broadcast_payload(
    conn: sqlite3.Connection, settings: Settings
) -> tuple[bytes, str, str, ReplyKeyboardMarkup] | None:
    today = local_today(settings.tz)
    mid = db.get_menu_for_date(conn, today)
    if not mid:
        return None
    items = db.list_menu_items(conn, mid)
    if not items:
        return None
    data = build_menu_txt_bytes(items)
    fname = f"menu_{today.isoformat()}.txt"
    caption = "Меню на сегодня. Оформите заказ кнопкой «Заказ на сегодня»."
    return data, fname, caption, employee_main_kb()


async def _send_bulk(
    bot: Bot,
    user_ids: set[int],
    text: str,
    reply_markup: ReplyKeyboardMarkup | None,
) -> tuple[int, list[str]]:
    ok = 0
    errors: list[str] = []
    for tid in sorted(user_ids):
        try:
            await bot.send_message(tid, text, reply_markup=reply_markup)
            ok += 1
        except Exception as e:  # noqa: BLE001
            log.warning("Send to %s failed: %s", tid, e)
            errors.append(f"{tid}: {e}")
    return ok, errors


async def _send_bulk_document(
    bot: Bot,
    user_ids: set[int],
    data: bytes,
    filename: str,
    caption: str,
    reply_markup: ReplyKeyboardMarkup | None,
) -> tuple[int, list[str]]:
    ok = 0
    errors: list[str] = []
    for tid in sorted(user_ids):
        try:
            await bot.send_document(
                tid,
                document=BufferedInputFile(data, filename=filename),
                caption=caption,
                reply_markup=reply_markup,
            )
            ok += 1
        except Exception as e:  # noqa: BLE001
            log.warning("Send to %s failed: %s", tid, e)
            errors.append(f"{tid}: {e}")
    return ok, errors


async def test_broadcast_menu_now(
    bot: Bot,
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    only_user_id: int | None = None,
) -> str:
    """Ручная рассылка меню (не ставит отметку menu_broadcasts — планировщик может отработать как обычно)."""
    payload = build_menu_broadcast_payload(conn, settings)
    if not payload:
        return "Нет меню на сегодня: загрузите .docx (админка) или дождитесь IMAP."
    data, fname, caption, kb = payload
    recipients = {only_user_id} if only_user_id is not None else collect_menu_broadcast_recipients(conn, settings)
    if not recipients:
        return "Некому слать: пусто ADMIN_IDS и нет привязанных сотрудников."
    ok, errs = await _send_bulk_document(bot, recipients, data, fname, caption, kb)
    extra = f"\nОшибки ({len(errs)}):\n" + "\n".join(errs[:5]) if errs else ""
    return f"Тестовая рассылка меню: доставлено {ok} из {len(recipients)}.{extra}"


TEST_ORDERS_CLOSED_TEXT = (
    "Приём заказов на сегодня закрыт.\n"
    "Заказ уже нельзя изменить — это тестовое сообщение или дедлайн прошёл."
)


async def test_broadcast_orders_closed(
    bot: Bot,
    conn: sqlite3.Connection,
    settings: Settings,
) -> str:
    recipients = collect_menu_broadcast_recipients(conn, settings)
    if not recipients:
        return "Некому слать: пусто ADMIN_IDS и нет привязанных сотрудников."
    ok, errs = await _send_bulk(bot, recipients, TEST_ORDERS_CLOSED_TEXT, None)
    extra = f"\nОшибки ({len(errs)}):\n" + "\n".join(errs[:5]) if errs else ""
    return f"Сообщение «заказы закрыты»: доставлено {ok} из {len(recipients)}.{extra}"


def _has_menu_with_items_today(conn: sqlite3.Connection, settings: Settings) -> bool:
    today = local_today(settings.tz)
    mid = db.get_menu_for_date(conn, today)
    if mid is None:
        return False
    return len(db.list_menu_items(conn, mid)) > 0


def _next_weekday_broadcast_after(now: datetime, broadcast_t: time) -> datetime:
    """Следующая рассылка меню (пн–пт) строго после момента now, в той же таймзоне."""
    tz = now.tzinfo
    if tz is None:
        raise ValueError("now must be timezone-aware")
    d = now.date()
    for _ in range(14):
        if d.weekday() < 5:
            cand = datetime.combine(d, broadcast_t, tzinfo=tz)
            if cand > now:
                return cand
        d += timedelta(days=1)
    raise RuntimeError("could not find next weekday menu broadcast")


def _imap_in_quiet_period(settings: Settings) -> bool:
    """
    Не трогать почту после закрытия заказов до «за час до открытия» следующего цикла
    (время рассылки меню = начало приёма заказов на новый день).
    """
    if settings.test_mode:
        return False
    now = local_now(settings.tz)
    deadline_t = parse_hhmm(settings.order_deadline_time)
    broadcast_t = parse_hhmm(settings.menu_broadcast_time)
    next_b = _next_weekday_broadcast_after(now, broadcast_t)
    resume = next_b - timedelta(hours=1)
    if not is_weekday_effective(settings.tz):
        return now < resume
    today_deadline = datetime.combine(now.date(), deadline_t, tzinfo=now.tzinfo)
    return now > today_deadline and now < resume


def _imap_poll_is_urgent(conn: sqlite3.Connection, settings: Settings) -> bool:
    """Будни, меню на сегодня ещё нет, локальное время уже после порога — чаще опрашивать почту."""
    if not is_weekday(settings.tz):
        return False
    if _has_menu_with_items_today(conn, settings):
        return False
    now = local_now(settings.tz)
    return now.time() >= parse_hhmm(settings.imap_urgent_after)


async def process_imap_and_menu(conn: sqlite3.Connection, settings: Settings) -> None:
    if not (settings.imap_host and settings.imap_user and settings.imap_password):
        log.debug("IMAP poll: пропуск — в .env не заданы host/user/password")
        return
    log.info(
        "IMAP poll: загрузка вложений (host=%s port=%s user=%s only_unseen=%s)",
        settings.imap_host,
        settings.imap_port,
        settings.imap_user,
        settings.imap_only_unseen,
    )
    try:
        atts = fetch_latest_docx_attachments(
            settings.imap_host,
            settings.imap_port,
            settings.imap_user,
            settings.imap_password,
            sender_filter=settings.imap_sender_filter,
            only_unseen=settings.imap_only_unseen,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("IMAP poll: ошибка при получении писем: %s", e, exc_info=True)
        return

    today = local_today(settings.tz)
    log.info("IMAP poll: получено вложений .docx для разбора: %s (дата меню: %s)", len(atts), today)
    for att in atts:
        if db.is_email_processed(conn, att.message_id):
            log.info("IMAP poll: письмо уже обработано, пропуск: %s (%s)", att.message_id, att.filename)
            continue
        try:
            items = parse_docx_bytes(att.data)
        except Exception as e:  # noqa: BLE001
            log.warning("IMAP poll: ошибка разбора DOCX %s: %s", att.filename, e, exc_info=True)
            continue
        if not items:
            log.info("IMAP poll: в %s нет строк меню — помечаю письмо обработанным", att.filename)
            db.mark_email_processed(conn, att.message_id)
            continue
        db.create_menu(conn, today, "imap", items)
        db.mark_email_processed(conn, att.message_id)
        log.info("IMAP poll: меню на %s обновлено из %s, позиций: %s", today, att.filename, len(items))


async def process_imap_scheduled(
    conn: sqlite3.Connection, settings: Settings, *, urgent_only: bool
) -> None:
    if not (settings.imap_host and settings.imap_user and settings.imap_password):
        return
    if _imap_in_quiet_period(settings):
        log.info(
            "IMAP job (urgent_only=%s): тихий период после дедлайна до ~часа перед рассылкой меню — опрос не выполняется",
            urgent_only,
        )
        return
    if _has_menu_with_items_today(conn, settings):
        log.info(
            "IMAP job (urgent_only=%s): меню на сегодня уже есть — опрос не нужен",
            urgent_only,
        )
        return
    urgent = _imap_poll_is_urgent(conn, settings)
    if urgent_only and not urgent:
        log.debug(
            "IMAP job urgent_only: пропуск (срочный=%s, порог IMAP_URGENT_AFTER=%s)",
            urgent,
            settings.imap_urgent_after,
        )
        return
    if not urgent_only and urgent:
        log.debug(
            "IMAP job slow: пропуск (срочный=%s — сейчас опрос делает urgent-интервал)",
            urgent,
        )
        return
    log.info("IMAP job (urgent_only=%s): запуск process_imap_and_menu", urgent_only)
    await process_imap_and_menu(conn, settings)


async def broadcast_weekday_menu(bot: Bot, conn: sqlite3.Connection, settings: Settings) -> None:
    if not is_weekday(settings.tz):
        return
    today = local_today(settings.tz)
    if db.was_menu_broadcast(conn, today):
        return
    payload = build_menu_broadcast_payload(conn, settings)
    if not payload:
        log.info("No menu to broadcast on %s", today)
        return
    data, fname, caption, kb = payload
    recipients = collect_menu_broadcast_recipients(conn, settings)
    await _send_bulk_document(bot, recipients, data, fname, caption, kb)
    db.mark_menu_broadcast(conn, today)
    log.info("Menu broadcast done for %s", today)


def _collect_no_order_recipients(conn: sqlite3.Connection, settings: Settings) -> set[int]:
    """
    Сотрудники, которым нужно напомнить о заказе:
    - активные и привязанные к Telegram
    - на сегодня нет заказа, либо заказ есть, но без позиций
    """
    today = local_today(settings.tz)
    recipients: set[int] = set()
    for emp in db.list_employees(conn, active_only=True):
        tid = emp.telegram_user_id
        if not tid:
            continue
        od = db.get_order_for_employee_date(conn, emp.id, today)
        if od is None:
            recipients.add(tid)
            continue
        order_id, _status = od
        if db.count_distinct_dishes_in_order(conn, order_id) == 0:
            recipients.add(tid)
    return recipients


def _collect_draft_cart_unconfirmed_recipients(conn: sqlite3.Connection, settings: Settings) -> set[int]:
    """
    Сотрудники с непустой корзиной на сегодня, но заказ ещё в статусе draft (не подтверждён).
    """
    today = local_today(settings.tz)
    recipients: set[int] = set()
    for emp in db.list_employees(conn, active_only=True):
        tid = emp.telegram_user_id
        if not tid:
            continue
        od = db.get_order_for_employee_date(conn, emp.id, today)
        if od is None:
            continue
        order_id, st = od
        if st != "draft":
            continue
        if db.count_distinct_dishes_in_order(conn, order_id) == 0:
            continue
        recipients.add(tid)
    return recipients


async def remind_no_order_before_deadline(
    bot: Bot, conn: sqlite3.Connection, settings: Settings
) -> None:
    """
    Будни: за ORDER_REMINDER_BEFORE_DEADLINE_MINUTES до ORDER_DEADLINE_TIME — напоминание тем,
    у кого нет заказа или пустая корзина. Один раз за календарный день; без меню не шлём.
    """
    if settings.order_reminder_before_deadline_minutes <= 0:
        return
    if not is_weekday(settings.tz):
        return
    today = local_today(settings.tz)
    if db.was_pre_deadline_reminder_sent(conn, today):
        return
    if not _has_menu_with_items_today(conn, settings):
        log.info("Pre-deadline no-order reminder: нет меню на %s — пропуск", today)
        return
    deadline_t = parse_hhmm(settings.order_deadline_time)
    tz = zone(settings.tz)
    deadline_dt = datetime.combine(today, deadline_t, tzinfo=tz)
    reminder_dt = deadline_dt - timedelta(minutes=settings.order_reminder_before_deadline_minutes)
    if reminder_dt.date() != today:
        log.warning(
            "Pre-deadline no-order reminder: окно напоминания не в текущие сутки "
            "(дедлайн %s, за %s мин) — пропуск; сдвиньте ORDER_DEADLINE_TIME или уменьшите ORDER_REMINDER_BEFORE_DEADLINE_MINUTES",
            settings.order_deadline_time,
            settings.order_reminder_before_deadline_minutes,
        )
        return
    recipients = _collect_no_order_recipients(conn, settings)
    if not recipients:
        log.info("Pre-deadline no-order reminder: все уже заказали на %s", today)
        db.mark_pre_deadline_reminder_sent(conn, today)
        return
    n = settings.order_reminder_before_deadline_minutes
    text = (
        f"Через ~{n} минут закончится приём заказов на сегодня (до {settings.order_deadline_time}).\n"
        "Если ещё не оформили заказ — откройте «Заказ на сегодня»."
    )
    kb = employee_main_kb()
    ok, errs = await _send_bulk(bot, recipients, text, kb)
    db.mark_pre_deadline_reminder_sent(conn, today)
    log.info("Pre-deadline no-order reminder: доставлено %s/%s", ok, len(recipients))
    if errs:
        log.warning("Pre-deadline no-order reminder: ошибок %s", len(errs))


async def remind_draft_cart_before_deadline(
    bot: Bot, conn: sqlite3.Connection, settings: Settings
) -> None:
    """
    Будни: за ORDER_DRAFT_CART_REMINDER_BEFORE_DEADLINE_MINUTES до дедлайна — тем, у кого в корзине
    есть позиции, но заказ не подтверждён. Один раз за календарный день; без меню не шлём.
    """
    if settings.order_draft_cart_reminder_before_deadline_minutes <= 0:
        return
    if not is_weekday(settings.tz):
        return
    today = local_today(settings.tz)
    if db.was_draft_cart_pre_deadline_reminder_sent(conn, today):
        return
    if not _has_menu_with_items_today(conn, settings):
        log.info("Pre-deadline draft-cart reminder: нет меню на %s — пропуск", today)
        return
    deadline_t = parse_hhmm(settings.order_deadline_time)
    tz = zone(settings.tz)
    deadline_dt = datetime.combine(today, deadline_t, tzinfo=tz)
    n = settings.order_draft_cart_reminder_before_deadline_minutes
    reminder_dt = deadline_dt - timedelta(minutes=n)
    if reminder_dt.date() != today:
        log.warning(
            "Pre-deadline draft-cart reminder: окно напоминания не в текущие сутки "
            "(дедлайн %s, за %s мин) — пропуск",
            settings.order_deadline_time,
            n,
        )
        return
    recipients = _collect_draft_cart_unconfirmed_recipients(conn, settings)
    if not recipients:
        log.info("Pre-deadline draft-cart reminder: нет черновиков с позициями на %s", today)
        db.mark_draft_cart_pre_deadline_reminder_sent(conn, today)
        return
    text = (
        f"Через ~{n} минут закончится приём заказов на сегодня (до {settings.order_deadline_time}).\n"
        "У вас в корзине уже выбраны блюда, но заказ не подтверждён. Откройте «Заказ на сегодня» "
        "и нажмите подтверждение, иначе столовая заказ не получит."
    )
    kb = employee_main_kb()
    ok, errs = await _send_bulk(bot, recipients, text, kb)
    db.mark_draft_cart_pre_deadline_reminder_sent(conn, today)
    log.info("Pre-deadline draft-cart reminder: доставлено %s/%s", ok, len(recipients))
    if errs:
        log.warning("Pre-deadline draft-cart reminder: ошибок %s", len(errs))


_CANTEEN_TEXT_CHUNK = 3800


async def send_canteen_summary_to_chat(
    bot: Bot,
    conn: sqlite3.Connection,
    order_date: date,
    chat_id: int,
) -> tuple[bool, str | None]:
    """
    Отправка сводки (Excel + CSV + текст) в указанный Telegram-чат.
    Возвращает (True, None) при успехе, иначе (False, краткое описание ошибки для людей).
    """
    items = aggregate_daily_canteen(conn, order_date)
    caption = f"Сводка на {order_date.isoformat()}"
    try:
        xlsx_data = build_canteen_excel_bytes(items)
        xlsx_name = f"canteen_{order_date.isoformat()}.xlsx"
        await bot.send_document(
            chat_id,
            document=BufferedInputFile(xlsx_data, filename=xlsx_name),
            caption=f"{caption} (Excel)",
        )
        csv_data = build_canteen_csv_bytes(items)
        csv_name = f"canteen_{order_date.isoformat()}.csv"
        await bot.send_document(
            chat_id,
            document=BufferedInputFile(csv_data, filename=csv_name),
            caption=f"{caption} (CSV)",
        )
        text = format_canteen_text(items)
        for i in range(0, len(text), _CANTEEN_TEXT_CHUNK):
            await bot.send_message(chat_id, text[i : i + _CANTEEN_TEXT_CHUNK])
    except Exception as e:  # noqa: BLE001
        log.exception("Сводка на %s не доставлена в чат %s: %s", order_date, chat_id, e)
        err = str(e).strip() or type(e).__name__
        if len(err) > 400:
            err = err[:397] + "..."
        return False, err
    return True, None


async def send_canteen_summary_to_canteen_chat(
    bot: Bot,
    conn: sqlite3.Connection,
    settings: Settings,
    order_date: date,
) -> tuple[bool, str | None]:
    """
    Отправка сводки в чат работника столовой (CANTEEN_CHAT_ID).
    """
    chat_id = settings.canteen_chat_id
    if not chat_id:
        return False, "CANTEEN_CHAT_ID не задан в .env"
    return await send_canteen_summary_to_chat(bot, conn, order_date, chat_id)


async def _notify_admins_canteen_summary_failed(
    bot: Bot,
    settings: Settings,
    order_date: date,
    reason: str,
) -> None:
    if not settings.admin_ids:
        return
    text = (
        f"Сводка заказов за {order_date.isoformat()} не доставлена работнику столовой.\n"
        f"Причина: {reason}\n\n"
        "Нажмите «Сводка в столовую» в админке — файлы придут вам в чат с ботом; перешлите их работнику столовой."
    )
    for aid in settings.admin_ids:
        try:
            await bot.send_message(aid, text)
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось уведомить админа %s о сбое сводки столовой: %s", aid, e)


async def auto_send_canteen_summary_weekday(
    bot: Bot, conn: sqlite3.Connection, settings: Settings
) -> None:
    """
    После дедлайна заказов (по cron в ORDER_DEADLINE_TIME, пн–пт):
    отправка сводки в CANTEEN_CHAT_ID (Excel + CSV + текст), один раз за календарный день.
    При ошибке — уведомление всем админам; при успехе — только лог.
    """
    if not is_weekday(settings.tz):
        return
    today = local_today(settings.tz)
    if db.was_canteen_summary_sent(conn, today):
        return
    ok, err = await send_canteen_summary_to_canteen_chat(bot, conn, settings, today)
    if ok:
        db.mark_canteen_summary_sent(conn, today)
        log.info("Автосводка столовой отправлена за %s", today)
        return
    await _notify_admins_canteen_summary_failed(bot, settings, today, err or "неизвестная ошибка")


async def send_monthly_report_previous(
    bot: Bot,
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    mark_sent: bool,
    report_month: Literal["previous", "current"] = "previous",
) -> None:
    if not settings.admin_ids:
        log.warning("ADMIN_IDS пуст — месячный отчёт некуда отправить.")
        return
    today = local_today(settings.tz)
    if report_month == "current":
        y, m = today.year, today.month
    else:
        y, m = previous_month(today)
    ym = f"{y:04d}-{m:02d}"
    if mark_sent and db.was_monthly_report_sent(conn, ym):
        return
    rows = monthly_totals_by_employee(conn, y, m)
    data = build_monthly_xlsx(rows)
    fname = f"meals_{ym}.xlsx"
    suffix = " (текущий месяц)" if report_month == "current" else ""
    caption = f"Месячный отчёт {ym}{suffix} (сотрудник / сумма)"
    for aid in settings.admin_ids:
        try:
            await bot.send_document(
                aid,
                document=BufferedInputFile(data, filename=fname),
                caption=caption,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("Monthly report to admin %s failed: %s", aid, e)
    if mark_sent:
        db.mark_monthly_report_sent(conn, ym)


def setup_scheduler(bot: Bot, conn: sqlite3.Connection, settings: Settings) -> AsyncIOScheduler:
    sched = AsyncIOScheduler(timezone=settings.tz)

    h_m, m_m = settings.menu_broadcast_time.split(":")
    h_d, m_d = settings.order_deadline_time.split(":")

    sched.add_job(
        process_imap_scheduled,
        IntervalTrigger(minutes=3),
        args=[conn, settings],
        kwargs={"urgent_only": True},
        id="imap_urgent",
        replace_existing=True,
    )
    sched.add_job(
        process_imap_scheduled,
        IntervalTrigger(minutes=15),
        args=[conn, settings],
        kwargs={"urgent_only": False},
        id="imap_slow",
        replace_existing=True,
    )

    sched.add_job(
        broadcast_weekday_menu,
        CronTrigger(
            day_of_week="mon-fri",
            hour=int(h_m),
            minute=int(m_m),
            timezone=settings.tz,
        ),
        args=[bot, conn, settings],
        id="menu_broadcast",
        replace_existing=True,
    )

    if settings.order_reminder_before_deadline_minutes > 0:
        h_r, m_r = cron_hm_before_deadline(
            settings.order_deadline_time,
            settings.order_reminder_before_deadline_minutes,
        )
        sched.add_job(
            remind_no_order_before_deadline,
            CronTrigger(
                day_of_week="mon-fri",
                hour=h_r,
                minute=m_r,
                timezone=settings.tz,
            ),
            args=[bot, conn, settings],
            id="no_order_reminder_pre_deadline",
            replace_existing=True,
        )

    if settings.order_draft_cart_reminder_before_deadline_minutes > 0:
        h_c, m_c = cron_hm_before_deadline(
            settings.order_deadline_time,
            settings.order_draft_cart_reminder_before_deadline_minutes,
        )
        sched.add_job(
            remind_draft_cart_before_deadline,
            CronTrigger(
                day_of_week="mon-fri",
                hour=h_c,
                minute=m_c,
                timezone=settings.tz,
            ),
            args=[bot, conn, settings],
            id="draft_cart_reminder_pre_deadline",
            replace_existing=True,
        )

    sched.add_job(
        auto_send_canteen_summary_weekday,
        CronTrigger(
            day_of_week="mon-fri",
            hour=int(h_d),
            minute=int(m_d),
            timezone=settings.tz,
        ),
        args=[bot, conn, settings],
        id="canteen_auto_summary",
        replace_existing=True,
    )

    sched.add_job(
        send_monthly_report_previous,
        CronTrigger(day=1, hour=9, minute=0, timezone=settings.tz),
        args=[bot, conn, settings],
        kwargs={"mark_sent": True},
        id="monthly_auto",
        replace_existing=True,
    )

    return sched
