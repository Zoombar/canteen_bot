from __future__ import annotations

from .db import MenuItemRow


def build_menu_txt_bytes(items: list[MenuItemRow]) -> bytes:
    lines = [f"{it.dish_name}\t{it.price:.2f}" for it in items]
    return ("\n".join(lines)).encode("utf-8")
