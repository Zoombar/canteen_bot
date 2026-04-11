from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from docx import Document

from .db import DishKind

# Heuristic: Russian menu keywords
_GARNISH_RE = re.compile(
    r"гарнир|каш[ае]|гречк|рис\b|макарон|пюре|картоф|овощ|капуст|фасол|горох|перлов",
    re.IGNORECASE,
)
_MAIN_RE = re.compile(
    r"котлет|отбивн|тефтел|биток|мяс|рыб|курин|индейк|печен|печён|филе|"
    r"гуляш|подлив|жаркое|бефстр|стейк|свин|говя|баран|фрикадел|рулет",
    re.IGNORECASE,
)


def classify_dish(name: str) -> DishKind:
    n = name.strip()
    g = bool(_GARNISH_RE.search(n))
    m = bool(_MAIN_RE.search(n))
    if g and not m:
        return "garnish"
    if m and not g:
        return "main"
    if g and m:
        # ambiguous: prefer main (common in lines like "Куриное филе с рисом")
        return "main"
    return "other"


_PRICE_RE = re.compile(
    r"""
    (?P<name>.+?)
    [\s\u00a0]*                           # spaces
    (?P<price>\d+(?:[.,]\d{1,2})?)       # price
    \s*(?:руб|р\.?)?\s*$                 # optional rub
    """,
    re.VERBOSE | re.IGNORECASE,
)


def _parse_price_token(token: str) -> float | None:
    s = (token or "").strip()
    if not s:
        return None
    s = s.replace("\u00a0", " ")
    s = re.sub(r"\s+", "", s)
    s = s.rstrip("рубРУБ.р")
    # Common DOCX-table format: 50-00, 100-0
    m_dash = re.fullmatch(r"(\d+)-(\d{1,2})", s)
    if m_dash:
        rub = m_dash.group(1)
        kop = m_dash.group(2).ljust(2, "0")
        s = f"{rub}.{kop}"
    else:
        s = s.replace(",", ".")
    if not re.fullmatch(r"\d+(?:\.\d{1,2})?", s):
        return None
    try:
        price = float(s)
    except ValueError:
        return None
    if price <= 0 or price > 100000:
        return None
    return price


def _parse_line(line: str) -> tuple[str, float] | None:
    s = line.strip()
    if not s or len(s) < 2:
        return None
    # Normalize long dashes between words, but keep numeric "50-00" intact.
    s = re.sub(r"\s*[—–]\s*", " ", s)
    m = _PRICE_RE.match(s)
    if m:
        name = m.group("name").strip().rstrip(".,;")
        price = _parse_price_token(m.group("price"))
        # Ignore partial regex captures like "Плов 100-" + "5".
        if name and price is not None and not re.search(r"\d-$", name):
            return name, price

    # fallback: parse last token as price (handles "50-00")
    parts = s.rsplit(None, 1)
    if len(parts) == 2:
        name = parts[0].strip()
        price = _parse_price_token(parts[1])
        if name and price is not None:
            return name, price
    return None


def parse_docx_bytes(data: bytes) -> list[tuple[str, float, DishKind]]:
    from io import BytesIO

    doc = Document(BytesIO(data))
    return _parse_docx_document(doc)


def _parse_docx_document(doc: Document) -> list[tuple[str, float, DishKind]]:
    lines: list[str] = []
    for p in doc.paragraphs:
        t = (p.text or "").strip()
        if t:
            lines.append(t)
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if not cells:
                continue
            # Table format: name in first columns, price in last column.
            price = _parse_price_token(cells[-1] if cells else "")
            if len(cells) >= 2 and price is not None:
                name = " ".join(cells[:-1])
                if name:
                    lines.append(f"{name} {price}")
                    continue
            lines.append(" ".join(cells))

    out: list[tuple[str, float, DishKind]] = []
    seen: set[str] = set()
    for line in lines:
        parsed = _parse_line(line)
        if not parsed:
            continue
        name, price = parsed
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        kind = classify_dish(name)
        out.append((name, price, kind))
    return out


def parse_docx_path(path: Path) -> list[tuple[str, float, DishKind]]:
    return _parse_docx_document(Document(str(path)))
