"""Оркестратор: опрашивает сервисы, наводит порядок в почте, строит план, шлёт уведомления."""

from __future__ import annotations

import hashlib
import os
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from . import notifications, planner, sorter
from .ai import AIClient, AIError
from .connectors import feeds, github, gmail, mtuci, portal, schedule, subscriptions, telegram, tg_account, vps
from .storage import Storage

log = logging.getLogger(__name__)
SOURCES = ("github", "gmail", "telegram", "schedule", "portal", "subscriptions", "weather", "feeds", "codeforces",
           "server", "server_projects")
_sync_lock = threading.Lock()


def today_for(settings: dict[str, Any]):
    return datetime.now(ZoneInfo(settings.get("timezone") or "Europe/Moscow")).date()


def is_stale(storage: Storage, source: str, hours: float) -> bool:
    snap = storage.get_snapshot(source)
    if not snap["updated_at"] or snap["error"]:
        return True
    updated = datetime.fromisoformat(snap["updated_at"])
    return datetime.now(timezone.utc) - updated > timedelta(hours=hours)


def notify(storage: Storage, settings: dict[str, Any], text: str) -> bool:
    """Отправляет сообщение владельцу в Telegram. True — если ушло."""
    token, chat_id = settings.get("telegram_bot_token"), settings.get("telegram_chat_id")
    if not token:
        return False
    try:
        if not chat_id:
            chat_id = telegram.discover_chat_id(token)
            if not chat_id:
                return False
            storage.save_settings({"telegram_chat_id": chat_id})
            settings["telegram_chat_id"] = chat_id
        telegram.send(token, chat_id, text)
        return True
    except telegram.TelegramError as exc:
        log.warning("Telegram: %s", exc)
        return False


# ------------------------------------------------------------------ почта

def _rescue_spam(client: gmail.GmailClient, storage: Storage, ai: AIClient | None,
                 trash_rest: bool = False) -> list[dict[str, Any]]:
    if "\\Junk" not in client.folders:
        return []
    client.select("spam")
    try:
        uids = client.search_since(7, limit=40)
        known = storage.mail_analysis([f"spam:{u}" for u in uids])
        fresh = list(client.fetch([u for u in uids if f"spam:{u}" not in known]))
        verdicts = sorter.analyze(fresh, ai)
        rescued = []
        for mail in fresh:
            verdict = verdicts[mail["uid"]]
            rescue = sorter.should_rescue_from_spam(mail, verdict)
            if rescue:
                client.ensure_label("DT/Rescued")
                client.add_label(mail["uid"], "DT/Rescued")
                if client.move_to_inbox(mail["uid"]):
                    rescued.append({**mail, **verdict, "rescued": True})
            storage.save_mail_analysis(f"spam:{mail['uid']}", {**verdict, "rescued": rescue})
        if trash_rest:  # проверенный и не важный спам — сразу в корзину
            junk = [m["uid"] for m in fresh if not sorter.should_rescue_from_spam(m, verdicts[m["uid"]])]
            if junk:
                client.trash(junk)
        return rescued
    finally:
        client.select("all")


def _apply_actions(client: gmail.GmailClient, mail: dict[str, Any], verdict: dict[str, Any],
                   settings: dict[str, Any], storage: Storage) -> dict[str, Any]:
    category = verdict["category"]
    uid = mail["uid"]
    actions = []
    if settings.get("gmail_apply_labels"):
        client.add_label(uid, sorter.CATEGORIES[category]["label"])
    keep = verdict["important"] or verdict["needs_reply"]
    if category == "receipts":
        amount = subscriptions.find_amount(f"{mail['subject']} {mail['snippet']}")
        storage.add_receipt(uid, (mail.get("date") or "")[:10], mail["from"], mail["subject"],
                            amount[0] if amount else None, amount[1] if amount else None)
        if settings.get("gmail_receipts_archive") and mail.get("in_inbox") and not keep:
            client.archive(uid)
            client.mark_read(uid)
            actions.append("в чеки")
    elif category == "promo" and not keep and settings.get("gmail_trash_promo"):
        client.trash([uid])
        actions.append("удалено")
    elif category in sorter.JUNK and not keep and settings.get("gmail_cleanup") and mail.get("in_inbox"):
        client.archive(uid)
        client.mark_read(uid)
        actions.append("убрано")
    return {**verdict, "actions": actions}


