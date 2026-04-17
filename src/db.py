from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Generator, Iterable, Literal

DishKind = Literal["garnish", "main", "other"]
OrderStatus = Literal["draft", "confirmed"]


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;

        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position TEXT NOT NULL,
            last_name TEXT NOT NULL,
            first_name TEXT NOT NULL,
            telegram_user_id INTEGER UNIQUE,
            telegram_username TEXT,
            active INTEGER NOT NULL DEFAULT 1
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_employees_name
            ON employees (last_name, first_name);

        CREATE TABLE IF NOT EXISTS menus (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            menu_date TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS menu_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            menu_id INTEGER NOT NULL REFERENCES menus(id) ON DELETE CASCADE,
            sort_order INTEGER NOT NULL DEFAULT 0,
            dish_name TEXT NOT NULL,
            price REAL NOT NULL,
            dish_kind TEXT NOT NULL DEFAULT 'other',
            UNIQUE(menu_id, dish_name)
        );

        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
            order_date TEXT NOT NULL,
            status TEXT NOT NULL,
            confirmed_at TEXT,
            UNIQUE(employee_id, order_date)
        );

        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
            menu_item_id INTEGER NOT NULL REFERENCES menu_items(id) ON DELETE CASCADE,
            quantity INTEGER NOT NULL CHECK (quantity > 0),
            UNIQUE(order_id, menu_item_id)
        );

        CREATE TABLE IF NOT EXISTS processed_emails (
            message_id TEXT PRIMARY KEY,
            processed_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS menu_broadcasts (
            menu_date TEXT PRIMARY KEY,
            sent_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS monthly_reports_sent (
            year_month TEXT PRIMARY KEY,
            sent_at TEXT NOT NULL
        );
        """
    )
    cols = {
        str(r["name"])
        for r in conn.execute("PRAGMA table_info(employees)").fetchall()
    }
    if "telegram_username" not in cols:
        conn.execute("ALTER TABLE employees ADD COLUMN telegram_username TEXT")
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Generator[sqlite3.Connection, None, None]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


@dataclass
class EmployeeRow:
    id: int
    position: str
    last_name: str
    first_name: str
    telegram_user_id: int | None
    telegram_username: str | None
    active: bool


@dataclass
class MenuItemRow:
    id: int
    menu_id: int
    sort_order: int
    dish_name: str
    price: float
    dish_kind: DishKind


def add_employee(
    conn: sqlite3.Connection,
    last_name: str,
    first_name: str,
    position: str = "",
) -> int:
    with transaction(conn):
        cur = conn.execute(
            """
            INSERT INTO employees (position, last_name, first_name)
            VALUES (?, ?, ?)
            """,
            (position.strip(), last_name.strip(), first_name.strip()),
        )
        return int(cur.lastrowid)


def list_employees(conn: sqlite3.Connection, active_only: bool = True) -> list[EmployeeRow]:
    q = (
        "SELECT id, position, last_name, first_name, telegram_user_id, telegram_username, active "
        "FROM employees"
    )
    if active_only:
        q += " WHERE active = 1"
    q += " ORDER BY last_name, first_name"
    rows = conn.execute(q).fetchall()
    return [
        EmployeeRow(
            id=r["id"],
            position=r["position"],
            last_name=r["last_name"],
            first_name=r["first_name"],
            telegram_user_id=r["telegram_user_id"],
            telegram_username=r["telegram_username"],
            active=bool(r["active"]),
        )
        for r in rows
    ]


def find_employee_by_name_admin(
    conn: sqlite3.Connection, last_name: str, first_name: str
) -> EmployeeRow | None:
    row = conn.execute(
        """
        SELECT id, position, last_name, first_name, telegram_user_id, telegram_username, active
        FROM employees
        WHERE last_name = ? AND first_name = ?
        """,
        (last_name.strip(), first_name.strip()),
    ).fetchone()
    if not row:
        return None
    return EmployeeRow(
        id=row["id"],
        position=row["position"],
        last_name=row["last_name"],
        first_name=row["first_name"],
        telegram_user_id=row["telegram_user_id"],
        telegram_username=row["telegram_username"],
        active=bool(row["active"]),
    )


def find_employee_by_name(conn: sqlite3.Connection, last_name: str, first_name: str) -> EmployeeRow | None:
    row = conn.execute(
        """
        SELECT id, position, last_name, first_name, telegram_user_id, telegram_username, active
        FROM employees
        WHERE last_name = ? AND first_name = ? AND active = 1
        """,
        (last_name.strip(), first_name.strip()),
    ).fetchone()
    if not row:
        return None
    return EmployeeRow(
        id=row["id"],
        position=row["position"],
        last_name=row["last_name"],
        first_name=row["first_name"],
        telegram_user_id=row["telegram_user_id"],
        telegram_username=row["telegram_username"],
        active=bool(row["active"]),
    )


def get_employee_by_tg(conn: sqlite3.Connection, telegram_user_id: int) -> EmployeeRow | None:
    row = conn.execute(
        """
        SELECT id, position, last_name, first_name, telegram_user_id, telegram_username, active
        FROM employees
        WHERE telegram_user_id = ? AND active = 1
        """,
        (telegram_user_id,),
    ).fetchone()
    if not row:
        return None
    return EmployeeRow(
        id=row["id"],
        position=row["position"],
        last_name=row["last_name"],
        first_name=row["first_name"],
        telegram_user_id=row["telegram_user_id"],
        telegram_username=row["telegram_username"],
        active=bool(row["active"]),
    )


def link_employee_telegram(
    conn: sqlite3.Connection,
    employee_id: int,
    telegram_user_id: int,
    telegram_username: str | None = None,
) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE employees SET telegram_user_id = ?, telegram_username = ? WHERE id = ?",
            (telegram_user_id, telegram_username, employee_id),
        )


def unlink_employee_telegram(conn: sqlite3.Connection, employee_id: int) -> None:
    with transaction(conn):
        conn.execute(
            "UPDATE employees SET telegram_user_id = NULL, telegram_username = NULL WHERE id = ?",
            (employee_id,),
        )


def deactivate_employee(conn: sqlite3.Connection, employee_id: int) -> None:
    with transaction(conn):
        conn.execute("UPDATE employees SET active = 0 WHERE id = ?", (employee_id,))


def activate_employee(conn: sqlite3.Connection, employee_id: int) -> None:
    with transaction(conn):
        conn.execute("UPDATE employees SET active = 1 WHERE id = ?", (employee_id,))


def delete_employee(conn: sqlite3.Connection, employee_id: int) -> None:
    with transaction(conn):
        conn.execute("DELETE FROM employees WHERE id = ?", (employee_id,))


def get_menu_for_date(conn: sqlite3.Connection, d: date) -> int | None:
    row = conn.execute("SELECT id FROM menus WHERE menu_date = ?", (d.isoformat(),)).fetchone()
    return int(row["id"]) if row else None


def _disambiguate_menu_item_names(
    items: list[tuple[str, float, DishKind]],
) -> list[tuple[str, float, DishKind]]:
    """UNIQUE(menu_id, dish_name): при одинаковом названии разных цен добавляем цену в имя."""
    counts: dict[str, int] = {}
    out: list[tuple[str, float, DishKind]] = []
    for name, price, kind in items:
        key = name.strip().casefold()
        n = counts.get(key, 0) + 1
        counts[key] = n
        if n == 1:
            out.append((name, price, kind))
        else:
            out.append((f"{name} ({price:.2f} ₽)", price, kind))
    return out


def create_menu(
    conn: sqlite3.Connection,
    d: date,
    source: str,
    items: Iterable[tuple[str, float, DishKind]],
) -> int:
    items_list = _disambiguate_menu_item_names(list(items))
    with transaction(conn):
        conn.execute("DELETE FROM menus WHERE menu_date = ?", (d.isoformat(),))
        cur = conn.execute(
            """
            INSERT INTO menus (menu_date, source, created_at)
            VALUES (?, ?, ?)
            """,
            (d.isoformat(), source, datetime.utcnow().isoformat(timespec="seconds") + "Z"),
        )
        menu_id = int(cur.lastrowid)
        for i, (name, price, kind) in enumerate(items_list):
            conn.execute(
                """
                INSERT INTO menu_items (menu_id, sort_order, dish_name, price, dish_kind)
                VALUES (?, ?, ?, ?, ?)
                """,
                (menu_id, i, name, float(price), kind),
            )
        return menu_id


def list_menu_items(conn: sqlite3.Connection, menu_id: int) -> list[MenuItemRow]:
    rows = conn.execute(
        """
        SELECT id, menu_id, sort_order, dish_name, price, dish_kind
        FROM menu_items
        WHERE menu_id = ?
        ORDER BY sort_order, id
        """,
        (menu_id,),
    ).fetchall()
    out: list[MenuItemRow] = []
    for r in rows:
        kind = r["dish_kind"]
        if kind not in ("garnish", "main", "other"):
            kind = "other"
        out.append(
            MenuItemRow(
                id=r["id"],
                menu_id=r["menu_id"],
                sort_order=r["sort_order"],
                dish_name=r["dish_name"],
                price=float(r["price"]),
                dish_kind=kind,  # type: ignore[arg-type]
            )
        )
    return out


def get_menu_item(conn: sqlite3.Connection, menu_item_id: int) -> MenuItemRow | None:
    row = conn.execute(
        """
        SELECT id, menu_id, sort_order, dish_name, price, dish_kind
        FROM menu_items WHERE id = ?
        """,
        (menu_item_id,),
    ).fetchone()
    if not row:
        return None
    kind = row["dish_kind"]
    if kind not in ("garnish", "main", "other"):
        kind = "other"
    return MenuItemRow(
        id=row["id"],
        menu_id=row["menu_id"],
        sort_order=row["sort_order"],
        dish_name=row["dish_name"],
        price=float(row["price"]),
        dish_kind=kind,  # type: ignore[arg-type]
    )


def get_or_create_draft_order(conn: sqlite3.Connection, employee_id: int, order_date: date) -> int:
    row = conn.execute(
        """
        SELECT id, status FROM orders
        WHERE employee_id = ? AND order_date = ?
        """,
        (employee_id, order_date.isoformat()),
    ).fetchone()
    if row:
        oid = int(row["id"])
        if row["status"] == "confirmed":
            return oid
        return oid
    with transaction(conn):
        cur = conn.execute(
            """
            INSERT INTO orders (employee_id, order_date, status)
            VALUES (?, ?, 'draft')
            """,
            (employee_id, order_date.isoformat()),
        )
        return int(cur.lastrowid)


def get_order_for_employee_date(
    conn: sqlite3.Connection, employee_id: int, order_date: date
) -> tuple[int, OrderStatus] | None:
    row = conn.execute(
        """
        SELECT id, status FROM orders
        WHERE employee_id = ? AND order_date = ?
        """,
        (employee_id, order_date.isoformat()),
    ).fetchone()
    if not row:
        return None
    st: OrderStatus = "confirmed" if row["status"] == "confirmed" else "draft"
    return int(row["id"]), st


def set_order_items(
    conn: sqlite3.Connection,
    order_id: int,
    lines: list[tuple[int, int]],  # menu_item_id, qty
) -> None:
    with transaction(conn):
        conn.execute("DELETE FROM order_items WHERE order_id = ?", (order_id,))
        for mid, qty in lines:
            if qty <= 0:
                continue
            conn.execute(
                """
                INSERT INTO order_items (order_id, menu_item_id, quantity)
                VALUES (?, ?, ?)
                """,
                (order_id, mid, qty),
            )


def list_order_items_with_menu(
    conn: sqlite3.Connection, order_id: int
) -> list[tuple[MenuItemRow, int]]:
    rows = conn.execute(
        """
        SELECT mi.id, mi.menu_id, mi.sort_order, mi.dish_name, mi.price, mi.dish_kind,
               oi.quantity
        FROM order_items oi
        JOIN menu_items mi ON mi.id = oi.menu_item_id
        WHERE oi.order_id = ?
        ORDER BY mi.sort_order, mi.id
        """,
        (order_id,),
    ).fetchall()
    out: list[tuple[MenuItemRow, int]] = []
    for r in rows:
        kind = r["dish_kind"]
        if kind not in ("garnish", "main", "other"):
            kind = "other"
        m = MenuItemRow(
            id=r["id"],
            menu_id=r["menu_id"],
            sort_order=r["sort_order"],
            dish_name=r["dish_name"],
            price=float(r["price"]),
            dish_kind=kind,  # type: ignore[arg-type]
        )
        out.append((m, int(r["quantity"])))
    return out


def confirm_order(conn: sqlite3.Connection, order_id: int) -> None:
    with transaction(conn):
        conn.execute(
            """
            UPDATE orders
            SET status = 'confirmed',
                confirmed_at = ?
            WHERE id = ?
            """,
            (datetime.utcnow().isoformat(timespec="seconds") + "Z", order_id),
        )


def count_distinct_dishes_in_order(conn: sqlite3.Connection, order_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM order_items WHERE order_id = ?",
        (order_id,),
    ).fetchone()
    return int(row["c"]) if row else 0


def is_email_processed(conn: sqlite3.Connection, message_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM processed_emails WHERE message_id = ?",
        (message_id,),
    ).fetchone()
    return row is not None


def mark_email_processed(conn: sqlite3.Connection, message_id: str) -> None:
    with transaction(conn):
        conn.execute(
            """
            INSERT OR IGNORE INTO processed_emails (message_id, processed_at)
            VALUES (?, ?)
            """,
            (message_id, datetime.utcnow().isoformat(timespec="seconds") + "Z"),
        )


def was_menu_broadcast(conn: sqlite3.Connection, d: date) -> bool:
    row = conn.execute(
        "SELECT 1 FROM menu_broadcasts WHERE menu_date = ?",
        (d.isoformat(),),
    ).fetchone()
    return row is not None


def mark_menu_broadcast(conn: sqlite3.Connection, d: date) -> None:
    with transaction(conn):
        conn.execute(
            """
            INSERT OR REPLACE INTO menu_broadcasts (menu_date, sent_at)
            VALUES (?, ?)
            """,
            (d.isoformat(), datetime.utcnow().isoformat(timespec="seconds") + "Z"),
        )


def was_monthly_report_sent(conn: sqlite3.Connection, year_month: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM monthly_reports_sent WHERE year_month = ?",
        (year_month,),
    ).fetchone()
    return row is not None


def mark_monthly_report_sent(conn: sqlite3.Connection, year_month: str) -> None:
    with transaction(conn):
        conn.execute(
            """
            INSERT OR REPLACE INTO monthly_reports_sent (year_month, sent_at)
            VALUES (?, ?)
            """,
            (year_month, datetime.utcnow().isoformat(timespec="seconds") + "Z"),
        )
