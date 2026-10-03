"""Уведомления в Telegram об изменениях: расписание, GitHub, деньги, сбои, обновления DailyTimer.

После каждой синхронизации сравниваем «было» и «стало» и шлём только новое.
Первая синхронизация источника ничего не шлёт — иначе пришло бы всё сразу.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Callable

from . import __version__
from .changelog import CHANGES
from .storage import Storage

log = logging.getLogger(__name__)

SOURCE_NAMES = {
    "github": "GitHub", "gmail": "Gmail", "telegram": "Telegram", "schedule": "расписание",
    "portal": "личный кабинет вуза", "subscriptions": "подписки", "weather": "погода",
    "feeds": "новости", "codeforces": "Codeforces", "server": "сервер (агент)",
    "server_projects": "проекты на сервере",
}
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def _day_label(day: str, today: date) -> str:
    d = date.fromisoformat(day)
    delta = (d - today).days
    name = "сегодня" if delta == 0 else "завтра" if delta == 1 else f"{WEEKDAYS[d.weekday()]} {d:%d.%m}"
    return name


def schedule_changes(old: list[dict[str, Any]], new: list[dict[str, Any]], today: date,
                     old_today: date, days: int = 7) -> list[str]:
    """Что поменялось в парах на ближайшие дни: добавились, отменились, перенесли, сменилась аудитория."""
    # Сравниваем только даты, которые были видны и раньше, и сейчас, — иначе «новыми» станут
    # пары, просто попавшие в окно по мере движения дней.
    lo = today.isoformat()
    hi = min(today + timedelta(days=days), old_today + timedelta(days=14)).isoformat()

    def by_day(lessons: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for lesson in lessons:
            if lo <= lesson["start"][:10] < hi:
                out[lesson["start"][:10]].append(lesson)
        return out

    old_days, new_days = by_day(old), by_day(new)
    lines = []
    for day in sorted(set(old_days) | set(new_days)):
        before = list(old_days.get(day, []))
        after = list(new_days.get(day, []))
        # Совпадающие полностью — убираем.
        for lesson in list(after):
            twin = next((b for b in before if b["start"] == lesson["start"] and b["title"] == lesson["title"]
                         and b.get("location", "") == lesson.get("location", "")), None)
            if twin:
                before.remove(twin)
                after.remove(lesson)
        label = _day_label(day, today)
        # Тот же предмет, но другое время или аудитория — перенос.
        for lesson in list(after):
            twin = next((b for b in before if b["title"] == lesson["title"]), None)
            if not twin:
                continue
            before.remove(twin)
            after.remove(lesson)
            if twin["start"] != lesson["start"]:
                lines.append(f"🔀 {label}: {lesson['title']} перенесли {twin['start'][11:16]} → {lesson['start'][11:16]}")
            else:
                lines.append(f"📍 {label} {lesson['start'][11:16]}: {lesson['title']} — теперь {lesson.get('location') or 'без аудитории'}")
        lines += [f"❌ {label} {b['start'][11:16]}: {b['title']} — отменили" for b in before]
        lines += [f"➕ {label} {a['start'][11:16]}: {a['title']}" + (f" ({a['location']})" if a.get("location") else "")
                  for a in after]
    return lines


def github_changes(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    lines = []
    for key, title in (("review_requests", "👀 Просят ревью"), ("assigned", "📌 Назначили на тебя")):
        seen = {i["url"] for i in old.get(key) or []}
        for item in new.get(key) or []:
            if item["url"] not in seen:
                lines.append(f"{title}: {item['repo']}#{item['number']} {item['title']}\n{item['url']}")
    return lines


def receipt_changes(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    seen = {r["uid"] for r in old.get("receipts") or []}
    lines = []
    for receipt in new.get("receipts") or []:
        if receipt["uid"] not in seen:
            amount = f" — {receipt['amount']} {receipt['currency']}" if receipt.get("amount") else ""
            lines.append(f"🧾 {receipt['subject']}{amount}")
    return lines


def subscription_alerts(new: dict[str, Any]) -> list[tuple[str, str]]:
    """(ключ, текст) для подписок, которые спишутся в ближайшие 3 дня — одно напоминание на списание."""
    out = []
    for sub in new.get("items") or []:
        days_left = sub.get("days_left")
        if days_left is None or not 0 <= days_left <= 3:
            continue
        amount = f" {sub['amount']} {sub['currency']}" if sub.get("amount") else ""
        when = "сегодня" if days_left == 0 else "завтра" if days_left == 1 else f"через {days_left} дн."
        out.append((f"sub:{sub['name']}:{sub['next_charge']}",
                    f"💳 {when} спишется {sub['name']}{amount}. Если подписка не нужна — самое время отменить."))
    return out


def server_changes(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Новые/закрытые публичные порты и упавшие контейнеры."""
    lines = []
    old_ports = {(p["proto"], p["port"]): p for p in old.get("ports") or [] if p.get("public")}
    new_ports = {(p["proto"], p["port"]): p for p in new.get("ports") or [] if p.get("public")}
    for key in sorted(set(new_ports) - set(old_ports)):
        p = new_ports[key]
        who = p.get("container") or p.get("process") or "неизвестный процесс"
        lines.append(f"🔓 Открылся публичный порт {p['port']}/{p['proto']} — {who}")
    for key in sorted(set(old_ports) - set(new_ports)):
        p = old_ports[key]
        lines.append(f"🔒 Закрылся порт {p['port']}/{p['proto']} ({p.get('container') or p.get('process') or '?'})")
    old_state = {c["name"]: c["state"] for c in old.get("containers") or []}
    for c in new.get("containers") or []:
        was = old_state.get(c["name"])
        if was == "running" and c["state"] != "running":
            lines.append(f"🛑 Контейнер {c['name']} остановился: {c['status']}")
        elif was and was != "running" and c["state"] == "running":
            lines.append(f"▶️ Контейнер {c['name']} снова работает")
    return lines


