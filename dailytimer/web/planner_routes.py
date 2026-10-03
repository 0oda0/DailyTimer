"""Страницы планера: задачи, матрица, чат с ИИ, привычки, фокус, итоги, календарь для телефона."""

from __future__ import annotations

import hashlib
import secrets
import threading
from datetime import date, datetime, timedelta
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from .. import sync
from ..ai import AIClient, AIError
from ..assistant import Assistant
from ..life import MOODS, Focus, Habits, Journal, week_stats
from ..storage import Storage
from ..tasks import PRIORITY_NAMES, Tasks, auto_schedule, parse_quick, timeline


def _back(request: Request, default: str = "/tasks") -> RedirectResponse:
    target = request.headers.get("referer") or default
    return RedirectResponse(target, status_code=303)


def register(app: FastAPI, storage: Storage, templates: Jinja2Templates, auth: Callable[..., Any],
             render_markdown: Callable[[str], str]) -> None:
    tasks = Tasks(storage)
    habits = Habits(storage)
    focus = Focus(storage)
    journal = Journal(storage)
    assistant = Assistant(storage)
    guard = [Depends(auth)]

    def today() -> date:
        return sync.today_for(storage.get_settings())

    # ------------------------------------------------------------ задачи

    @app.get("/tasks", response_class=HTMLResponse, dependencies=guard)
    def tasks_page(request: Request, view: str = "list", tag: str = "") -> Any:
        day = today()
        views = tasks.views(day)
        if tag:
            views = {k: [t for t in v if tag in t["tag_list"]] for k, v in views.items()}
        all_tags = sorted({tg for t in tasks.open_tasks() for tg in (t["tags"] or "").split(",") if tg})
        return templates.TemplateResponse(request, "tasks.html", {
            "view": view, "views": views, "matrix": tasks.matrix(day), "tags": all_tags, "tag": tag,
            "priorities": PRIORITY_NAMES, "done_today": tasks.done_between(day, day), "today": day,
        })

    @app.post("/tasks/add", dependencies=guard)
    async def task_add(request: Request) -> Any:
        form = await request.form()
        text = str(form.get("text", "")).strip()
        if text:
            extra = {}
            if form.get("source"):
                extra = {"source": str(form["source"]), "source_ref": str(form.get("source_ref", "")),
                         "notes": str(form.get("notes", ""))}
            tasks.add_quick(text, today(), **extra)
        return _back(request)

    @app.post("/tasks/{task_id}/done", dependencies=guard)
    def task_done(request: Request, task_id: int) -> Any:
        tasks.complete(task_id, today())
        return _back(request)

    @app.post("/tasks/{task_id}/undo", dependencies=guard)
    def task_undo(request: Request, task_id: int) -> Any:
        tasks.reopen(task_id)
        return _back(request)

    @app.post("/tasks/{task_id}/delete", dependencies=guard)
    def task_delete(request: Request, task_id: int) -> Any:
        tasks.delete(task_id)
        return _back(request)

    @app.post("/tasks/{task_id}/edit", dependencies=guard)
    async def task_edit(request: Request, task_id: int) -> Any:
        form = await request.form()
        tags = [t.strip().lstrip("#").lower() for t in str(form.get("tags", "")).split(",") if t.strip()]
        tasks.update(
            task_id,
            title=str(form.get("title", "")).strip() or "Без названия",
            notes=str(form.get("notes", "")),
            due_date=str(form.get("due_date", "")) or None,
            due_time=str(form.get("due_time", "")) or None,
            duration=int(form.get("duration") or 30),
            priority=int(form.get("priority") or 4),
            tags=",".join(tags), project=tags[0] if tags else "",
            recur=str(form.get("recur", "")),
            scheduled_start=None,
        )
        return _back(request)

    @app.post("/tasks/{task_id}/tomorrow", dependencies=guard)
    def task_tomorrow(request: Request, task_id: int) -> Any:
        tasks.update(task_id, due_date=(today() + timedelta(days=1)).isoformat(), scheduled_start=None)
        return _back(request)

    @app.post("/tasks/plan", dependencies=guard)
    def tasks_plan(request: Request) -> Any:
        settings = storage.get_settings()
        lessons = (storage.get_snapshot("schedule")["data"] or {}).get("lessons", [])
        auto_schedule(tasks, today(), lessons, settings.get("wake_time") or "08:00",
                      settings.get("sleep_time") or "23:30", now=datetime.now())
        return _back(request, "/")

    @app.get("/api/parse", dependencies=guard)
    def api_parse(text: str) -> Any:
        """Подсказка под полем быстрого ввода: что распознано."""
        return parse_quick(text, today()).as_dict()

    # ------------------------------------------------------------ расписание

    @app.get("/schedule", response_class=HTMLResponse, dependencies=guard)
    def schedule_page(request: Request) -> Any:
        day = today()
        snap = storage.get_snapshot("schedule")
        portal = storage.get_snapshot("portal")
        names = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
        by_day: dict[str, list[dict[str, Any]]] = {}
        for lesson in (snap["data"] or {}).get("lessons", []):
            if lesson["start"][:10] >= day.isoformat():
                by_day.setdefault(lesson["start"][:10], []).append(lesson)
        days = []
        for offset in range(14):
            current = day + timedelta(days=offset)
            iso = current.isoformat()
            days.append({"iso": iso, "date": current.strftime("%d.%m"), "name": names[current.weekday()],
                         "today": offset == 0, "tomorrow": offset == 1, "lessons": by_day.get(iso, []),
                         "week_start": current.weekday() == 0 and offset > 0})
        settings = storage.get_settings()
        return templates.TemplateResponse(request, "schedule.html", {
            "days": days, "updated_at": snap["updated_at"], "error": snap["error"],
            "portal": portal["data"] or {}, "portal_error": portal["error"], "portal_at": portal["updated_at"],
            "configured": bool(settings.get("schedule_login") or settings.get("schedule_ics_url")
                               or settings.get("schedule_manual")),
            "refreshing": sync.schedule_refreshing() or "refreshing" in request.query_params,
            "total": sum(len(d["lessons"]) for d in days),
        })

    @app.post("/schedule/refresh", dependencies=guard)
    def schedule_refresh() -> Any:
        threading.Thread(target=lambda: sync.refresh_schedule(storage), daemon=True).start()
        return RedirectResponse("/schedule?refreshing=1", status_code=303)

    # ------------------------------------------------------------ чат

    @app.get("/chat", response_class=HTMLResponse, dependencies=guard)
    def chat_page(request: Request) -> Any:
        history = assistant.history("web")
        for message in history:
            message["html"] = render_markdown(message["content"])
        return templates.TemplateResponse(request, "chat.html", {"history": history})

    @app.post("/api/chat", dependencies=guard)
    async def api_chat(request: Request) -> Any:
        body = await request.json()
        text = str(body.get("text", ""))[:4000]
        # Ответ ИИ может занять минуты — считаем в отдельном потоке, чтобы сайт не замирал.
        answer = await run_in_threadpool(assistant.reply, text, "web")
        return JSONResponse({"reply": answer, "html": render_markdown(answer)})

    @app.post("/chat/clear", dependencies=guard)
    def chat_clear() -> Any:
        assistant.clear("web")
        return RedirectResponse("/chat", status_code=303)

    # ------------------------------------------------------------ привычки

    @app.get("/habits", response_class=HTMLResponse, dependencies=guard)
    def habits_page(request: Request) -> Any:
        return templates.TemplateResponse(request, "habits.html", {"habits": habits.overview(today())})

    @app.post("/habits/add", dependencies=guard)
    async def habit_add(request: Request) -> Any:
        form = await request.form()
        if str(form.get("name", "")).strip():
            habits.add(str(form["name"]), str(form.get("icon", "✅")), int(form.get("per_week") or 7))
        return _back(request, "/habits")

    @app.post("/habits/{habit_id}/toggle", dependencies=guard)
    async def habit_toggle(request: Request, habit_id: int) -> Any:
        form = await request.form()
        day = date.fromisoformat(str(form["day"])) if form.get("day") else today()
        habits.toggle(habit_id, day)
        return _back(request, "/habits")

    @app.post("/habits/{habit_id}/archive", dependencies=guard)
    def habit_archive(request: Request, habit_id: int) -> Any:
        habits.archive(habit_id)
        return _back(request, "/habits")

    # ------------------------------------------------------------ фокус

    @app.get("/focus", response_class=HTMLResponse, dependencies=guard)
    def focus_page(request: Request, task: int | None = None) -> Any:
        day = today()
        return templates.TemplateResponse(request, "focus.html", {
            "tasks": tasks.views(day)["today"] + tasks.views(day)["overdue"] + tasks.views(day)["inbox"][:10],
            "selected": task, "minutes": focus.minutes_on(day), "sessions": focus.sessions_on(day),
        })

    @app.post("/api/focus", dependencies=guard)
    async def api_focus(request: Request) -> Any:
        body = await request.json()
        task_id = int(body["task_id"]) if body.get("task_id") else None
        focus.log(int(body.get("minutes") or 25), task_id, str(body.get("kind") or "focus"))
        return {"ok": True, "today": focus.minutes_on(today())}

    # ------------------------------------------------------------ итоги

    @app.get("/review", response_class=HTMLResponse, dependencies=guard)
    def review_page(request: Request, day: str = "") -> Any:
        current = date.fromisoformat(day) if day else today()
        review = storage.get_snapshot("weekly_review")["data"] or {}
        return templates.TemplateResponse(request, "review.html", {
            "day": current, "entry": journal.get(current), "moods": MOODS, "stats": week_stats(storage, current),
            "done": tasks.done_between(current - timedelta(days=6), current), "recent": journal.recent(),
            "review_html": render_markdown(review.get("text", "")), "review_at": review.get("at"),
            "review_pending": bool(review.get("pending")),
            "left_today": tasks.views(current)["today"] + tasks.views(current)["overdue"],
        })

    @app.post("/review/save", dependencies=guard)
    async def review_save(request: Request) -> Any:
        form = await request.form()
        day = date.fromisoformat(str(form.get("day") or today().isoformat()))
        mood = int(form["mood"]) if form.get("mood") else None
        journal.save(day, mood, str(form.get("wins", "")), str(form.get("notes", "")), str(form.get("tomorrow", "")))
        # «На завтра» — каждая строка становится задачей на завтра (ритуал «закрыть день»).
        if form.get("make_tasks"):
            for line in str(form.get("tomorrow", "")).splitlines():
                if line.strip():
                    parsed = parse_quick(line, day)
                    if parsed.due_date in (None, day.isoformat()):  # «в 10» в планах на завтра — это завтра
                        parsed.due_date = (day + timedelta(days=1)).isoformat()
                    tasks.add(**parsed.as_dict())
        if form.get("move_left"):
            for task in tasks.views(day)["today"] + tasks.views(day)["overdue"]:
                tasks.update(task["id"], due_date=(day + timedelta(days=1)).isoformat(), scheduled_start=None)
        return RedirectResponse(f"/review?day={day.isoformat()}&saved=1", status_code=303)

    review_lock = threading.Lock()

    @app.post("/review/ai", dependencies=guard)
    def review_ai() -> Any:
        def work() -> None:
            if not review_lock.acquire(blocking=False):
                return
            try:
                _weekly_review()
            finally:
                review_lock.release()

        storage.save_snapshot("weekly_review", {**(storage.get_snapshot("weekly_review")["data"] or {}),
                                                "pending": True})
        threading.Thread(target=work, daemon=True).start()
        return RedirectResponse("/review", status_code=303)

    def _weekly_review() -> None:
        day = today()
        ai = AIClient.from_settings(storage.get_settings())
        stats = week_stats(storage, day)
        done = tasks.done_between(day - timedelta(days=6), day)
        notes = journal.recent(7)
        prompt = (
            f"Неделя до {day.isoformat()}. Выполнено задач: {stats['total_done']}, фокус: {stats['total_focus']} мин.\n"
            "Сделано: " + "; ".join(t["title"] for t in done[:30]) + "\n"
            "Дневник: " + " | ".join(f"{n['day']}: настроение {n['mood']}, {n['wins']} {n['notes']}" for n in notes) + "\n"
            "Открытые задачи: " + "; ".join(t["title"] for t in tasks.open_tasks()[:30])
        )
        if ai is None:
            text = "ИИ выключен — включи его в Подключения → ИИ."
        else:
            try:
                text = ai.chat("Ты коуч по продуктивности. Сделай итоги недели по-русски в Markdown: "
                               "## Что получилось, ## Что мешало, ## 3 фокуса на следующую неделю. Коротко и конкретно.",
                               prompt, temperature=0.5)
            except AIError as exc:
                text = f"ИИ недоступен: {exc}"
        storage.save_snapshot("weekly_review", {"text": text, "at": datetime.now().isoformat(timespec="minutes")})

    # ------------------------------------------------------------ календарь для телефона

    def calendar_token() -> str:
        token = storage.get_settings().get("calendar_token")
        if not token:
            token = secrets.token_urlsafe(24)
            storage.save_settings({"calendar_token": token})
        return token

    @app.get("/calendar/link", response_class=PlainTextResponse, dependencies=guard)
    def calendar_link(request: Request) -> str:
        base = str(request.base_url).rstrip("/")
        return f"{base}/calendar.ics?token={calendar_token()}"

    @app.get("/calendar.ics")
    def calendar_feed(token: str = "") -> Response:
        if not token or not secrets.compare_digest(token, calendar_token()):
            raise HTTPException(status.HTTP_403_FORBIDDEN)
        return Response(build_ics(storage, tasks), media_type="text/calendar; charset=utf-8")

    # ------------------------------------------------------------ PWA

    @app.get("/manifest.webmanifest")
    def manifest() -> Any:
        return JSONResponse({
            "name": "DailyTimer", "short_name": "DailyTimer", "start_url": "/", "display": "standalone",
            "background_color": "#111318", "theme_color": "#3b5bdb", "lang": "ru",
            "icons": [{"src": "/static/icon.svg", "sizes": "any", "type": "image/svg+xml"}],
        }, media_type="application/manifest+json")


