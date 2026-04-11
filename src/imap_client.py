from __future__ import annotations

import email
import imaplib
import re
from dataclasses import dataclass
from email.header import decode_header
from email.message import Message


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
    with imaplib.IMAP4_SSL(host, port) as M:
        M.login(user, password)
        M.select(mailbox)
        crit = "UNSEEN" if only_unseen else "ALL"
        typ, data = M.search(None, crit)
        if typ != "OK" or not data or not data[0]:
            return out
        ids = data[0].split()
        # oldest first -> process newest last
        for eid in ids:
            typ, msg_data = M.fetch(eid, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            if not isinstance(raw, (bytes, bytearray)):
                continue
            msg = email.message_from_bytes(raw)
            mid = _get_message_id(msg)
            if not mid:
                mid = f"fallback-{eid.decode()}"

            from_hdr = msg.get("From", "")
            if not _sender_matches(from_hdr, sender_filter):
                continue

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
                    continue
                out.append(MailAttachment(message_id=mid, filename=filename, data=payload))
    return out
