from __future__ import annotations

import logging
import sqlite3

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import db
from .config import Settings
from .handlers.common import employee_main_kb
from .imap_client import fetch_latest_docx_attachments
from .menu_parse import parse_docx_bytes
from .reports import build_monthly_xlsx, monthly_totals_by_employee
from .timeutil import is_weekday, local_today, previous_month
from aiogram.types import BufferedInputFile

log = logging.getLogger(__name__)


def _menu_lines(items: list[db.MenuItemRow]) -> str:
    lines = ["Меню на сегодня:"]
    for it in items:
        lines.append(f"• {it.dish_name} — {it.price:.2f} руб.")
    return "\n".join(lines)


async def process_imap_and_menu(conn: sqlite3.Connection, settings: Settings) -> None:
    if not (settings.imap_host and settings.imap_user and settings.imap_password):
        return
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
        log.warning("IMAP error: %s", e)
        return

    today = local_today(settings.tz)
    for att in atts:
        if db.is_email_processed(conn, att.message_id):
            continue
        try:
            items = parse_docx_bytes(att.data)
        except Exception as e:  # noqa: BLE001
            log.warning("DOCX parse error %s: %s", att.filename, e)
            continue
        if not items:
            log.info("No menu rows in %s", att.filename)
            db.mark_email_processed(conn, att.message_id)
            continue
        db.create_menu(conn, today, "imap", items)
        db.mark_email_processed(conn, att.message_id)
        log.info("Menu from IMAP updated: %s items", len(items))


async def broadcast_weekday_menu(bot: Bot, conn: sqlite3.Connection, settings: Settings) -> None:
    if not is_weekday(settings.tz):
        return
    today = local_today(settings.tz)
    if db.was_menu_broadcast(conn, today):
        return
    mid = db.get_menu_for_date(conn, today)
    if not mid:
        log.info("No menu to broadcast on %s", today)
        return
    items = db.list_menu_items(conn, mid)
    if not items:
        return
    text = _menu_lines(items) + "\n\nОформите заказ кнопкой «Заказ на сегодня»."
    kb = employee_main_kb()
    recipients: set[int] = set(settings.admin_ids)
    for emp in db.list_employees(conn, active_only=True):
        tid = emp.telegram_user_id
        if tid:
            recipients.add(tid)
    for tid in recipients:
        try:
            await bot.send_message(tid, text, reply_markup=kb)
        except Exception as e:  # noqa: BLE001
            log.warning("Broadcast fail %s: %s", tid, e)
    db.mark_menu_broadcast(conn, today)
    log.info("Menu broadcast done for %s", today)


async def send_monthly_report_previous(
    bot: Bot,
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    mark_sent: bool,
) -> None:
    if not settings.admin_ids:
        log.warning("ADMIN_IDS пуст — месячный отчёт некуда отправить.")
        return
    today = local_today(settings.tz)
    y, m = previous_month(today)
    ym = f"{y:04d}-{m:02d}"
    if mark_sent and db.was_monthly_report_sent(conn, ym):
        return
    rows = monthly_totals_by_employee(conn, y, m)
    data = build_monthly_xlsx(rows)
    fname = f"meals_{ym}.xlsx"
    caption = f"Месячный отчёт {ym} (сотрудник / сумма)"
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

    sched.add_job(
        process_imap_and_menu,
        IntervalTrigger(minutes=3),
        args=[conn, settings],
        id="imap",
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

    sched.add_job(
        send_monthly_report_previous,
        CronTrigger(day=1, hour=9, minute=0, timezone=settings.tz),
        args=[bot, conn, settings],
        kwargs={"mark_sent": True},
        id="monthly_auto",
        replace_existing=True,
    )

    return sched
