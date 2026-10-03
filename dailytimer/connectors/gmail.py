"""Gmail через IMAP: чтение, ярлыки, уборка из «Входящих», спасение писем из «Спама».

Google не пускает сторонние программы по обычному паролю. Нужен «пароль приложения»:
включи двухэтапную проверку и создай его на https://myaccount.google.com/apppasswords
(16 символов) — это и есть «пароль», который вводится в DailyTimer.
"""

from __future__ import annotations

import email
import imaplib
import re
from datetime import date, timedelta
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from html import unescape
from typing import Any, Iterator

HOST = "imap.gmail.com"
imaplib._MAXLINE = 10_000_000  # большие письма с вложениями


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


def parse_message(uid: str, raw: bytes, labels: str = "", flags: str = "") -> dict[str, Any]:
    msg = email.message_from_bytes(raw)
    try:
        date_iso = parsedate_to_datetime(msg.get("Date")).isoformat()
    except Exception:
        date_iso = None
    return {
        "uid": uid,
        "from": _decode(msg.get("From")),
        "subject": _decode(msg.get("Subject")) or "(без темы)",
        "date": date_iso,
        "snippet": _text_of(msg),
        "list_unsubscribe": bool(msg.get("List-Unsubscribe")),
        "gmail_labels": labels,
        "unread": "\\Seen" not in flags,
        "in_inbox": "\\Inbox" in labels,
    }


