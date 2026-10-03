"""Подписки: находим их по чекам и письмам о продлении в Gmail + ручной список.

Ручной формат (по строке): «Spotify 299 RUB 15» — название, сумма, валюта, день списания.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

# Известные сервисы: фрагмент адреса отправителя → название.
KNOWN_SERVICES = {
    "spotify": "Spotify",
    "netflix": "Netflix",
    "youtube": "YouTube Premium",
    "apple.com": "Apple",
    "google.com": "Google",
    "yandex": "Яндекс Плюс",
    "kinopoisk": "Кинопоиск",
    "ivi.ru": "Иви",
    "okko": "Okko",
    "vk.com": "VK Музыка",
    "github.com": "GitHub",
    "openai.com": "ChatGPT",
    "anthropic.com": "Claude",
    "jetbrains": "JetBrains",
    "notion": "Notion",
    "figma": "Figma",
    "adobe": "Adobe",
    "microsoft": "Microsoft",
    "steampowered": "Steam",
    "playstation": "PlayStation",
    "discord": "Discord Nitro",
    "telegram": "Telegram Premium",
    "boosty": "Boosty",
    "patreon": "Patreon",
}

GMAIL_QUERY = (
    "newer_than:45d (subscription OR receipt OR invoice OR renewal OR renew "
    "OR подписка OR подписки OR чек OR продление OR списание OR оплата)"
)

_AMOUNT = re.compile(
    r"(?:(?P<cur1>[$€£₽]|USD|EUR|RUB|руб\.?)\s?(?P<a1>\d[\d\s]{0,6}(?:[.,]\d{2})?))"
    r"|(?:(?P<a2>\d[\d\s]{0,6}(?:[.,]\d{2})?)\s?(?P<cur2>[$€£₽]|USD|EUR|RUB|руб\.?))",
    re.I,
)
_CUR = {"$": "USD", "€": "EUR", "£": "GBP", "₽": "RUB", "руб": "RUB", "руб.": "RUB"}


def find_amount(text: str) -> tuple[float, str] | None:
    match = _AMOUNT.search(text)
    if not match:
        return None
    raw = (match.group("a1") or match.group("a2")).replace(" ", "").replace(",", ".")
    cur = (match.group("cur1") or match.group("cur2")).upper().rstrip(".")
    return float(raw), _CUR.get(cur.lower(), _CUR.get(cur, cur))


def service_of(sender: str) -> str | None:
    lowered = sender.lower()
    for key, name in KNOWN_SERVICES.items():
        if key in lowered:
            return name
    return None


def from_emails(mails: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for mail in sorted(mails, key=lambda m: m.get("date") or ""):
        name = service_of(mail.get("from", ""))
        if not name:
            continue
        text = f"{mail.get('subject', '')} {mail.get('snippet', '')}"
        amount = find_amount(text)
        charged = (mail.get("date") or "")[:10] or None
        next_charge = None
        if charged:
            next_charge = (datetime.fromisoformat(charged).date() + timedelta(days=30)).isoformat()
        found[name] = {
            "name": name,
            "amount": amount[0] if amount else None,
            "currency": amount[1] if amount else None,
            "last_charge": charged,
            "next_charge": next_charge,
            "evidence": mail.get("subject"),
            "source": "gmail",
        }
    return list(found.values())


def parse_manual(text: str, today: date) -> list[dict[str, Any]]:
    subs = []
    for line in (text or "").splitlines():
        match = re.match(r"^\s*(.+?)\s+(\d+(?:[.,]\d+)?)\s+([A-Za-zА-Яа-я₽$€]+)\s+(\d{1,2})\s*$", line)
        if not match:
            continue
        name, amount, currency, day = match.groups()
        day = min(int(day), 28)
        next_charge = today.replace(day=day)
        if next_charge < today:
            next_charge = (next_charge.replace(day=1) + timedelta(days=32)).replace(day=day)
        subs.append(
            {
                "name": name.strip(),
                "amount": float(amount.replace(",", ".")),
                "currency": currency.upper(),
                "last_charge": None,
                "next_charge": next_charge.isoformat(),
                "evidence": "вручную",
                "source": "manual",
            }
        )
    return subs


def merge(manual: list[dict[str, Any]], detected: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    by_name = {s["name"].lower(): s for s in detected}
    by_name.update({s["name"].lower(): s for s in manual})  # ручные важнее
    subs = list(by_name.values())
    for sub in subs:
        if sub.get("next_charge"):
            sub["days_left"] = (date.fromisoformat(sub["next_charge"]) - today).days
    return sorted(subs, key=lambda s: (s.get("days_left") is None, s.get("days_left") or 0))