def sync_gmail(storage: Storage, settings: dict[str, Any], ai: AIClient | None) -> list[dict[str, Any]] | None:
    """Возвращает письма-кандидаты в подписки (или None, если их не обновляли)."""
    with gmail.GmailClient(settings["gmail_email"], settings["gmail_app_password"]) as client:
        rescued = (_rescue_spam(client, storage, ai, trash_rest=bool(settings.get("gmail_trash_spam")))
                   if settings.get("gmail_rescue_spam") else [])

        mails = list(client.fetch(client.search("newer_than:3d -in:sent -in:chats", limit=80)))
        known = storage.mail_analysis([m["uid"] for m in mails])
        fresh = [m for m in mails if m["uid"] not in known]
        verdicts = sorter.analyze(fresh, ai)
        if settings.get("gmail_apply_labels"):
            for label in {sorter.CATEGORIES[v["category"]]["label"] for v in verdicts.values()}:
                client.ensure_label(label)
        new_alerts = []
        for mail in fresh:
            analysis = _apply_actions(client, mail, verdicts[mail["uid"]], settings, storage)
            storage.save_mail_analysis(mail["uid"], analysis)
            known[mail["uid"]] = analysis
            if analysis["actions"]:
                mail["in_inbox"] = False
            if analysis["important"] or analysis["needs_reply"]:
                new_alerts.append({**mail, **analysis})
        for mail in mails:
            mail.update(known.get(mail["uid"], {"category": "other"}))
            mail["snippet"] = mail["snippet"][:300]
        mails.sort(key=lambda m: m.get("date") or "", reverse=True)

        # Раз в 6 часов — поиск чеков и подписок за полтора месяца (чтобы собрать их в DT/Receipts).
        sub_mails = None
        now = datetime.now()
        if storage.first_time(f"subscan:{now:%Y-%m-%d}:{now.hour // 6}"):
            sub_mails = list(client.fetch(client.search(subscriptions.GMAIL_QUERY, limit=80)))
            done = storage.mail_analysis([m["uid"] for m in sub_mails])
            for mail in sub_mails:
                if mail["uid"] in done:
                    continue
                verdict = sorter.rule_analysis(mail)
                if verdict["category"] == "receipts":
                    client.ensure_label(sorter.CATEGORIES["receipts"]["label"])
                    storage.save_mail_analysis(mail["uid"], _apply_actions(client, mail, verdict, settings, storage))

    previous = storage.get_snapshot("gmail")["data"] or {}
    digest = previous.get("digest", "")
    attention = [m for m in mails if m.get("important") or m.get("needs_reply")]
    fingerprint = hashlib.sha1("|".join(m["uid"] for m in attention).encode()).hexdigest()
    if ai and attention and fingerprint != previous.get("digest_key"):
        digest = _mail_digest(ai, attention)
    storage.save_snapshot("gmail", {
        "messages": mails,
        "rescued": rescued + [r for r in previous.get("rescued", []) if r["uid"] not in {x["uid"] for x in rescued}][:20],
        "digest": digest if attention else "",
        "digest_key": fingerprint,
    })

    if settings.get("telegram_notify_mail"):
        for mail in new_alerts + rescued:
            if not storage.first_time(f"mail:{mail.get('rescued', False)}:{mail['uid']}:{mail['subject']}"):
                continue
            tag = "🛟 Вытащено из спама" if mail.get("rescued") else ("✉️ Ждёт ответа" if mail.get("needs_reply") else "❗ Важное")
            notify(storage, settings, f"{tag}\n{mail['from']}\n{mail['subject']}\n— {mail.get('summary', '')}")
    return sub_mails


def _mail_digest(ai: AIClient, attention: list[dict[str, Any]]) -> str:
    lines = [
        f"- {'[ждёт ответа] ' if m.get('needs_reply') else ''}{m['from']}: {m['subject']} — {m.get('summary', '')}"
        for m in attention[:15]
    ]
    try:
        return ai.chat(
            "Сделай очень краткую сводку почты на русском (2–4 предложения): что важно и на что ответить. "
            "Без приветствий и воды.",
            "\n".join(lines),
            temperature=0.2,
        )
    except AIError as exc:
        log.warning("Сводка почты не получилась: %s", exc)
        return ""


def run_purge(storage: Storage, kinds: list[str] | None = None) -> dict[str, Any]:
    """Генеральная уборка почты (вызывается кнопкой, при первом подключении и раз в неделю)."""
    from . import mail_cleanup

    settings = storage.get_settings()
    if not (settings.get("gmail_email") and settings.get("gmail_app_password")):
        return {"error": "Gmail не подключён"}
    return mail_cleanup.purge(storage, settings, kinds or mail_cleanup.selected_kinds(settings),
                              AIClient.from_settings(settings), lambda text: notify(storage, settings, text))


def run_triage(storage: Storage) -> dict[str, Any]:
    """Полный разбор всего ящика (мусор, чеки, оповещения, остальные «Входящие»)."""
    from . import mail_cleanup

    settings = storage.get_settings()
    if not (settings.get("gmail_email") and settings.get("gmail_app_password")):
        return {"error": "Gmail не подключён"}
    return mail_cleanup.triage(storage, settings, AIClient.from_settings(settings),
                               lambda text: notify(storage, settings, text))