def _special_folders(lines: list[bytes]) -> dict[str, str]:
    """Разбирает ответ LIST и находит папки по special-use флагам (\\All, \\Junk, \\Trash)."""
    found = {}
    for raw in lines:
        line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
        match = re.match(r'\((?P<flags>[^)]*)\) "(?P<sep>[^"]*)" (?P<name>.+)$', line)
        if not match:
            continue
        name = match.group("name").strip()
        for flag in ("\\All", "\\Junk", "\\Trash", "\\Sent"):
            if flag in match.group("flags").split():
                found[flag] = name if name.startswith('"') else f'"{name}"'
    return found


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
        typ, lines = self.imap.list()
        self.folders = _special_folders(lines or []) if typ == "OK" else {}
        self.select("all")

    def select(self, which: str) -> None:
        """which: all | spam | inbox. «Вся почта» — UID там стабильны при архивации."""
        name = {"all": self.folders.get("\\All"), "spam": self.folders.get("\\Junk"),
                "trash": self.folders.get("\\Trash")}.get(which) or "INBOX"
        typ, data = self.imap.select(name)
        if typ != "OK":
            raise GmailError(f"Не удалось открыть папку {name}: {data}")
        self.current = which if name != "INBOX" else "inbox"

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
        # Запрос может быть на русском — передаём его IMAP-литералом в UTF-8.
        self.imap.literal = gmail_query.encode("utf-8")
        typ, data = self.imap.uid("SEARCH", "CHARSET", "UTF-8", "X-GM-RAW")
        if typ != "OK":
            raise GmailError(f"Поиск не удался: {data}")
        uids = data[0].decode().split() if data and data[0] else []
        return uids[-limit:]

    def search_all(self, gmail_query: str = "") -> list[str]:
        """Все UID по запросу (без ограничения) — для генеральной уборки."""
        if not gmail_query:
            typ, data = self.imap.uid("SEARCH", "ALL")
        else:
            self.imap.literal = gmail_query.encode("utf-8")
            typ, data = self.imap.uid("SEARCH", "CHARSET", "UTF-8", "X-GM-RAW")
        if typ != "OK":
            raise GmailError(f"Поиск не удался: {data}")
        return data[0].decode().split() if data and data[0] else []

    def headers(self, uids: list[str]) -> list[dict[str, str]]:
        """Только отправитель и тема — быстро, без тела письма."""
        out = []
        for uid in uids:
            typ, data = self.imap.uid("FETCH", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if typ == "OK" and data and isinstance(data[0], tuple):
                msg = email.message_from_bytes(data[0][1])
                out.append({"uid": uid, "from": _decode(msg.get("From")), "subject": _decode(msg.get("Subject")),
                            "date": msg.get("Date", "")})
        return out

    def headers_batch(self, uids: list[str], batch: int = 200) -> list[dict[str, Any]]:
        """Заголовки многих писем пачками (один запрос на 200 писем) — для разбора всего ящика."""
        out: list[dict[str, Any]] = []
        for start in range(0, len(uids), batch):
            chunk = ",".join(uids[start : start + batch])
            typ, data = self.imap.uid(
                "FETCH", chunk, "(UID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE LIST-UNSUBSCRIBE)])")
            if typ != "OK" or not data:
                continue
            for item in data:
                if not isinstance(item, tuple):
                    continue
                meta = item[0].decode(errors="replace")
                uid = re.search(r"UID (\d+)", meta)
                if not uid:
                    continue
                msg = email.message_from_bytes(item[1])
                out.append({"uid": uid.group(1), "from": _decode(msg.get("From")),
                            "subject": _decode(msg.get("Subject")) or "(без темы)", "date": msg.get("Date", ""),
                            "snippet": "", "list_unsubscribe": bool(msg.get("List-Unsubscribe")), "in_inbox": True})
        return out

    def bulk(self, uids: list[str], label: str | None = None, archive: bool = False, read: bool = False,
             batch: int = 500) -> int:
        """Ярлык / архив / «прочитано» сразу для многих писем."""
        if label:
            self.ensure_label(label)
        for start in range(0, len(uids), batch):
            chunk = ",".join(uids[start : start + batch])
            if label:
                self.imap.uid("STORE", chunk, "+X-GM-LABELS", f'("{label}")')
            if read:
                self.imap.uid("STORE", chunk, "+FLAGS.SILENT", "(\\Seen)")
            if archive:
                self.imap.uid("STORE", chunk, "-X-GM-LABELS", "(\\Inbox)")
        return len(uids)

    PURGE_LABEL = "DT/Purged"

    def trash(self, uids: list[str], batch: int = 500) -> int:
        """Перемещает письма в «Корзину» пачками (Gmail удалит их окончательно через 30 дней).
        Каждое письмо получает ярлык DT/Purged — чтобы «удалить навсегда» трогало только наши."""
        target = self.folders.get("\\Trash")
        if not target:
            raise GmailError("Не нашёл папку «Корзина» в Gmail")
        self.ensure_label(self.PURGE_LABEL)
        moved = 0
        for start in range(0, len(uids), batch):
            chunk = ",".join(uids[start : start + batch])
            self.imap.uid("STORE", chunk, "+X-GM-LABELS", f'("{self.PURGE_LABEL}")')
            typ, data = self.imap.uid("MOVE", chunk, target)
            if typ != "OK":
                raise GmailError(f"Не удалось переместить в корзину: {data}")
            moved += len(uids[start : start + batch])
        return moved

    def purge_trashed(self) -> int:
        """Окончательно удаляет из «Корзины» только письма, которые туда переложил DailyTimer."""
        self.select("trash")
        try:
            uids = self.search_all("label:dt-purged")
            for start in range(0, len(uids), 500):
                chunk = ",".join(uids[start : start + 500])
                self.imap.uid("STORE", chunk, "+FLAGS.SILENT", "(\\Deleted)")
            self.imap.expunge()
            return len(uids)
        finally:
            self.select("all")

    def search_since(self, days: int, limit: int = 40) -> list[str]:
        since = (date.today() - timedelta(days=days)).strftime("%d-%b-%Y")
        typ, data = self.imap.uid("SEARCH", "SINCE", since)
        uids = data[0].decode().split() if typ == "OK" and data and data[0] else []
        return uids[-limit:]

    def fetch(self, uids: list[str]) -> Iterator[dict[str, Any]]:
        for uid in uids:
            typ, data = self.imap.uid("FETCH", uid, "(FLAGS X-GM-LABELS BODY.PEEK[])")
            if typ != "OK" or not data or not isinstance(data[0], tuple):
                continue
            meta = data[0][0].decode(errors="replace")
            labels = re.search(r"X-GM-LABELS \((.*?)\)", meta)
            flags = re.search(r"FLAGS \((.*?)\)", meta)
            yield parse_message(uid, data[0][1], labels.group(1) if labels else "", flags.group(1) if flags else "")

    def ensure_label(self, label: str) -> None:
        self.imap.create(f'"{label}"')  # если ярлык уже есть, Gmail просто вернёт NO

    def add_label(self, uid: str, label: str) -> None:
        self.imap.uid("STORE", uid, "+X-GM-LABELS", f'("{label}")')

    def archive(self, uid: str) -> None:
        self.imap.uid("STORE", uid, "-X-GM-LABELS", "(\\Inbox)")

    def mark_read(self, uid: str) -> None:
        self.imap.uid("STORE", uid, "+FLAGS", "(\\Seen)")

    def move_to_inbox(self, uid: str) -> bool:
        """Из «Спама» во «Входящие» (Gmail поддерживает MOVE)."""
        typ, _ = self.imap.uid("MOVE", uid, "INBOX")
        return typ == "OK"
