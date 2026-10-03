"""Gmail через IMAP: читаем свежие письма, сортируем, вешаем ярлыки DT/*.

Google не пускает сторонние программы по обычному паролю. Нужен «пароль приложения»:
включи двухэтапную проверку и создай его на https://myaccount.google.com/apppasswords
(16 символов) — это и есть «пароль», который вводится в DailyTimer.
"""

from __future__ import annotations

import email
import imaplib
import re
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Any, Iterator

HOST = "imap.gmail.com"


class GmailError(RuntimeError):
    pass


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _text_of(msg: Message, limit: int = 1500) -> str:
    plain, html = "", ""
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        ctype = part.get_content_type()
        if part.get_content_disposition() == "attachment" or ctype not in {"text/plain", "text/html"}:
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except Exception:
            continue
        if ctype == "text/plain" and not plain:
            plain = text
        elif ctype == "text/html" and not html:
            html = text
    if not plain and html:
        html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
        plain = unescape(re.sub(r"<[^>]+>", " ", html))
    return re.sub(r"\s+", " ", plain).strip()[:limit]


def parse_message(uid: str, raw: bytes, labels: str = "") -> dict[str, Any]:
    msg = email.message_from_bytes(raw)
    try:
        date = parsedate_to_datetime(msg.get("Date")).isoformat()
    except Exception:
        date = None
    return {
        "uid": uid,
        "from": _decode(msg.get("From")),
        "subject": _decode(msg.get("Subject")) or "(без темы)",
        "date": date,
        "snippet": _text_of(msg),
        "list_unsubscribe": bool(msg.get("List-Unsubscribe")),
        "gmail_labels": labels,
    }


class GmailClient:
    def __init__(self, address: str, app_password: str):
        if not address or not app_password:
            raise GmailError("Не указаны адрес Gmail и пароль приложения")
        self.imap = imaplib.IMAP4_SSL(HOST)
        try:
            self.imap.login(address, app_password.replace(" ", ""))
        except imaplib.IMAP4.error as exc:
            raise GmailError(
                "Gmail не принял логин. Нужен пароль приложения "
                "(https://myaccount.google.com/apppasswords), а не обычный пароль."
            ) from exc
        self.imap.select("INBOX")

    def close(self) -> None:
        try:
            self.imap.logout()
        except Exception:
            pass

    def __enter__(self) -> "GmailClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def search(self, gmail_query: str, limit: int = 60) -> list[str]:
        """Поиск синтаксисом Gmail (X-GM-RAW), например 'newer_than:2d'."""
        typ, data = self.imap.uid("SEARCH", "X-GM-RAW", f'"{gmail_query}"')
        if typ != "OK":
            raise GmailError(f"Поиск не удался: {data}")
        uids = data[0].decode().split() if data and data[0] else []
        return uids[-limit:]

    def fetch(self, uids: list[str]) -> Iterator[dict[str, Any]]:
        for uid in uids:
            typ, data = self.imap.uid("FETCH", uid, "(X-GM-LABELS BODY.PEEK[])")
            if typ != "OK" or not data or not isinstance(data[0], tuple):
                continue
            meta = data[0][0].decode(errors="replace")
            labels = re.search(r"X-GM-LABELS \((.*?)\)", meta)
            yield parse_message(uid, data[0][1], labels.group(1) if labels else "")

    def ensure_label(self, label: str) -> None:
        self.imap.create(f'"{label}"')  # если ярлык уже есть, Gmail просто вернёт NO

    def add_label(self, uid: str, label: str) -> None:
        self.imap.uid("STORE", uid, "+X-GM-LABELS", f'("{label}")')

    def archive(self, uid: str) -> None:
        self.imap.uid("STORE", uid, "-X-GM-LABELS", "(\\Inbox)")
