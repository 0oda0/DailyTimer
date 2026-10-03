"""Автосортировка писем: сначала быстрые правила, остальное — пачкой в ИИ."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from .ai import AIClient, AIError

log = logging.getLogger(__name__)

# Ярлыки в Gmail — ASCII, чтобы не возиться с modified UTF-7 в IMAP.
CATEGORIES: dict[str, dict[str, str]] = {
    "important": {"label": "DT/Important", "title": "Важное"},
    "study": {"label": "DT/Study", "title": "Учёба"},
    "dev": {"label": "DT/Dev", "title": "Разработка"},
    "finance": {"label": "DT/Finance", "title": "Финансы и чеки"},
    "subscriptions": {"label": "DT/Subscriptions", "title": "Подписки"},
    "newsletters": {"label": "DT/Newsletters", "title": "Рассылки"},
    "promo": {"label": "DT/Promo", "title": "Реклама"},
    "social": {"label": "DT/Social", "title": "Соцсети"},
    "other": {"label": "DT/Other", "title": "Прочее"},
}

_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("dev", re.compile(r"@(github\.com|gitlab\.com|bitbucket\.org|vercel\.com|netlify\.com|sentry\.io)", re.I)),
    ("study", re.compile(r"(\.edu\b|\.ac\.\w+|univer|university|universitet|lms|moodle|stepik|coursera|\.study)", re.I)),
    ("social", re.compile(r"@(.*\.)?(facebookmail\.com|instagram\.com|vk\.com|linkedin\.com|twitter\.com|x\.com|discord\.com|telegram\.org)", re.I)),
    ("subscriptions", re.compile(r"(подписк|subscription|renew|продлен|автоплат|trial|пробн\w+ период)", re.I)),
    ("finance", re.compile(r"(чек|receipt|invoice|счёт|счет на оплату|оплат|payment|списан|tinkoff|tbank|sberbank|alfabank|paypal)", re.I)),
    ("promo", re.compile(r"(скидк|распродаж|акци[яи]|промокод|sale\b|% off|discount|black friday|предложени)", re.I)),
    ("newsletters", re.compile(r"(newsletter|digest|дайджест|рассылк|weekly|noreply@substack|medium\.com)", re.I)),
]


def classify_by_rules(mail: dict[str, Any]) -> str | None:
    sender = mail.get("from", "")
    text = f"{mail.get('subject', '')} {mail.get('snippet', '')[:300]}"
    for category, pattern in _RULES:
        target = sender if category in {"dev", "social"} else f"{sender} {text}"
        if pattern.search(target):
            return category
    if mail.get("list_unsubscribe"):
        return "newsletters"
    return None


_SYSTEM = (
    "Ты сортируешь почту студента-разработчика. Для каждого письма выбери одну категорию из: "
    + ", ".join(CATEGORIES)
    + ". 'important' — только то, что требует личного действия скоро (дедлайн, ответ человеку, "
    "безопасность аккаунта). Ответь строго JSON-объектом {\"<id>\": \"<категория>\"} без пояснений."
)


def classify(mails: list[dict[str, Any]], ai: AIClient | None) -> dict[str, str]:
    """Возвращает {uid: category}."""
    result: dict[str, str] = {}
    unknown: list[dict[str, Any]] = []
    for mail in mails:
        category = classify_by_rules(mail)
        # Письма от живых людей и "учёба" стоит перепроверить ИИ — там бывает важное.
        if category and (category not in {"study", "finance"} or ai is None):
            result[mail["uid"]] = category
        else:
            unknown.append(mail)
    if ai and unknown:
        for start in range(0, len(unknown), 20):
            chunk = unknown[start : start + 20]
            payload = [
                {"id": m["uid"], "from": m.get("from"), "subject": m.get("subject"), "text": m.get("snippet", "")[:400]}
                for m in chunk
            ]
            try:
                answer = ai.chat_json(_SYSTEM, json.dumps(payload, ensure_ascii=False))
            except AIError as exc:
                log.warning("ИИ-сортировка не удалась: %s", exc)
                answer = {}
            for mail in chunk:
                cat = answer.get(mail["uid"]) if isinstance(answer, dict) else None
                result[mail["uid"]] = cat if cat in CATEGORIES else (classify_by_rules(mail) or "other")
    for mail in unknown:
        result.setdefault(mail["uid"], classify_by_rules(mail) or "other")
    return result