def _ics_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def build_ics(storage: Storage, tasks: Tasks) -> str:
    """Пары и задачи со временем — в формате iCalendar для подписки с телефона."""
    tz = storage.get_settings().get("timezone") or "Europe/Moscow"
    stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//DailyTimer//RU", "X-WR-CALNAME:DailyTimer",
             f"X-WR-TIMEZONE:{tz}"]

    def event(uid: str, start: datetime, end: datetime, summary: str, location: str = "", alarm: int = 0) -> None:
        lines.extend([
            "BEGIN:VEVENT", f"UID:{uid}@dailytimer", f"DTSTAMP:{stamp}",
            f"DTSTART;TZID={tz}:{start.strftime('%Y%m%dT%H%M%S')}",
            f"DTEND;TZID={tz}:{end.strftime('%Y%m%dT%H%M%S')}", f"SUMMARY:{_ics_escape(summary)}",
        ])
        if location:
            lines.append(f"LOCATION:{_ics_escape(location)}")
        if alarm:
            lines.extend(["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_ics_escape(summary)}",
                          f"TRIGGER:-PT{alarm}M", "END:VALARM"])
        lines.append("END:VEVENT")

    for lesson in (storage.get_snapshot("schedule")["data"] or {}).get("lessons", []):
        start, end = datetime.fromisoformat(lesson["start"]), datetime.fromisoformat(lesson["end"])
        digest = hashlib.md5(lesson["title"].encode()).hexdigest()[:10]
        event(f"lesson-{start:%Y%m%d%H%M}-{digest}", start, end,
              "🎓 " + lesson["title"], lesson.get("location", ""), alarm=15)
    for task in tasks.open_tasks():
        start = None
        if task["due_date"] and task["due_time"]:
            start = datetime.fromisoformat(f"{task['due_date']}T{task['due_time']}")
        elif task["scheduled_start"]:
            start = datetime.fromisoformat(task["scheduled_start"])
        if start:
            event(f"task-{task['id']}", start, start + timedelta(minutes=task["duration"]), "✅ " + task["title"],
                  alarm=10 if task["remind"] else 0)
        elif task["due_date"]:
            day = date.fromisoformat(task["due_date"])
            lines += ["BEGIN:VEVENT", f"UID:task-{task['id']}@dailytimer", f"DTSTAMP:{stamp}",
                      f"DTSTART;VALUE=DATE:{day:%Y%m%d}", f"DTEND;VALUE=DATE:{day + timedelta(days=1):%Y%m%d}",
                      f"SUMMARY:{_ics_escape('✅ ' + task['title'])}", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
