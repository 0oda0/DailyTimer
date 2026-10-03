"""Оркестратор: опрашивает все сервисы, сортирует почту, строит план."""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from . import planner, sorter
from .ai import AIClient
from .connectors import github, gmail, schedule, subscriptions
from .storage import Storage

log = logging.getLogger(__name__)
SOURCES = ("github", "gmail", "schedule", "subscriptions")
_sync_lock = threading.Lock()


def today_for(settings: dict[str, Any]):
    return datetime.now(ZoneInfo(settings.get("timezone") or "Europe/Moscow")).date()


def sync_gmail(storage: Storage, settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Возвращает письма-кандидаты в подписки (для коннектора подписок)."""
    with gmail.GmailClient(settings["gmail_email"], settings["gmail_app_password"]) as client:
        mails = list(client.fetch(client.search("newer_than:3d", limit=60)))
        known = storage.sorted_uids([m["uid"] for m in mails])
        fresh = [m for m in mails if m["uid"] not in known]
        categories = sorter.classify(fresh, AIClient.from_settings(settings))
        if settings.get("gmail_apply_labels") and categories:
            for label in {sorter.CATEGORIES[c]["label"] for c in categories.values()}:
                client.ensure_label(label)
        for mail in fresh:
            category = categories.get(mail["uid"], "other")
            if settings.get("gmail_apply_labels"):
                client.add_label(mail["uid"], sorter.CATEGORIES[category]["label"])
                if settings.get("gmail_archive_promo") and category == "promo":
                    client.archive(mail["uid"])
            storage.mark_sorted(mail["uid"], category)
            known[mail["uid"]] = category
        for mail in mails:
            mail["category"] = known.get(mail["uid"], "other")
        mails.sort(key=lambda m: m.get("date") or "", reverse=True)
        storage.save_snapshot("gmail", {"messages": mails})
        return list(client.fetch(client.search(subscriptions.GMAIL_QUERY, limit=80)))


def sync_all(storage: Storage) -> dict[str, str | None]:
    if not _sync_lock.acquire(blocking=False):
        return {"status": "уже идёт синхронизация"}
    try:
        settings = storage.get_settings()
        today = today_for(settings)
        errors: dict[str, str | None] = {}

        def run(source: str, fn) -> Any:
            try:
                result = fn()
                errors[source] = None
                return result
            except Exception as exc:  # один упавший сервис не должен ломать остальные
                log.warning("Синхронизация %s: %s", source, exc)
                storage.save_snapshot(source, None, error=str(exc))
                errors[source] = str(exc)
                return None

        if settings.get("github_token"):
            run("github", lambda: storage.save_snapshot("github", github.fetch(settings["github_token"])))
        if settings.get("schedule_ics_url") or settings.get("schedule_manual"):
            run("schedule", lambda: storage.save_snapshot("schedule", schedule.fetch(settings, today)))
        sub_mails = None
        if settings.get("gmail_email") and settings.get("gmail_app_password"):
            sub_mails = run("gmail", lambda: sync_gmail(storage, settings))

        def subs() -> None:
            detected = subscriptions.from_emails(sub_mails or [])
            if sub_mails is None:  # почта недоступна — оставляем найденное раньше
                previous = storage.get_snapshot("subscriptions")["data"] or {}
                detected = [s for s in previous.get("items", []) if s.get("source") == "gmail"]
            manual = subscriptions.parse_manual(settings.get("subscriptions_manual", ""), today)
            storage.save_snapshot("subscriptions", {"items": subscriptions.merge(manual, detected, today)})

        run("subscriptions", subs)
        return errors
    finally:
        _sync_lock.release()


def collect(storage: Storage) -> dict[str, Any]:
    return {source: storage.get_snapshot(source)["data"] for source in SOURCES}


def build_plan(storage: Storage) -> dict[str, Any]:
    settings = storage.get_settings()
    today = today_for(settings)
    content, engine = planner.make_plan(collect(storage), settings, today)
    storage.save_plan(today.isoformat(), content, engine)
    return storage.get_plan(today.isoformat()) or {}