def disk_alerts(status: dict[str, Any], threshold: int, today: date) -> list[tuple[str, str]]:
    out = []
    for disk in status.get("disks") or []:
        if disk["percent"] >= threshold:
            free_gb = disk["free"] / 1024 ** 3
            out.append((f"disk:{disk['mount']}:{today.isoformat()}",
                        f"💾 Диск {disk['mount']} заполнен на {disk['percent']}% — свободно {free_gb:.1f} ГБ"))
    return out


def project_changes(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[str]:
    lines = []
    before = {p["path"]: p for p in old}
    for project in new:
        was = before.get(project["path"])
        if was is None:
            continue
        if (project.get("behind") or 0) > 0 and not (was.get("behind") or 0):
            upstream = project.get("upstream") or {}
            lines.append(f"⬆️ Для {project['name']} есть обновление ({project['behind']} коммит.): "
                         f"{upstream.get('message', '')}\nОбновить можно в разделе «Сервер».")
        ci, old_ci = project.get("ci") or {}, was.get("ci") or {}
        if ci.get("conclusion") == "failure" and ci.get("id") != old_ci.get("id"):
            lines.append(f"❌ CI упал в {project['github']} ({ci.get('name')}, {ci.get('sha')})\n{ci.get('url')}")
        elif ci.get("conclusion") == "success" and old_ci.get("conclusion") == "failure":
            lines.append(f"✅ CI в {project['github']} снова зелёный")
    return lines


def health_changes(before: dict[str, dict[str, Any]], errors: dict[str, str | None]) -> list[str]:
    lines = []
    for source, error in errors.items():
        if source not in SOURCE_NAMES:
            continue
        was = before.get(source) or {}
        name = SOURCE_NAMES[source]
        if error and not was.get("error"):
            lines.append(f"⚠️ Не получилось обновить {name}: {error[:300]}")
        elif not error and was.get("error"):
            lines.append(f"✅ {name[:1].upper() + name[1:]} снова обновляется")
    return lines


def after_sync(storage: Storage, settings: dict[str, Any], before: dict[str, dict[str, Any]],
               errors: dict[str, str | None], today: date, send: Callable[[str], None]) -> list[str]:
    """Собирает все изменения после синхронизации и отправляет одним-двумя сообщениями."""
    after = {source: storage.get_snapshot(source) for source in before}
    blocks: list[str] = []

    def had(source: str) -> bool:  # первая успешная загрузка источника — не уведомляем
        return bool(before[source]["updated_at"]) and before[source]["data"] is not None

    if settings.get("notify_schedule") and had("schedule") and not errors.get("schedule"):
        old_today = date.fromisoformat(before["schedule"]["updated_at"][:10])
        lines = schedule_changes((before["schedule"]["data"] or {}).get("lessons", []),
                                 (after["schedule"]["data"] or {}).get("lessons", []), today, old_today)
        if lines:
            blocks.append("📅 Изменения в расписании:\n" + "\n".join(lines[:20]))

    if settings.get("notify_github") and had("github") and not errors.get("github"):
        blocks += github_changes(before["github"]["data"] or {}, after["github"]["data"] or {})

    if settings.get("notify_money"):
        if settings.get("notify_receipts") and had("subscriptions"):
            lines = receipt_changes(before["subscriptions"]["data"] or {}, after["subscriptions"]["data"] or {})
            if lines:
                blocks.append("Новые чеки:\n" + "\n".join(lines[:10]))
        for key, text in subscription_alerts(after["subscriptions"]["data"] or {}):
            if storage.first_time(f"notify:{key}"):
                blocks.append(text)

    if settings.get("notify_server"):
        if had("server") and not errors.get("server"):
            blocks += server_changes(before["server"]["data"] or {}, after["server"]["data"] or {})
        if not errors.get("server") and after["server"]["data"]:
            threshold = int(settings.get("server_disk_alert") or 90)
            for key, text in disk_alerts(after["server"]["data"], threshold, today):
                if storage.first_time(f"notify:{key}"):
                    blocks.append(text)
        if had("server_projects") and not errors.get("server_projects"):
            blocks += project_changes((before["server_projects"]["data"] or {}).get("projects", []),
                                      (after["server_projects"]["data"] or {}).get("projects", []))

    if settings.get("notify_errors"):
        blocks += health_changes(before, errors)

    if blocks:
        message = "\n\n".join(blocks)
        try:
            send(message)
        except Exception:
            log.exception("Не удалось отправить уведомление об изменениях")
    return blocks


def version_message(last_seen: str | None) -> str | None:
    """Сообщение «DailyTimer обновлён» со списком нового с прошлой версии (или None)."""
    if last_seen == __version__:
        return None

    def key(version: str) -> tuple[int, ...]:
        return tuple(int(x) for x in version.split(".") if x.isdigit())

    fresh = [(v, items) for v, items in CHANGES if not last_seen or key(v) > key(last_seen)]
    if not fresh:
        return None
    if not last_seen:
        fresh = fresh[:1]  # при первой установке — только текущая версия
    lines = [f"🚀 DailyTimer обновлён до версии {__version__}"]
    for version, items in fresh[:3]:
        lines.append(f"\nЧто нового в {version}:")
        lines += [f"• {item}" for item in items]
    return "\n".join(lines)


def announce_version(storage: Storage, send: Callable[[str], bool]) -> None:
    """Вызывается при старте: если версия новая и уведомление ушло — запоминаем её."""
    state = storage.get_snapshot("app_version")["data"] or {}
    message = version_message(state.get("version"))
    if message is None:
        return
    if not storage.get_settings().get("notify_app_updates") or send(message):
        storage.save_snapshot("app_version", {"version": __version__})
