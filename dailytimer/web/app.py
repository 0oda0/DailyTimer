"""Веб-панель: дашборд, настройки, ручной запуск синхронизации и плана."""

from __future__ import annotations

import html
import logging
import os
import re
import secrets
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import bot, notifications, sync
from ..ai import LOCAL_MODELS, AIClient
from ..connectors import tg_account
from ..life import Focus, Habits
from ..tasks import Tasks, timeline
from ..sorter import CATEGORIES
from ..storage import DEFAULT_SETTINGS, SECRET_FIELDS, Storage
from .planner_routes import register as register_planner
from .sections import SECTIONS, section_status

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
BOOL_FIELDS = {k for k, v in DEFAULT_SETTINGS.items() if isinstance(v, bool)}
INT_FIELDS = {k for k, v in DEFAULT_SETTINGS.items() if isinstance(v, int) and not isinstance(v, bool)}


def render_markdown(text: str) -> str:
    """Мини-рендер Markdown для плана: заголовки, списки, жирный, курсив, ссылки, цитаты."""
    out, in_list = [], False
    for raw in text.splitlines():
        line = html.escape(raw)
        line = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", line)
        line = re.sub(r"(?<![\w*])[_*](.+?)[_*](?![\w*])", r"<em>\1</em>", line)
        line = re.sub(r"\[(.+?)\]\((https?://[^)\s]+)\)", r'<a href="\2" target="_blank">\1</a>', line)
        item = re.match(r"^\s*(?:[-*]|\d+\.)\s+(.*)", line)
        if item:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{item.group(1)}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        heading = re.match(r"^(#{1,4})\s+(.*)", line)
        if heading:
            level = min(len(heading.group(1)) + 1, 5)
            out.append(f"<h{level}>{heading.group(2)}</h{level}>")
        elif line.startswith("&gt;"):
            out.append(f"<blockquote>{line[4:].strip()}</blockquote>")
        elif line.strip():
            out.append(f"<p>{line}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def create_app(storage: Storage | None = None, start_scheduler: bool = True) -> FastAPI:
    storage = storage or Storage()
    app = FastAPI(title="DailyTimer", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    security = HTTPBasic(auto_error=False)
    panel_user = os.environ.get("DAILYTIMER_USER", "admin")
    panel_password = os.environ.get("DAILYTIMER_PASSWORD", "")

    def auth(credentials: HTTPBasicCredentials | None = Depends(security)) -> None:
        if not panel_password:
            return  # пароль не задан — режим локального запуска
        if credentials is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Basic"})
        ok_user = secrets.compare_digest(credentials.username.encode(), panel_user.encode())
        ok_pass = secrets.compare_digest(credentials.password.encode(), panel_password.encode())
        if not (ok_user and ok_pass):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Basic"})

    scheduler = BackgroundScheduler()
    poller = bot.BotPoller(storage)

    def reschedule() -> None:
        settings = storage.get_settings()
        tz = settings.get("timezone") or "Europe/Moscow"
        hour, minute = (int(x) for x in (settings.get("plan_time") or "07:00").split(":"))
        scheduler.add_job(
            lambda: sync.sync_all(storage), "interval", id="sync", replace_existing=True,
            minutes=max(5, int(settings.get("sync_interval_minutes") or 15)),
            next_run_time=datetime.now(),
        )

        def morning() -> None:
            sync.sync_all(storage, force=True)
            sync.build_plan(storage, send=True)

        scheduler.add_job(
            morning, CronTrigger(hour=hour, minute=minute, timezone=tz), id="plan", replace_existing=True
        )
        scheduler.add_job(lambda: bot.send_reminders(storage), "interval", minutes=1, id="reminders",
                          replace_existing=True)
        e_hour, e_minute = (int(x) for x in (settings.get("evening_time") or "21:30").split(":"))
        scheduler.add_job(
            lambda: sync.notify(storage, storage.get_settings(), bot.evening_digest(storage)),
            CronTrigger(hour=e_hour, minute=e_minute, timezone=tz), id="evening", replace_existing=True,
        )

    if start_scheduler:
        @app.on_event("startup")
        def _start() -> None:
            reschedule()
            scheduler.start()
            poller.start()
            in_background(lambda: notifications.announce_version(
                storage, lambda text: sync.notify(storage, storage.get_settings(), text)))
            ai = AIClient.from_settings(storage.get_settings())
            if ai and ai.local:  # скачать локальную модель заранее, в фоне
                threading.Thread(target=ai.local.ensure_model, daemon=True).start()

        @app.on_event("shutdown")
        def _stop() -> None:
            scheduler.shutdown(wait=False)
            poller.stop()

    plan_lock = threading.Lock()
    task_store = Tasks(storage)

    def in_background(fn: Any, lock: threading.Lock | None = None) -> None:
        def runner() -> None:
            if lock and not lock.acquire(blocking=False):
                return
            try:
                fn()
            except Exception:
                log.exception("Фоновая задача упала")
            finally:
                if lock:
                    lock.release()

        threading.Thread(target=runner, daemon=True).start()

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def dashboard(request: Request) -> Any:
        settings = storage.get_settings()
        today = sync.today_for(settings)
        snaps = {s: storage.get_snapshot(s) for s in sync.SOURCES}
        plan = storage.get_plan(today.isoformat())
        lessons = (snaps["schedule"]["data"] or {}).get("lessons", [])
        gmail_data = snaps["gmail"]["data"] or {}
        mails = gmail_data.get("messages", [])
        by_cat: dict[str, list] = {}
        for mail in mails:
            by_cat.setdefault(mail.get("category", "other"), []).append(mail)
        subs_data = snaps["subscriptions"]["data"] or {}
        totals: dict[str, float] = {}
        for receipt in subs_data.get("receipts", []):
            if receipt.get("amount"):
                totals[receipt["currency"]] = totals.get(receipt["currency"], 0) + receipt["amount"]
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "today": today,
                "snaps": snaps,
                "plan": plan,
                "plan_html": render_markdown(plan["content"]) if plan else "",
                "busy": sync._sync_lock.locked() or plan_lock.locked() or "busy" in request.query_params,
                "today_lessons": [l for l in lessons if l["start"].startswith(today.isoformat())],
                "upcoming_lessons": [l for l in lessons if l["start"][:10] > today.isoformat()][:12],
                "replies": [m for m in mails if m.get("needs_reply")],
                "important": [m for m in mails if m.get("important") and not m.get("needs_reply")],
                "rescued": gmail_data.get("rescued", []),
                "digest": gmail_data.get("digest", ""),
                "cleaned": sum(1 for m in mails if m.get("actions")),
                "mail_groups": [(k, CATEGORIES[k]["title"], by_cat[k]) for k in CATEGORIES if k in by_cat],
                "receipts": subs_data.get("receipts", []),
                "timeline": timeline(task_store, today, lessons, settings.get("wake_time") or "08:00",
                                     settings.get("sleep_time") or "23:30"),
                "task_views": task_store.views(today),
                "habits": Habits(storage).overview(today),
                "focus_minutes": Focus(storage).minutes_on(today),
                "receipt_totals": totals,
                "configured": any(
                    settings.get(k)
                    for k in ("github_token", "gmail_email", "schedule_ics_url", "schedule_manual", "schedule_login")
                ),
            },
        )

    def masked_settings() -> dict[str, Any]:
        return {k: ("••••••••" if k in SECRET_FIELDS and v else v) for k, v in storage.get_settings().items()}

    def after_change() -> None:
        if not start_scheduler:
            return
        reschedule()
        in_background(lambda: sync.sync_all(storage, force=True))
        ai = AIClient.from_settings(storage.get_settings())
        if ai and ai.local:
            in_background(ai.local.ensure_model)

    def back(section: str, **params: str) -> RedirectResponse:
        query = "&".join(f"{k}={quote(v)}" for k, v in params.items())
        return RedirectResponse(f"/settings/{section}" + (f"?{query}" if query else ""), status_code=303)

    @app.get("/settings", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def settings_overview(request: Request) -> Any:
        settings = storage.get_settings()
        snaps = {src: storage.get_snapshot(src) for src in sync.SOURCES}
        cards = [
            {"key": key, **meta, "status": section_status(key, settings, snaps)} for key, meta in SECTIONS.items()
        ]
        return templates.TemplateResponse(request, "settings_overview.html", {"cards": cards, "sections": SECTIONS})

    @app.get("/settings/{section}", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def settings_section(request: Request, section: str, saved: bool = False, error: str = "", info: str = "") -> Any:
        if section not in SECTIONS:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        snaps = {src: storage.get_snapshot(src) for src in sync.SOURCES}
        return templates.TemplateResponse(
            request,
            "settings.html",
            {"s": masked_settings(), "raw_tg_pending": bool(storage.get_settings().get("tg_pending")),
             "models": LOCAL_MODELS, "saved": saved, "error": error, "info": info,
             "section": section, "sections": SECTIONS, "snaps": snaps,
             "status": section_status(section, storage.get_settings(), snaps)},
        )

    @app.post("/settings/{section}", dependencies=[Depends(auth)])
    async def save_settings(request: Request, section: str) -> Any:
        if section not in SECTIONS:
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        form = await request.form()
        updates: dict[str, Any] = {}
        for key in SECTIONS[section]["fields"]:
            if key in BOOL_FIELDS:  # галочки — только из этого раздела, иначе снимутся чужие
                updates[key] = form.get(key) == "on"
                continue
            if key not in form:
                continue
            value = str(form[key]).strip()
            if key in SECRET_FIELDS and value == "••••••••":
                continue  # секрет не меняли
            if key in INT_FIELDS:
                try:
                    updates[key] = int(value)
                except ValueError:
                    updates[key] = DEFAULT_SETTINGS[key]
            else:
                updates[key] = value
        if "telegram_bot_token" in updates:
            updates["telegram_chat_id"] = ""  # новый бот — chat_id найдём заново по /start
        storage.save_settings(updates)
        after_change()
        return back(section, saved="1")

    @app.post("/telegram/test", dependencies=[Depends(auth)])
    def telegram_test() -> Any:
        sync.notify(storage, storage.get_settings(), "DailyTimer подключён ✅ Сюда будут приходить план дня и срочные письма.")
        return back("social", info="Тестовое сообщение отправлено — проверь Telegram")

    @app.post("/telegram/account/code", dependencies=[Depends(auth)])
    async def telegram_send_code(request: Request) -> Any:
        form = await request.form()
        current = storage.get_settings()
        api_id = str(form.get("tg_api_id", "")).strip() or current.get("tg_api_id", "")
        api_hash = str(form.get("tg_api_hash", "")).strip()
        if not api_hash or api_hash == "••••••••":
            api_hash = current.get("tg_api_hash", "")
        phone = str(form.get("tg_phone", "")).strip().replace(" ", "")
        try:
            pending, code_hash = tg_account.send_code(api_id, api_hash, phone)
        except tg_account.TgError as exc:
            return back("social", error=str(exc))
        storage.save_settings({"tg_api_id": api_id, "tg_api_hash": api_hash, "tg_phone": phone,
                               "tg_pending": f"{pending}|{code_hash}"})
        return back("social", info="Код отправлен в Telegram — введи его ниже")

    @app.post("/telegram/account/login", dependencies=[Depends(auth)])
    async def telegram_login(request: Request) -> Any:
        form = await request.form()
        current = storage.get_settings()
        pending, _, code_hash = (current.get("tg_pending") or "").rpartition("|")
        if not pending:
            return back("social", error="Сначала запроси код")
        try:
            session, name = tg_account.sign_in(
                current["tg_api_id"], current["tg_api_hash"], pending, current["tg_phone"],
                str(form.get("code", "")), code_hash, str(form.get("password", "")),
            )
        except tg_account.TgError as exc:
            return back("social", error=str(exc))
        storage.save_settings({"tg_session": session, "tg_pending": "", "tg_name": name})
        after_change()
        return back("social", info=f"Telegram подключён: {name}")

    @app.post("/telegram/account/logout", dependencies=[Depends(auth)])
    def telegram_logout() -> Any:
        storage.save_settings({"tg_session": "", "tg_pending": "", "tg_name": ""})
        storage.save_snapshot("telegram", {"chats": []})
        return back("social", info="Telegram-аккаунт отключён")

    @app.post("/plan", dependencies=[Depends(auth)])
    def run_plan() -> Any:
        in_background(lambda: sync.build_plan(storage), plan_lock)
        return RedirectResponse("/?busy=1", status_code=303)

    @app.get("/api/state", dependencies=[Depends(auth)])
    def api_state() -> Any:
        today = sync.today_for(storage.get_settings())
        return {"data": sync.collect(storage), "plan": storage.get_plan(today.isoformat())}

    register_planner(app, storage, templates, auth, render_markdown)

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
