from __future__ import annotations

import io
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

import sqlite3
from openpyxl import Workbook

from .db import DishKind

CONTAINER_PRICE_RUB = 12.0
_FIRST_COURSE_RE = re.compile(
    r"суп|борщ|щи|уха|солянк|лапш|рассольник|харчо|шурп|окрошк|крем[- ]?суп|первое",
    re.IGNORECASE,
)


@dataclass
class OrderLine:
    menu_item_id: int
    dish_name: str
    dish_kind: DishKind
    quantity: int


def allocate_pairs_for_order(lines: list[OrderLine]) -> tuple[int, dict[int, int]]:
    """Сохранено для совместимости тестов/старых вызовов."""
    pair_labels, rem = _allocate_pair_labels_for_order(lines)
    pairs = sum(pair_labels.values())
    return pairs, rem


def is_first_course_name(dish_name: str) -> bool:
    return bool(_FIRST_COURSE_RE.search((dish_name or "").strip()))


def count_containers_for_order(lines: list[OrderLine]) -> int:
    main_qty = sum(x.quantity for x in lines if x.dish_kind == "main")
    first_qty = sum(x.quantity for x in lines if is_first_course_name(x.dish_name))
    return max(0, main_qty + first_qty)


def _allocate_pair_labels_for_order(lines: list[OrderLine]) -> tuple[dict[str, int], dict[int, int]]:
    rem: dict[int, int] = defaultdict(int)
    for x in lines:
        rem[x.menu_item_id] += x.quantity

    garn = [x for x in lines if x.dish_kind == "garnish" and rem[x.menu_item_id] > 0]
    mains = [x for x in lines if x.dish_kind == "main" and rem[x.menu_item_id] > 0]
    gi = 0
    mi = 0
    pair_labels: dict[str, int] = defaultdict(int)
    while gi < len(garn) and mi < len(mains):
        g = garn[gi]
        m = mains[mi]
        g_have = rem[g.menu_item_id]
        m_have = rem[m.menu_item_id]
        if g_have <= 0:
            gi += 1
            continue
        if m_have <= 0:
            mi += 1
            continue
        take = min(g_have, m_have)
        rem[g.menu_item_id] -= take
        rem[m.menu_item_id] -= take
        pair_labels[f"{m.dish_name} + {g.dish_name}"] += take
    return dict(pair_labels), rem


def aggregate_daily_canteen(
    conn: sqlite3.Connection, order_date: date
) -> list[tuple[str, int]]:
    """Сводка по подтверждённым заказам за день: пары второе+гарнир, остатки и контейнеры."""
    rows = conn.execute(
        """
        SELECT o.id AS order_id,
               mi.id AS menu_item_id,
               mi.dish_name AS dish_name,
               mi.dish_kind AS dish_kind,
               oi.quantity AS qty
        FROM orders o
        JOIN order_items oi ON oi.order_id = o.id
        JOIN menu_items mi ON mi.id = oi.menu_item_id
        WHERE o.order_date = ? AND o.status = 'confirmed'
        ORDER BY o.id, mi.sort_order, mi.id
        """,
        (order_date.isoformat(),),
    ).fetchall()
    if not rows:
        return []

    pairs_agg: dict[str, int] = defaultdict(int)
    leftovers_agg: dict[str, int] = defaultdict(int)
    containers = 0

    current_order_id: int | None = None
    order_lines: list[OrderLine] = []

    def flush_order() -> None:
        nonlocal containers
        if not order_lines:
            return
        pair_labels, rem = _allocate_pair_labels_for_order(order_lines)
        for label, qty in pair_labels.items():
            pairs_agg[label] += qty
        for x in order_lines:
            left = rem.get(x.menu_item_id, 0)
            if left > 0:
                leftovers_agg[x.dish_name] += left
        containers += count_containers_for_order(order_lines)

    for r in rows:
        oid = int(r["order_id"])
        if current_order_id is None:
            current_order_id = oid
        elif oid != current_order_id:
            flush_order()
            order_lines = []
            current_order_id = oid
        order_lines.append(
            OrderLine(
                menu_item_id=int(r["menu_item_id"]),
                dish_name=str(r["dish_name"]),
                dish_kind=str(r["dish_kind"]) if str(r["dish_kind"]) in ("garnish", "main", "other") else "other",  # type: ignore[arg-type]
                quantity=int(r["qty"]),
            )
        )
    flush_order()

    out: list[tuple[str, int]] = []
    for name, q in sorted(pairs_agg.items(), key=lambda x: (-x[1], x[0])):
        out.append((name, q))
    for name, q in sorted(leftovers_agg.items(), key=lambda x: (-x[1], x[0])):
        out.append((name, q))
    if containers > 0:
        out.append(("Контейнеры (12 руб)", containers))
    return out


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
    """Return rows (display_name, total_rub) for confirmed orders in month (с контейнерами)."""
    start = date(year, month, 1)
    if month == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month + 1, 1)
    rows = conn.execute(
        """
        SELECT e.id AS employee_id,
               e.last_name,
               e.first_name,
               e.position,
               o.id AS order_id,
               mi.id AS menu_item_id,
               mi.dish_name AS dish_name,
               mi.dish_kind AS dish_kind,
               oi.quantity AS qty,
               mi.price AS price
        FROM orders o
        JOIN employees e ON e.id = o.employee_id
        JOIN order_items oi ON oi.order_id = o.id
        JOIN menu_items mi ON mi.id = oi.menu_item_id
        WHERE o.status = 'confirmed'
          AND o.order_date >= ? AND o.order_date < ?
        ORDER BY e.id, o.id, mi.sort_order, mi.id
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    if not rows:
        return []

    out_map: dict[int, tuple[str, float]] = {}
    current_emp_id: int | None = None
    current_order_id: int | None = None
    order_lines: list[OrderLine] = []
    order_food_total = 0.0
    emp_total = 0.0
    emp_name = ""

    def flush_order() -> None:
        nonlocal emp_total, order_food_total
        if not order_lines:
            return
        containers = count_containers_for_order(order_lines)
        emp_total += order_food_total + containers * CONTAINER_PRICE_RUB
        order_lines.clear()
        order_food_total = 0.0

    def flush_employee() -> None:
        nonlocal emp_total
        flush_order()
        if current_emp_id is None:
            return
        out_map[current_emp_id] = (emp_name, emp_total)
        emp_total = 0.0

    for r in rows:
        eid = int(r["employee_id"])
        oid = int(r["order_id"])
        if current_emp_id is None:
            current_emp_id = eid
        elif eid != current_emp_id:
            flush_employee()
            current_emp_id = eid
            current_order_id = None
        if current_order_id is None:
            current_order_id = oid
        elif oid != current_order_id:
            flush_order()
            current_order_id = oid

        base = f"{r['last_name']} {r['first_name']}"
        pos = (r["position"] or "").strip()
        emp_name = f"{base} ({pos})" if pos else base

        qty = int(r["qty"])
        order_food_total += qty * float(r["price"] or 0.0)
        dish_kind_raw = str(r["dish_kind"])
        dish_kind: DishKind = dish_kind_raw if dish_kind_raw in ("garnish", "main", "other") else "other"
        order_lines.append(
            OrderLine(
                menu_item_id=int(r["menu_item_id"]),
                dish_name=str(r["dish_name"]),
                dish_kind=dish_kind,
                quantity=qty,
            )
        )

    flush_employee()
    return [out_map[k] for k in sorted(out_map.keys(), key=lambda eid: out_map[eid][0])]


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