def maybe_first_purge(storage: Storage) -> None:
    """Первый полный разбор ящика — сразу после подключения почты (и один раз после обновления)."""
    settings = storage.get_settings()
    if not (settings.get("gmail_auto_purge") and settings.get("gmail_email") and settings.get("gmail_app_password")):
        return
    if storage.first_time("mail-triage-v1"):
        run_triage(storage)


# ------------------------------------------------------------------ расписание

def build_schedule(storage: Storage, settings: dict[str, Any], today) -> None:
    """Пары из кабинета вуза + ICS + ручная таблица → один снимок «schedule»."""
    data = schedule.fetch(settings, today, days=14)
    portal_lessons = (storage.get_snapshot("portal")["data"] or {}).get("lessons", [])
    lessons = sorted(data["lessons"] + portal_lessons, key=lambda l: l["start"])
    storage.save_snapshot("schedule", {"lessons": lessons}, error="; ".join(data["errors"]) or None)


_schedule_lock = threading.Lock()


def refresh_schedule(storage: Storage) -> None:
    """Перечитать расписание прямо сейчас (кнопка «Обновить из ЛК»), не трогая почту и остальное."""
    if not _schedule_lock.acquire(blocking=False):
        return
    try:
        settings = storage.get_settings()
        today = today_for(settings)
        if settings.get("schedule_login") and (settings.get("schedule_portal_url") or settings.get("schedule_page_url")):
            state = str(storage.data_dir / "portal_session.json")
            try:
                storage.save_snapshot("portal", fetch_portal(settings, today, AIClient.from_settings(settings), state))
            except Exception as exc:  # noqa: BLE001 — ошибка видна на странице
                log.warning("Расписание из кабинета: %s", exc)
                storage.save_snapshot("portal", None, error=str(exc))
        build_schedule(storage, settings, today)
    finally:
        _schedule_lock.release()


def schedule_refreshing() -> bool:
    return _schedule_lock.locked()


def fetch_portal(settings: dict[str, Any], today, ai: AIClient | None, state: str) -> dict[str, Any]:
    """Загрузка из кабинета с повтором: браузер на слабом сервере иногда падает или не успевает."""
    from .ai import free_local_models, server_ram_gb

    if server_ram_gb() and server_ram_gb() < 6:
        freed = free_local_models(settings.get("ai_local_url") or None)
        if freed:
            log.info("Выгрузил ИИ-модель из памяти перед входом в кабинет (RAM %.1f ГБ)", server_ram_gb())
    last: Exception | None = None
    for attempt in (1, 2):
        try:
            return _fetch_portal_once(settings, today, ai, state)
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
            if "логин" in text or "пароль" in text or "не пустил" in text:
                raise  # неверные данные — повтор не поможет
            log.warning("Кабинет, попытка %s: %s: %s", attempt, type(exc).__name__, text)
            last = exc
            if attempt == 1 and state and os.path.exists(state):
                os.remove(state)  # сохранённая сессия могла протухнуть — войдём заново
    raise RuntimeError(f"Кабинет не загрузился: {type(last).__name__}: {str(last)[:300]}") from last


def _fetch_portal_once(settings: dict[str, Any], today, ai: AIClient | None, state: str) -> dict[str, Any]:
    """Для известных вузов — точный API, для остальных — универсальный разбор страницы."""
    url = settings.get("schedule_portal_url") or settings.get("schedule_page_url") or ""
    if mtuci.is_mtuci(url):
        try:
            return mtuci.fetch(settings, today, days=14, state_file=state)
        except mtuci.MtuciError as exc:
            if "логин" in str(exc) or "пароль" in str(exc):
                raise
            log.warning("API МТУСИ не сработал, пробую разбор страницы: %s", exc)
            fallback = dict(settings, schedule_page_url=settings.get("schedule_page_url") or mtuci.BASE + "/student/schedule")
            return portal.fetch(fallback, today, ai, state)
    return portal.fetch(settings, today, ai, state)


# ------------------------------------------------------------------ сервер

def refresh_projects(storage: Storage, settings: dict[str, Any], agent: "vps.Agent", fetch: bool) -> None:
    projects = agent.projects(fetch=fetch)
    for project in projects:
        if project.get("github"):
            project.update(vps.github_status(project["github"], project.get("branch") or "",
                                             settings.get("github_token", "")))
    storage.save_snapshot("server_projects", {"projects": projects})


def check_projects(storage: Storage, notify_changes: bool = True) -> None:
    """Проверяет сайты проектов из раздела «Проекты»; о падении и восстановлении пишет в Telegram."""
    from .projects import Projects, health_message

    changes = Projects(storage).check_health()
    if notify_changes and changes:
        notify(storage, storage.get_settings(), health_message(changes))


