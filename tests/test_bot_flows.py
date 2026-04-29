from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import pytest

from src import db, jobs
from src.config import Settings
from src.handlers.employee_order import _menu_kb, _open_menu
from src.imap_client import MailAttachment
from src.timeutil import local_today


@dataclass
class _User:
    id: int


class _FakeMessage:
    def __init__(self, user_id: int) -> None:
        self.from_user = _User(user_id)
        self.answers: list[str] = []

    async def answer(self, text: str, reply_markup=None) -> None:  # noqa: ANN001
        self.answers.append(text)

    async def answer_document(self, document, caption: str, reply_markup=None) -> None:  # noqa: ANN001
        self.answers.append(caption)


class _FakeBot:
    def __init__(self) -> None:
        self.sent_messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, reply_markup=None) -> None:  # noqa: ANN001
        self.sent_messages.append((chat_id, text))

    async def send_document(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        return


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.init_schema(c)
    return c


@pytest.fixture
def settings() -> Settings:
    return Settings(
        bot_token="dummy",
        admin_ids=(101, 102),
        canteen_chat_id=None,
        tz="Asia/Omsk",
        menu_broadcast_time="08:30",
        order_deadline_time="11:00",
        order_reminder_before_deadline_minutes=20,
        order_draft_cart_reminder_before_deadline_minutes=10,
        imap_host="imap.local",
        imap_port=993,
        imap_user="user",
        imap_password="pass",
        imap_sender_filter=None,
        imap_only_unseen=False,
        imap_urgent_after="00:00",
        test_mode=True,
        db_path=None,  # type: ignore[arg-type]
    )


def _link_employee(conn: sqlite3.Connection, user_id: int, last: str = "User", first: str = "Test") -> None:
    eid = db.add_employee(conn, last, first)
    db.link_employee_telegram(conn, eid, user_id, None)


@pytest.mark.asyncio
async def test_order_button_shows_no_menu_message_when_absent(conn: sqlite3.Connection, settings: Settings) -> None:
    _link_employee(conn, 777)
    msg = _FakeMessage(user_id=777)

    await _open_menu(msg, conn, settings)

    assert msg.answers
    assert "Меню на сегодня ещё не загружено." in msg.answers[-1]


@pytest.mark.asyncio
async def test_imap_poll_without_new_menu_does_nothing(conn: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = _FakeBot()
    monkeypatch.setattr(jobs, "fetch_latest_docx_attachments", lambda *a, **k: [])
    monkeypatch.setattr(jobs, "_imap_in_quiet_period", lambda s: False)
    monkeypatch.setattr(jobs, "_imap_poll_is_urgent", lambda c, s: True)

    await jobs.process_imap_scheduled(bot, conn, settings, urgent_only=True)

    today = local_today(settings.tz)
    assert db.get_menu_for_date(conn, today) is None
    assert bot.sent_messages == []


@pytest.mark.asyncio
async def test_new_menu_triggers_mass_notification_when_enabled(
    conn: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _link_employee(conn, 201, "A", "A")
    _link_employee(conn, 202, "B", "B")
    bot = _FakeBot()

    att = MailAttachment(message_id="<m1>", filename="menu.docx", data=b"docx")
    monkeypatch.setattr(jobs, "fetch_latest_docx_attachments", lambda *a, **k: [att])
    monkeypatch.setattr(jobs, "parse_docx_bytes", lambda data: [("Борщ", 120.0, "main")])
    monkeypatch.setattr(jobs, "_imap_in_quiet_period", lambda s: False)
    monkeypatch.setattr(jobs, "_imap_poll_is_urgent", lambda c, s: True)

    await jobs.process_imap_scheduled(bot, conn, settings, urgent_only=True)

    texts = [t for _, t in bot.sent_messages]
    assert any("Новое меню на сегодня загружено." in t for t in texts)


@pytest.mark.asyncio
async def test_new_menu_notification_respects_admin_toggle(
    conn: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _link_employee(conn, 301)
    bot = _FakeBot()
    db.set_app_setting_bool(conn, jobs.MENU_NOTIFY_SETTING_KEY, False)

    att = MailAttachment(message_id="<m2>", filename="menu.docx", data=b"docx")
    monkeypatch.setattr(jobs, "fetch_latest_docx_attachments", lambda *a, **k: [att])
    monkeypatch.setattr(jobs, "parse_docx_bytes", lambda data: [("Плов", 180.0, "main")])
    monkeypatch.setattr(jobs, "_imap_in_quiet_period", lambda s: False)
    monkeypatch.setattr(jobs, "_imap_poll_is_urgent", lambda c, s: True)

    await jobs.process_imap_scheduled(bot, conn, settings, urgent_only=True)

    assert bot.sent_messages == []


def test_menu_pagination_first_page_has_next_only() -> None:
    items = [
        db.MenuItemRow(id=i + 1, menu_id=1, sort_order=i, dish_name=f"Dish {i+1}", price=10.0 + i, dish_kind="other")
        for i in range(21)
    ]
    kb = _menu_kb(items, cart={}, page=0)
    nav_texts = [btn.text for btn in kb.inline_keyboard[-2]]
    assert "▶️" in nav_texts
    assert "◀️" not in nav_texts
    assert "1/3" in nav_texts


def test_menu_pagination_middle_page_has_both_arrows() -> None:
    items = [
        db.MenuItemRow(id=i + 1, menu_id=1, sort_order=i, dish_name=f"Dish {i+1}", price=10.0 + i, dish_kind="other")
        for i in range(25)
    ]
    kb = _menu_kb(items, cart={}, page=1)
    nav_texts = [btn.text for btn in kb.inline_keyboard[-2]]
    assert "◀️" in nav_texts
    assert "▶️" in nav_texts
    assert "2/3" in nav_texts


def test_menu_pagination_clamps_out_of_range_page() -> None:
    items = [
        db.MenuItemRow(id=i + 1, menu_id=1, sort_order=i, dish_name=f"Dish {i+1}", price=10.0 + i, dish_kind="other")
        for i in range(11)
    ]
    kb = _menu_kb(items, cart={}, page=99)
    nav_texts = [btn.text for btn in kb.inline_keyboard[-2]]
    assert "2/2" in nav_texts
