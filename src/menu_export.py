from __future__ import annotations

from .db import MenuItemRow
from .menu_parse import sanitize_dish_name


def _one_line(s: str) -> str:
    return " ".join(s.split())


def build_menu_txt_bytes(items: list[MenuItemRow]) -> bytes:
    lines = [
        f"{_one_line(sanitize_dish_name(it.dish_name))} — {it.price:.2f} ₽"
        for it in items
    ]
    return "\r\n".join(lines).encode("utf-8")
