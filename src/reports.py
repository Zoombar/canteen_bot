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
    garn = [x for x in lines if x.dish_kind == "garnish"]
    mains = [x for x in lines if x.dish_kind == "main"]
    G = sum(x.quantity for x in garn)
    M = sum(x.quantity for x in mains)
    P = min(G, M)
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
            t = min(have, left)
            rem[x.menu_item_id] -= t
            left -= t
        return left

    _ = consume(garn, P)
    _ = consume(mains, P)
    return P, rem


def aggregate_daily_canteen(
    conn: sqlite3.Connection, order_date: date
) -> tuple[int, list[tuple[str, int]]]:
    """
    Returns (total_pairs, block2 list of (dish_name, qty)).
    block2 uses remaining quantities after intra-order pairing for garnish/main;
    'other' dishes always fully included.
    """
    by_order: dict[int, list[OrderLine]] = defaultdict(list)
    order_ids: list[int] = []
    seen_oid: set[int] = set()
    # regroup - need order id
    rows2 = conn.execute(
        """
        SELECT o.id AS oid, oi.menu_item_id, mi.dish_name, mi.dish_kind, oi.quantity
        FROM orders o
        JOIN order_items oi ON oi.order_id = o.id
        JOIN menu_items mi ON mi.id = oi.menu_item_id
        WHERE o.order_date = ? AND o.status = 'confirmed'
        ORDER BY o.id, mi.sort_order, mi.id
        """,
        (order_date.isoformat(),),
    ).fetchall()
    for r in rows2:
        oid = int(r["oid"])
        kind = r["dish_kind"]
        if kind not in ("garnish", "main", "other"):
            kind = "other"
        by_order[oid].append(
            OrderLine(
                menu_item_id=int(r["menu_item_id"]),
                dish_name=r["dish_name"],
                dish_kind=kind,  # type: ignore[arg-type]
                quantity=int(r["quantity"]),
            )
        )
        if oid not in seen_oid:
            seen_oid.add(oid)
            order_ids.append(oid)

    total_pairs = 0
    name_qty: defaultdict[str, int] = defaultdict(int)

    for oid in order_ids:
        lines = by_order.get(oid, [])
        if not lines:
            continue
        p, rem = allocate_pairs_for_order(lines)
        total_pairs += p
        for mid, q in rem.items():
            if q <= 0:
                continue
            # map id to name
            name = next((x.dish_name for x in lines if x.menu_item_id == mid), str(mid))
            name_qty[name] += q

    block2 = sorted(name_qty.items(), key=lambda x: (-x[1], x[0]))
    return total_pairs, block2


def format_canteen_text(total_pairs: int, block2: list[tuple[str, int]]) -> str:
    lines = [
        "Сводка заказов (столовая)",
        "",
        "Блок 1 — пары (гарнир + основное):",
        f"Всего пар: {total_pairs}",
        "",
        "Блок 2 — позиции (остаток после учёта пар внутри заказа сотрудника):",
    ]
    if not block2:
        lines.append("(нет позиций)")
    else:
        for name, q in block2:
            lines.append(f"- {name}: {q}")
    return "\n".join(lines)


def build_canteen_excel_bytes(total_pairs: int, block2: list[tuple[str, int]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Сводка"
    ws.append(["Блок", "Позиция", "Количество"])
    ws.append(["1 (пары)", "Гарнир + основное (пары)", total_pairs])
    for name, q in block2:
        ws.append(["2", name, q])
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


def build_canteen_csv_bytes(total_pairs: int, block2: list[tuple[str, int]]) -> bytes:
    import csv

    bio = io.StringIO()
    w = csv.writer(bio, delimiter=";")
    w.writerow(["block", "name", "qty"])
    w.writerow([1, "pair_garnish_main", total_pairs])
    for name, q in block2:
        w.writerow([2, name, q])
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
