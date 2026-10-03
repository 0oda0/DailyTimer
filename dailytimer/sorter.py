"""Анализ писем: категория, важность, нужен ли ответ, краткая сводка.

Сначала быстрые правила (работают и без ИИ), затем всё неочевидное — пачками в ИИ.
"""

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
    "personal": {"label": "DT/Personal", "title": "Личные"},
    "study": {"label": "DT/Study", "title": "Учёба"},
    "dev": {"label": "DT/Dev", "title": "Разработка"},
    "receipts": {"label": "DT/Receipts", "title": "Чеки и оплаты"},
    "security": {"label": "DT/Security", "title": "Безопасность и коды"},
    "newsletters": {"label": "DT/Newsletters", "title": "Рассылки"},
    "promo": {"label": "DT/Promo", "title": "Реклама"},
    "social": {"label": "DT/Social", "title": "Соцсети"},
    "other": {"label": "DT/Other", "title": "Прочее"},
}
# Что считаем мусором и убираем из «Входящих» (если письмо не помечено важным).
JUNK = {"promo", "newsletters", "social", "other"}

_RULES: list[tuple[str, str, re.Pattern[str]]] = [
    # (категория, где искать: sender|text|all, шаблон)
    ("dev", "sender", re.compile(r"@(.*\.)?(github\.com|gitlab\.com|bitbucket\.org|vercel\.com|netlify\.com|sentry\.io|jetbrains\.com)", re.I)),
    ("social", "sender", re.compile(r"@(.*\.)?(facebookmail\.com|instagram\.com|vk\.com|vk\.ru|linkedin\.com|twitter\.com|x\.com|discord\.com|telegram\.org|tiktok\.com|reddit\.com|pinterest\.com)", re.I)),
    ("security", "text", re.compile(r"(код подтверждения|verification code|one-time|вход в аккаунт|new sign-in|новый вход|сброс пароля|password reset|security alert|оповещение системы безопасности)", re.I)),
    ("receipts", "all", re.compile(r"(чек\b|кассовый чек|receipt|invoice|счёт|счет на оплату|оплата прошла|payment (received|confirmed)|списан|order confirm|заказ оформлен|ваш заказ|подписк\w+ (продлен|оформлен)|subscription (renew|confirm)|ofd\.ru|taxcom|1-ofd|platformaofd)", re.I)),
    ("study", "all", re.compile(r"(\.edu\b|\.ac\.\w+|универ|university|институт|кафедр|деканат|lms|moodle|stepik|coursera|сесси[яи]|экзамен|зачёт|зачет|курсов\w+ работ|дедлайн|deadline)", re.I)),
    ("promo", "all", re.compile(r"(скидк|распродаж|акци[яи]|промокод|кэшбэк|sale\b|% off|discount|black friday|выгодн|только сегодня|спецпредложени)", re.I)),
    ("newsletters", "all", re.compile(r"(newsletter|digest|дайджест|рассылк|weekly|substack|medium\.com)", re.I)),
]
_NOREPLY = re.compile(r"(no-?reply|noreply|do-?not-?reply|notifications?@|mailer|news@|info@|support@|robot)", re.I)
_ASKS = re.compile(
    r"(\?|подскажи|ответь|ответьте|жду ответ|сможешь|сможете|можешь ли|можете ли|когда (будет|сможешь|удобно)|"
    r"подтверди|согласу|please (reply|confirm|let me know)|could you|can you|are you available)",
    re.I,
)
_URGENT = re.compile(r"(срочно|urgent|asap|дедлайн|deadline|до \d{1,2}[.:]\d{2}|сегодня до|завтра до|последн\w+ (день|срок)|просроч)", re.I)


def is_person(mail: dict[str, Any]) -> bool:
    return not mail.get("list_unsubscribe") and not _NOREPLY.search(mail.get("from", ""))