# ------------------------------------------------------------------ всё вместе

def sync_all(storage: Storage, force: bool = False) -> dict[str, str | None]:
    if not _sync_lock.acquire(blocking=False):
        return {"status": "уже идёт синхронизация"}
    try:
        settings = storage.get_settings()
        today = today_for(settings)
        ai = AIClient.from_settings(settings)
        errors: dict[str, str | None] = {}
        before = {source: storage.get_snapshot(source) for source in SOURCES}

        def run(source: str, fn: Callable[[], Any]) -> Any:
            try:
                result = fn()
                errors[source] = None
                return result
            except Exception as exc:  # один упавший сервис не должен ломать остальные
                log.warning("Синхронизация %s: %s", source, exc)
                storage.save_snapshot(source, None, error=str(exc))
                errors[source] = str(exc)
                return None

        if settings.get("tg_session"):
            run("telegram", lambda: storage.save_snapshot("telegram", tg_account.fetch(
                settings["tg_api_id"], settings["tg_api_hash"], settings["tg_session"])))

        if settings.get("github_token"):
            run("github", lambda: storage.save_snapshot("github", github.fetch(settings["github_token"])))

        portal_hours = float(settings.get("schedule_refresh_hours") or 6)
        if settings.get("schedule_login") and (settings.get("schedule_portal_url") or settings.get("schedule_page_url")):
            if force or is_stale(storage, "portal", portal_hours):
                state = str(storage.data_dir / "portal_session.json")
                run("portal", lambda: storage.save_snapshot("portal", fetch_portal(settings, today, ai, state)))

        run("schedule", lambda: build_schedule(storage, settings, today))

        sub_mails = None
        if settings.get("gmail_email") and settings.get("gmail_app_password"):
            sub_mails = run("gmail", lambda: sync_gmail(storage, settings, ai))

        def subs() -> None:
            previous = storage.get_snapshot("subscriptions")["data"] or {}
            if sub_mails is None:  # не обновляли — оставляем найденное раньше
                detected = [s for s in previous.get("items", []) if s.get("source") == "gmail"]
            else:
                detected = subscriptions.from_emails(sub_mails)
            manual = subscriptions.parse_manual(settings.get("subscriptions_manual", ""), today)
            month_start = today.replace(day=1).isoformat()
            storage.save_snapshot("subscriptions", {
                "items": subscriptions.merge(manual, detected, today),
                "receipts": storage.receipts(month_start),
            })

        run("subscriptions", subs)

        if settings.get("weather_city") and (force or is_stale(storage, "weather", 1)):
            run("weather", lambda: storage.save_snapshot(
                "weather", feeds.weather(settings["weather_city"], settings.get("timezone") or "Europe/Moscow")))
        rss = [u.strip() for u in (settings.get("rss_feeds") or "").splitlines() if u.strip()]
        if rss and (force or is_stale(storage, "feeds", 1)):
            run("feeds", lambda: storage.save_snapshot("feeds", {"feeds": feeds.feeds(rss)}))
        agent = vps.Agent.from_settings(settings)
        if agent:
            run("server", lambda: storage.save_snapshot("server", agent.status()))
            if force or is_stale(storage, "server_projects", float(settings.get("server_check_hours") or 1)):
                run("server_projects", lambda: refresh_projects(storage, settings, agent, fetch=True))
        if settings.get("codeforces") and (force or is_stale(storage, "codeforces", 6)):
            run("codeforces", lambda: storage.save_snapshot("codeforces", {"contests": feeds.codeforces_contests()}))
        try:
            if settings.get("telegram_bot_token"):
                notifications.after_sync(storage, settings, before, errors, today,
                                         lambda text: notify(storage, settings, text))
        except Exception:
            log.exception("Ошибка при подготовке уведомлений")
        return errors
    finally:
        _sync_lock.release()


def collect(storage: Storage) -> dict[str, Any]:
    return {source: storage.get_snapshot(source)["data"] for source in SOURCES}


def build_plan(storage: Storage, send: bool = False) -> dict[str, Any]:
    settings = storage.get_settings()
    today = today_for(settings)
    from .life import Habits
    from .tasks import Tasks

    data = collect(storage)
    views = Tasks(storage).views(today)
    data["tasks"] = {k: views[k] for k in ("overdue", "today", "upcoming", "inbox")}
    data["habits"] = Habits(storage).overview(today)
    from .memory import Memory

    data["memory"] = Memory(storage).prompt_block()
    from .projects import Projects

    data["projects"] = Projects(storage).prompt_block()
    content, engine = planner.make_plan(data, settings, today)
    storage.save_plan(today.isoformat(), content, engine)
    if send:
        notify(storage, settings, content)
    return storage.get_plan(today.isoformat()) or {}
