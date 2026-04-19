from __future__ import annotations

import email
import imaplib
import logging
import re
from dataclasses import dataclass
from email.header import decode_header
from email.message import Message

log = logging.getLogger(__name__)


@dataclass
class MailAttachment:
    message_id: str
    filename: str
    data: bytes


def _decode_mime_header(s: str) -> str:
    parts = decode_header(s)
    out: list[str] = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def _get_message_id(msg: Message) -> str:
    mid = msg.get("Message-ID") or ""
    return mid.strip() or ""


def _sender_matches(from_hdr: str, filt: str | None) -> bool:
    if not filt:
        return True
    return filt.lower() in (from_hdr or "").lower()


def fetch_latest_docx_attachments(
    host: str,
    port: int,
    user: str,
    password: str,
    *,
    sender_filter: str | None,
    only_unseen: bool,
    mailbox: str = "INBOX",
) -> list[MailAttachment]:
    out: list[MailAttachment] = []
    crit = "UNSEEN" if only_unseen else "ALL"
    log.info(
        "IMAP fetch: connect %s:%s user=%s mailbox=%s crit=%s sender_filter=%r",
        host,
        port,
        user,
        mailbox,
        crit,
        sender_filter,
    )
    with imaplib.IMAP4_SSL(host, port) as M:
        M.login(user, password)
        log.info("IMAP fetch: login OK")
        typ_sel, _ = M.select(mailbox)
        log.info("IMAP fetch: SELECT %s -> %s", mailbox, typ_sel)
        typ, data = M.search(None, crit)
        if typ != "OK" or not data or not data[0]:
            log.info("IMAP fetch: SEARCH %s -> писем нет (typ=%s)", crit, typ)
            return out
        ids = data[0].split()
        log.info("IMAP fetch: найдено писем по %s: %s", crit, len(ids))
        # oldest first -> process newest last
        for eid in ids:
            typ, msg_data = M.fetch(eid, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                log.warning("IMAP fetch: не удалось FETCH id=%s typ=%s", eid, typ)
                continue
            raw = msg_data[0][1]
            if not isinstance(raw, (bytes, bytearray)):
                log.warning("IMAP fetch: id=%s payload не bytes", eid)
                continue
            msg = email.message_from_bytes(raw)
            mid = _get_message_id(msg)
            if not mid:
                mid = f"fallback-{eid.decode()}"

            from_hdr = msg.get("From", "")
            if not _sender_matches(from_hdr, sender_filter):
                log.info(
                    "IMAP fetch: письмо id=%s msg_id=%s пропуск (отправитель не под фильтр): %s",
                    eid,
                    mid[:80] if len(mid) > 80 else mid,
                    from_hdr[:120],
                )
                continue

            docx_in_msg = 0
            for part in msg.walk():
                if part.get_content_maintype() == "multipart":
                    continue
                filename = part.get_filename()
                if not filename:
                    continue
                filename = _decode_mime_header(filename)
                if not re.search(r"\.docx$", filename, re.IGNORECASE):
                    continue
                payload = part.get_payload(decode=True)
                if not payload:
                    log.info("IMAP fetch: вложение %s без данных", filename)
                    continue
                out.append(MailAttachment(message_id=mid, filename=filename, data=payload))
                docx_in_msg += 1
            if docx_in_msg:
                log.info(
                    "IMAP fetch: письмо id=%s msg_id=%s — вложений .docx: %s (%s)",
                    eid,
                    mid[:80] if len(mid) > 80 else mid,
                    docx_in_msg,
                    ", ".join(a.filename for a in out[-docx_in_msg:]),
                )
            else:
                log.info(
                    "IMAP fetch: письмо id=%s msg_id=%s подошло по отправителю, но .docx не найдено",
                    eid,
                    mid[:80] if len(mid) > 80 else mid,
                )
    log.info("IMAP fetch: итого вложений .docx к обработке: %s", len(out))
    return out


def imap_diagnose_connection(
    host: str,
    port: int,
    user: str,
    password: str,
    *,
    sender_filter: str | None,
    only_unseen: bool,
    mailbox: str = "INBOX",
) -> str:
    """
    Ручная проверка IMAP (для тестовой кнопки в боте): подключение, счётчики писем,
    образцы From/Subject, сколько .docx вернёт текущая логика fetch_latest_docx_attachments.
    """
    lines: list[str] = []

    def add(msg: str) -> None:
        log.info("IMAP diagnose: %s", msg)
        lines.append(msg)

    add(f"Сервер: {host}:{port}, логин: {user}")
    add(f"Папка: {mailbox!r}")
    add(f"Фильтр отправителя (подстрока From): {sender_filter or '(не задан — все)'}")
    add(f"В настройках бота only_unseen={only_unseen} → поиск: {'UNSEEN' if only_unseen else 'ALL'}")
    try:
        with imaplib.IMAP4_SSL(host, port) as M:
            add("SSL: соединение установлено")
            M.login(user, password)
            add("Аутентификация: успех")
            typ_sel, _ = M.select(mailbox)
            add(f"SELECT: {typ_sel}")

            typ_u, data_u = M.search(None, "UNSEEN")
            unseen_n = 0
            if typ_u == "OK" and data_u and data_u[0]:
                unseen_n = len(data_u[0].split())
            add(f"Писем UNSEEN: {unseen_n}")

            typ_a, data_a = M.search(None, "ALL")
            all_n = 0
            all_ids: list[bytes] = []
            if typ_a == "OK" and data_a and data_a[0]:
                all_ids = data_a[0].split()
                all_n = len(all_ids)
            add(f"Писем по SEARCH ALL: {all_n}")

            sample = all_ids[-5:] if len(all_ids) > 5 else all_ids
            if sample:
                add("Последние письма в папке (заголовки):")
                for eid in sample:
                    typ, msg_data = M.fetch(eid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID)])")
                    if typ != "OK" or not msg_data or not msg_data[0]:
                        add(f"  id={eid!r}: не удалось прочитать заголовок")
                        continue
                    chunk = msg_data[0]
                    hdr_bytes = b""
                    if isinstance(chunk, tuple) and len(chunk) >= 2 and isinstance(chunk[1], (bytes, bytearray)):
                        hdr_bytes = bytes(chunk[1])
                    elif isinstance(chunk, (bytes, bytearray)):
                        hdr_bytes = bytes(chunk)
                    if hdr_bytes:
                        preview = hdr_bytes.decode("utf-8", errors="replace").strip().replace("\r\n", " | ")
                        if len(preview) > 220:
                            preview = preview[:217] + "…"
                        add(f"  id={eid.decode()}: {preview}")
                    else:
                        add(f"  id={eid.decode()}: пустой ответ FETCH")
            else:
                add("Писем в папке нет — нечего показывать.")

        atts = fetch_latest_docx_attachments(
            host,
            port,
            user,
            password,
            sender_filter=sender_filter,
            only_unseen=only_unseen,
            mailbox=mailbox,
        )
        add(f"По правилам бота сейчас готово к разбору .docx: {len(atts)} шт.")
        for a in atts[:10]:
            mid = a.message_id
            mid_show = (mid[:70] + "…") if len(mid) > 70 else mid
            add(f"  • {a.filename} (Message-ID: {mid_show})")
        if len(atts) > 10:
            add(f"  … и ещё {len(atts) - 10}")
        add("Проверка завершена. Полный ход загрузки смотрите в логах (IMAP fetch / IMAP diagnose).")
    except Exception as e:  # noqa: BLE001
        log.exception("IMAP diagnose failed")
        add(f"Ошибка: {type(e).__name__}: {e}")
    return "\n".join(lines)
