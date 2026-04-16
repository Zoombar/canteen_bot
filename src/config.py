from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _get_bool(key: str, default: bool = False) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _parse_admin_ids(raw: str | None) -> list[int]:
    if not raw:
        return []
    out: list[int] = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        out.append(int(part))
    return out


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_ids: tuple[int, ...]
    canteen_chat_id: int | None
    tz: str
    menu_broadcast_time: str  # HH:MM
    order_deadline_time: str  # HH:MM
    imap_host: str | None
    imap_port: int
    imap_user: str | None
    imap_password: str | None
    imap_sender_filter: str | None
    imap_only_unseen: bool
    test_mode: bool  # заказы в выходные + без дедлайна + команды /test_* для админов
    db_path: Path


def load_settings() -> Settings:
    base = Path(__file__).resolve().parent.parent
    db_path = base / "data" / "bot.db"
    return Settings(
        bot_token=os.getenv("BOT_TOKEN", "").strip(),
        admin_ids=tuple(_parse_admin_ids(os.getenv("ADMIN_IDS"))),
        canteen_chat_id=(
            int(_cc)
            if (_cc := (os.getenv("CANTEEN_CHAT_ID") or "").strip())
            else None
        ),
        # Таймзона всегда Омск, независимо от того, где крутится сервер.
        tz="Asia/Omsk",
        menu_broadcast_time=os.getenv("MENU_BROADCAST_TIME", "08:30").strip() or "08:30",
        order_deadline_time=os.getenv("ORDER_DEADLINE_TIME", "11:00").strip() or "11:00",
        imap_host=(os.getenv("IMAP_HOST") or "").strip() or None,
        imap_port=int(os.getenv("IMAP_PORT", "993") or "993"),
        imap_user=(os.getenv("IMAP_USER") or "").strip() or None,
        imap_password=(os.getenv("IMAP_PASSWORD") or "").strip() or None,
        imap_sender_filter=(os.getenv("IMAP_SENDER_FILTER") or "").strip() or None,
        imap_only_unseen=_get_bool("IMAP_ONLY_UNSEEN", True),
        test_mode=_get_bool("TEST_MODE", False),
        db_path=db_path,
    )
