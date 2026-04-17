from __future__ import annotations

import io
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

import sqlite3
from openpyxl import Workbook

from .db import DishKind


@dataclass
class OrderLine:
    menu_item_id: int
    dish_name: str
    dish_kind: DishKind
    quantity: int


def allocate_pairs_for_order(lines: list[OrderLine]) -> tuple[int, dict[int, int]]:
    """Сохранено для совместимости тестов/старых вызовов."""
    garn = [x for x in lines if x.dish_kind == "garnish"]
    mains = [x for x in lines if x.dish_kind == "main"]
    g_total = sum(x.quantity for x in garn)
    m_total = sum(x.quantity for x in mains)
    pairs = min(g_total, m_total)
    rem: dict[int, int] = defaultdict(int)
    for x in lines:
        rem[x.menu_item_id] += x.quantity

    def consume(kind_lines: list[OrderLine], need: int) -> int:
        left = need
        for x in kind_lines:
            if left <= 0:
                break
            have = rem[x.menu_item_id]
            if have <= 0:
                continue
            take = min(have, left)
            rem[x.menu_item_id] -= take
            left -= take
        return left

    _ = consume(garn, pairs)
    _ = consume(mains, pairs)
    return pairs, rem


def aggregate_daily_canteen(
    conn: sqlite3.Connection, order_date: date
) -> list[tuple[str, int]]:
    """Простая сводка: только (название блюда, количество) по подтверждённым заказам за день."""
    rows = conn.execute(
        """
        SELECT mi.dish_name AS dish_name, SUM(oi.quantity) AS qty
        FROM orders o
        JOIN order_items oi ON oi.order_id = o.id
        JOIN menu_items mi ON mi.id = oi.menu_item_id
        WHERE o.order_date = ? AND o.status = 'confirmed'
        GROUP BY mi.id, mi.dish_name
        ORDER BY qty DESC, mi.dish_name
        """,
        (order_date.isoformat(),),
    ).fetchall()
    return [(str(r["dish_name"]), int(r["qty"])) for r in rows]


def format_canteen_text(items: list[tuple[str, int]]) -> str:
    lines = [
        "Сводка заказов (столовая)",
        "",
        "Позиции:",
    ]
    if not items:
        lines.append("(нет позиций)")
    else:
        for name, q in items:
            lines.append(f"- {name}: {q}")
    return "\n".join(lines)


def build_canteen_excel_bytes(items: list[tuple[str, int]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Сводка"
    ws.append(["Позиция", "Количество"])
    for name, q in items:
        ws.append([name, q])
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


def build_canteen_csv_bytes(items: list[tuple[str, int]]) -> bytes:
    import csv

    bio = io.StringIO()
    w = csv.writer(bio, delimiter=";")
    w.writerow(["name", "qty"])
    for name, q in items:
        w.writerow([name, q])
    return bio.getvalue().encode("utf-8-sig")


def monthly_totals_by_employee(conn: sqlite3.Connection, year: int, month: int) -> list[tuple[str, float]]:
    """Return rows (display_name, total_rub) for confirmed orders in month."""
    start = date(year, month, 1)
    if month == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month + 1, 1)
    rows = conn.execute(
        """
        SELECT e.last_name, e.first_name, e.position,
               SUM(oi.quantity * mi.price) AS total
        FROM orders o
        JOIN employees e ON e.id = o.employee_id
        JOIN order_items oi ON oi.order_id = o.id
        JOIN menu_items mi ON mi.id = oi.menu_item_id
        WHERE o.status = 'confirmed'
          AND o.order_date >= ? AND o.order_date < ?
        GROUP BY e.id
        ORDER BY e.last_name, e.first_name
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    out: list[tuple[str, float]] = []
    for r in rows:
        base = f"{r['last_name']} {r['first_name']}"
        pos = (r["position"] or "").strip()
        name = f"{base} ({pos})" if pos else base
        out.append((name, float(r["total"] or 0)))
    return out


def build_monthly_xlsx(rows: list[tuple[str, float]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Итоги"
    ws.append(["Сотрудник", "Сумма, руб"])
    for name, total in rows:
        ws.append([name, round(total, 2)])
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()
