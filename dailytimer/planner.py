"""Формирует план на день из собранных данных: через ИИ, а без ключа — по простым правилам."""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

from .ai import AIClient, AIError

log = logging.getLogger(__name__)

_SYSTEM = """Ты — личный ассистент-планировщик студента, который учится и программирует.
По данным из его сервисов составь реалистичный план на сегодня на русском языке в Markdown:
1. «## Главное сегодня» — 3 самых важных пункта.
2. «## Расписание» — блоки по времени от подъёма до сна: пары (фиксированы), дорога,
   глубокая работа, ревью PR, разбор почты, еда, отдых. Не ставь задачи поверх пар.
3. «## Почта» — на какие письма ответить и что можно игнорировать.
4. «## Деньги и подписки» — что скоро спишется и стоит ли отменить.
Будь конкретным (названия репозиториев, тем писем, предметов), не выдумывай фактов."""


def build_context(data: dict[str, Any], settings: dict[str, Any], today: date) -> str:
    lines = [
        f"Сегодня: {today.isoformat()} ({['пн','вт','ср','чт','пт','сб','вс'][today.weekday()]})",
        f"Подъём: {settings.get('wake_time')}, отбой: {settings.get('sleep_time')}",
    ]
    lessons = [l for l in (data.get("schedule") or {}).get("lessons", []) if l["start"].startswith(today.isoformat())]
    lines.append("\nПары сегодня:" if lessons else "\nПар сегодня нет.")
    for lesson in lessons:
        place = f" ({lesson['location']})" if lesson.get("location") else ""
        lines.append(f"- {lesson['start'][11:16]}–{lesson['end'][11:16]} {lesson['title']}{place}")

    gh = data.get("github") or {}
    for key, title in (("review_requests", "Ждут моего ревью"), ("assigned", "Назначено на меня"), ("my_prs", "Мои открытые PR")):
        items = gh.get(key) or []
        if items:
            lines.append(f"\n{title}:")
            lines += [f"- {i['repo']}#{i['number']}: {i['title']}" for i in items[:10]]
    if gh.get("notifications"):
        lines.append(f"\nНепрочитанных уведомлений GitHub: {len(gh['notifications'])}")

    mails = (data.get("gmail") or {}).get("messages", [])
    important = [m for m in mails if m.get("category") in {"important", "study", "finance"}]
    if important:
        lines.append("\nВажные письма:")
        lines += [f"- [{m['category']}] {m['from']}: {m['subject']} — {m['snippet'][:160]}" for m in important[:15]]
    if mails:
        counts: dict[str, int] = {}
        for m in mails:
            counts[m.get("category", "other")] = counts.get(m.get("category", "other"), 0) + 1
        lines.append("Всего свежих писем по категориям: " + ", ".join(f"{k}={v}" for k, v in counts.items()))

    subs = (data.get("subscriptions") or {}).get("items", [])
    soon = [s for s in subs if s.get("days_left") is not None and s["days_left"] <= 7]
    if subs:
        lines.append("\nПодписки (скоро списания):" if soon else "\nБлижайших списаний нет.")
        for s in soon:
            amount = f"{s['amount']} {s['currency']}" if s.get("amount") else "сумма неизвестна"
            lines.append(f"- {s['name']}: {amount}, через {s['days_left']} дн. ({s['next_charge']})")
    return "\n".join(lines)


def rule_based_plan(data: dict[str, Any], settings: dict[str, Any], today: date) -> str:
    out = [f"# План на {today.strftime('%d.%m.%Y')}", "", "_ИИ не подключён — план собран по правилам._", ""]
    lessons = [l for l in (data.get("schedule") or {}).get("lessons", []) if l["start"].startswith(today.isoformat())]
    gh = data.get("github") or {}
    mails = (data.get("gmail") or {}).get("messages", [])
    subs = (data.get("subscriptions") or {}).get("items", [])

    out.append("## Главное сегодня")
    top = []
    if lessons:
        top.append(f"Пары: {len(lessons)}, первая в {lessons[0]['start'][11:16]}")
    if gh.get("review_requests"):
        top.append(f"Сделать ревью: {len(gh['review_requests'])} PR")
    important = [m for m in mails if m.get("category") == "important"]
    if important:
        top.append(f"Ответить на важные письма: {len(important)}")
    out += [f"- {t}" for t in top] or ["- Свободный день — займись своими проектами"]

    out += ["", "## Расписание", f"- {settings.get('wake_time')} подъём"]
    out += [f"- {l['start'][11:16]}–{l['end'][11:16]} {l['title']}" for l in lessons]
    out.append(f"- {settings.get('sleep_time')} отбой")

    if gh.get("review_requests") or gh.get("assigned"):
        out += ["", "## GitHub"]
        out += [f"- Ревью: [{i['repo']}#{i['number']} {i['title']}]({i['url']})" for i in gh.get("review_requests", [])[:5]]
        out += [f"- Задача: [{i['repo']}#{i['number']} {i['title']}]({i['url']})" for i in gh.get("assigned", [])[:5]]
    if important:
        out += ["", "## Почта"] + [f"- {m['from']}: {m['subject']}" for m in important[:7]]
    soon = [s for s in subs if s.get("days_left") is not None and s["days_left"] <= 7]
    if soon:
        out += ["", "## Деньги и подписки"]
        out += [f"- {s['name']}: через {s['days_left']} дн., {s.get('amount') or '?'} {s.get('currency') or ''}" for s in soon]
    return "\n".join(out)


def make_plan(data: dict[str, Any], settings: dict[str, Any], today: date | None = None) -> tuple[str, str]:
    """Возвращает (markdown, движок)."""
    today = today or datetime.now().date()
    ai = AIClient.from_settings(settings)
    if ai:
        try:
            return ai.chat(_SYSTEM, build_context(data, settings, today)), ai.name
        except AIError as exc:
            log.warning("ИИ недоступен, план по правилам: %s", exc)
            return rule_based_plan(data, settings, today) + f"\n\n> ⚠️ ИИ недоступен: {exc}", "rules"
    return rule_based_plan(data, settings, today), "rules"