def rule_analysis(mail: dict[str, Any]) -> dict[str, Any]:
    sender = mail.get("from", "")
    text = f"{mail.get('subject', '')} {mail.get('snippet', '')[:600]}"
    category = None
    for cat, where, pattern in _RULES:
        target = {"sender": sender, "text": text, "all": f"{sender} {text}"}[where]
        if pattern.search(target):
            category = cat
            break
    person = is_person(mail)
    if category is None:
        category = "personal" if person else ("newsletters" if mail.get("list_unsubscribe") else "other")
    needs_reply = person and category in {"personal", "study", "dev", "other"} and bool(_ASKS.search(text))
    important = bool(_URGENT.search(text)) and category not in {"promo", "newsletters", "social"}
    if needs_reply or important:
        if category in {"personal", "other"}:
            category = "important"
    return {
        "category": category,
        "important": important or category == "important",
        "needs_reply": needs_reply,
        "summary": _short(mail.get("snippet", "")),
        "by": "rules",
    }


def _short(text: str, limit: int = 160) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


_SYSTEM = (
    "Ты разбираешь почту студента-программиста. Для каждого письма верни объект:\n"
    '{"c": категория, "imp": 0|1, "reply": 0|1, "sum": "суть письма в 1 предложении по-русски"}\n'
    "Категории: " + ", ".join(CATEGORIES) + ".\n"
    "imp=1 только если письмо требует действия в ближайшие дни: дедлайн, учёба, деньги, "
    "доступ к аккаунту, письмо от живого человека по делу. Реклама и рассылки — всегда imp=0.\n"
    "reply=1 только если живой человек ждёт ответа.\n"
    'Ответ — строго JSON-объект вида {"<id>": {...}, ...} без пояснений.'
)


def analyze(mails: list[dict[str, Any]], ai: AIClient | None, batch: int = 8) -> dict[str, dict[str, Any]]:
    """Возвращает {uid: {category, important, needs_reply, summary, by}}."""
    result = {m["uid"]: rule_analysis(m) for m in mails}
    if ai is None:
        return result
    # Явную рекламу/соцсети с List-Unsubscribe не гоняем через ИИ — экономим время модели.
    unsure = [
        m for m in mails
        if not (m.get("list_unsubscribe") and result[m["uid"]]["category"] in {"promo", "social", "newsletters"})
    ]
    for start in range(0, len(unsure), batch):
        chunk = unsure[start : start + batch]
        payload = [
            {"id": m["uid"], "from": m.get("from"), "subject": m.get("subject"), "text": m.get("snippet", "")[:500]}
            for m in chunk
        ]
        try:
            answer = ai.chat_json(_SYSTEM, json.dumps(payload, ensure_ascii=False))
        except AIError as exc:
            log.warning("ИИ-анализ почты не удался, остаются правила: %s", exc)
            continue
        if not isinstance(answer, dict):
            continue
        for mail in chunk:
            item = answer.get(mail["uid"])
            if not isinstance(item, dict):
                continue
            rules = result[mail["uid"]]
            category = item.get("c") if item.get("c") in CATEGORIES else rules["category"]
            # Чеки и коды по правилам надёжнее, чем по мнению маленькой модели.
            if rules["category"] in {"receipts", "security"}:
                category = rules["category"]
            result[mail["uid"]] = {
                "category": category,
                "important": (bool(item.get("imp")) or rules["important"])
                and category not in {"promo", "newsletters", "social"},
                "needs_reply": bool(item.get("reply")) and is_person(mail),
                "summary": _short(str(item.get("sum") or rules["summary"])),
                "by": ai.name,
            }
    return result


def should_rescue_from_spam(mail: dict[str, Any], verdict: dict[str, Any]) -> bool:
    """Письмо из «Спама», которое стоит вернуть во «Входящие».

    В спаме много мошенничества под «личные письма», поэтому без ИИ возвращаем только учёбу,
    а с ИИ — то, что он счёл важным, ждущим ответа, учёбой, чеком или уведомлением безопасности.
    """
    if verdict["category"] in {"promo", "newsletters", "social"}:
        return False
    if verdict["by"] == "rules":
        return verdict["category"] == "study"
    return verdict["important"] or verdict["needs_reply"] or verdict["category"] in {"study", "receipts", "security"}
