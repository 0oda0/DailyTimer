"""Задачи: быстрый ввод на русском, повторы, матрица Эйзенхауэра, авто-раскладка по свободному времени.

Идеи взяты из лучших планеров: быстрый ввод (Todoist), повторы и матрица (TickTick),
авто-планирование в свободные окна (Motion/Reclaim), таймлайн дня (Structured/Sunsama).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from .storage import Storage

WEEKDAYS = {
    "пн": 0, "понедельник": 0, "понедельникам": 0,
    "вт": 1, "вторник": 1, "вторникам": 1,
    "ср": 2, "среда": 2, "среду": 2, "средам": 2,
    "чт": 3, "четверг": 3, "четвергам": 3,
    "пт": 4, "пятница": 4, "пятницу": 4, "пятницам": 4,
    "сб": 5, "суббота": 5, "субботу": 5, "субботам": 5,
    "вс": 6, "воскресенье": 6, "воскресеньям": 6,
}
MONTHS = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6, "июл": 7, "авг": 8,
          "сен": 9, "окт": 10, "ноя": 11, "дек": 12}
PRIORITY_NAMES = {1: "срочно и важно", 2: "важно", 3: "обычный", 4: "без приоритета"}
_WD = "|".join(sorted(WEEKDAYS, key=len, reverse=True))


@dataclass
class Parsed:
    title: str
    due_date: str | None = None
    due_time: str | None = None
    duration: int = 30
    priority: int = 4
    tags: list[str] = field(default_factory=list)
    recur: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"title": self.title, "due_date": self.due_date, "due_time": self.due_time,
                "duration": self.duration, "priority": self.priority, "tags": self.tags, "recur": self.recur}


def _next_weekday(today: date, weekday: int, allow_today: bool = False) -> date:
    delta = (weekday - today.weekday()) % 7
    if delta == 0 and not allow_today:
        delta = 7
    return today + timedelta(days=delta)


def parse_quick(text: str, today: date) -> Parsed:
    """«сдать лабу по сетям завтра в 15:00 1ч !1 #учеба» → поля задачи."""
    s = f" {text.strip()} "
    p = Parsed(title="")

    def cut(pattern: str) -> re.Match[str] | None:
        nonlocal s
        match = re.search(pattern, s, re.I)
        if match:
            s = s[: match.start()] + " " + s[match.end():]
        return match

    # Приоритет: !1..!4, p1..p4, !!!/!!
    if m := cut(r"\s(?:!|p)([1-4])(?=\s)"):
        p.priority = int(m.group(1))
    elif cut(r"\s!!!(?=\s)") or cut(r"\s(срочно)(?=\s)"):
        p.priority = 1
    elif cut(r"\s!!(?=\s)") or cut(r"\s(важно)(?=\s)"):
        p.priority = 2
    # Теги
    while m := cut(r"\s#([\w\-]+)"):
        p.tags.append(m.group(1).lower())
    # Длительность
    if m := cut(r"\s(\d+(?:[.,]\d+)?)\s?(?:ч|час|часа|часов|h)(?=\s)"):
        p.duration = int(float(m.group(1).replace(",", ".")) * 60)
    elif m := cut(r"\s(\d+)\s?(?:м|мин|минут|минуты|min|m)(?=\s)"):
        p.duration = int(m.group(1))
    # Повторы
    recur_day = None
    if cut(r"\s(каждый день|ежедневно)(?=\s)"):
        p.recur = "daily"
    elif cut(r"\s(по будням)(?=\s)"):
        p.recur = "weekdays"
    elif m := cut(rf"\s(?:кажд\w+|по)\s({_WD})(?=\s)"):
        recur_day = WEEKDAYS[m.group(1).lower()]
        p.recur = f"weekly:{recur_day}"
    elif cut(r"\s(каждую неделю|еженедельно)(?=\s)"):
        p.recur = "weekly"
    elif cut(r"\s(каждый месяц|ежемесячно)(?=\s)"):
        p.recur = "monthly"
    # Дата
    due: date | None = None
    if cut(r"\s(?:до\s)?послезавтра(?=\s)"):
        due = today + timedelta(days=2)
    elif cut(r"\s(?:до\s)?завтра(?=\s)"):
        due = today + timedelta(days=1)
    elif cut(r"\s(?:до\s)?сегодня(?=\s)"):
        due = today
    elif m := cut(r"\sчерез\s(\d+)?\s?(день|дня|дней|неделю|недели|недель)(?=\s)"):
        n = int(m.group(1) or 1)
        due = today + timedelta(days=n * (7 if m.group(2).startswith("нед") else 1))
    elif m := cut(r"\s(?:до\s)?(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?(?=\s)"):
        year = int(m.group(3)) + (2000 if m.group(3) and len(m.group(3)) == 2 else 0) if m.group(3) else today.year
        try:
            due = date(year, int(m.group(2)), int(m.group(1)))
            if not m.group(3) and due < today:
                due = due.replace(year=today.year + 1)
        except ValueError:
            due = None
    elif m := cut(r"\s(?:до\s)?(\d{1,2})\s(янв|фев|мар|апр|ма[яй]|июн|июл|авг|сен|окт|ноя|дек)\w*(?=\s)"):
        month = MONTHS[m.group(2).lower()[:3] if m.group(2).lower()[:3] in MONTHS else m.group(2).lower()]
        try:
            due = date(today.year, month, int(m.group(1)))
            if due < today:
                due = due.replace(year=today.year + 1)
        except ValueError:
            due = None
    elif m := cut(rf"\s(?:до\s|в\s|во\s)?({_WD})(?=\s)"):
        due = _next_weekday(today, WEEKDAYS[m.group(1).lower()])
    elif cut(r"\sна выходных(?=\s)"):
        due = _next_weekday(today, 5, allow_today=True)
    elif cut(r"\sна неделе(?=\s)"):
        due = _next_weekday(today, 4, allow_today=True)
    # Время
    if m := cut(r"\s(?:в\s)?(\d{1,2}):(\d{2})(?=\s)"):
        p.due_time = f"{int(m.group(1)):02d}:{m.group(2)}"
    elif m := cut(r"\sв\s(\d{1,2})(?:\s?(?:ч|часов|часа))?(?=\s)"):
        hour = int(m.group(1))
        if hour <= 23:
            p.due_time = f"{hour:02d}:00"
    elif m := cut(r"\s(утром|днём|днем|вечером)(?=\s)"):
        p.due_time = {"утром": "09:00", "днём": "13:00", "днем": "13:00", "вечером": "19:00"}[m.group(1).lower()]

    if p.recur and due is None:
        if recur_day is not None:
            due = _next_weekday(today, recur_day, allow_today=True)
        elif p.recur == "weekdays" and today.weekday() >= 5:
            due = _next_weekday(today, 0)
        else:
            due = today
    if p.due_time and due is None:
        due = today
    p.due_date = due.isoformat() if due else None
    p.title = re.sub(r"\s+", " ", s).strip(" ,.-") or text.strip()
    return p


def next_occurrence(recur: str, current: date) -> date | None:
    if not recur:
        return None
    if recur == "daily":
        return current + timedelta(days=1)
    if recur == "weekdays":
        nxt = current + timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
        return nxt
    if recur.startswith("weekly"):
        return current + timedelta(days=7)
    if recur == "monthly":
        month = current.month % 12 + 1
        year = current.year + (current.month == 12)
        return date(year, month, min(current.day, 28))
    return None


RECUR_NAMES = {"daily": "каждый день", "weekdays": "по будням", "weekly": "каждую неделю", "monthly": "каждый месяц"}


def recur_name(recur: str) -> str:
    if recur.startswith("weekly:"):
        day = int(recur.split(":")[1])
        return "каждый " + ["пн", "вт", "ср", "чт", "пт", "сб", "вс"][day]
    return RECUR_NAMES.get(recur, "")


# ------------------------------------------------------------------ CRUD

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Tasks:
    def __init__(self, storage: Storage):
        self.db = storage

    def add(self, title: str, *, due_date: str | None = None, due_time: str | None = None, duration: int = 30,
            priority: int = 4, tags: list[str] | None = None, recur: str = "", notes: str = "",
            source: str = "", source_ref: str = "", parent_id: int | None = None) -> int:
        tags = tags or []
        return self.db.execute(
            "INSERT INTO tasks(title, notes, due_date, due_time, duration, priority, project, tags, recur, "
            "source, source_ref, parent_id, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (title.strip(), notes, due_date, due_time, max(5, int(duration or 30)), min(4, max(1, int(priority))),
             tags[0] if tags else "", ",".join(tags), recur, source, source_ref, parent_id, _now()),
        )

    def add_quick(self, text: str, today: date, **extra: Any) -> int:
        parsed = parse_quick(text, today)
        return self.add(**{**parsed.as_dict(), **extra})

    def get(self, task_id: int) -> dict[str, Any] | None:
        rows = self.db.query("SELECT * FROM tasks WHERE id = ?", (task_id,))
        return rows[0] if rows else None

    def find(self, query: str) -> dict[str, Any] | None:
        """Ищет открытую задачу по номеру или куску названия (для чата и бота)."""
        query = query.strip().lstrip("#")
        if query.isdigit():
            task = self.get(int(query))
            return task if task and task["status"] == "open" else None
        rows = self.db.query("SELECT * FROM tasks WHERE status = 'open' AND lower(title) LIKE ? ORDER BY id DESC",
                             (f"%{query.lower()}%",))
        return rows[0] if rows else None

    def update(self, task_id: int, **fields: Any) -> None:
        allowed = {"title", "notes", "due_date", "due_time", "duration", "priority", "project", "tags", "recur",
                   "scheduled_start", "status", "remind"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE tasks SET {sets} WHERE id = ?", (*fields.values(), task_id))

    def complete(self, task_id: int, today: date) -> int | None:
        """Закрывает задачу; для повторяющейся создаёт следующую. Возвращает id новой."""
        task = self.get(task_id)
        if not task or task["status"] != "open":
            return None
        self.db.execute("UPDATE tasks SET status = 'done', done_at = ? WHERE id = ?", (_now(), task_id))
        base = date.fromisoformat(task["due_date"]) if task["due_date"] else today
        nxt = next_occurrence(task["recur"], max(base, today) if task["recur"] != "monthly" else base)
        if nxt:
            return self.add(task["title"], due_date=nxt.isoformat(), due_time=task["due_time"],
                            duration=task["duration"], priority=task["priority"],
                            tags=[t for t in task["tags"].split(",") if t], recur=task["recur"], notes=task["notes"])
        return None

    def reopen(self, task_id: int) -> None:
        self.db.execute("UPDATE tasks SET status = 'open', done_at = NULL WHERE id = ?", (task_id,))

    def delete(self, task_id: int) -> None:
        self.db.execute("DELETE FROM tasks WHERE id = ? OR parent_id = ?", (task_id, task_id))

    def open_tasks(self) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM tasks WHERE status = 'open' ORDER BY due_date IS NULL, due_date, "
            "due_time IS NULL, due_time, priority, id"
        )

    def done_between(self, start: date, end: date) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM tasks WHERE status = 'done' AND substr(done_at, 1, 10) BETWEEN ? AND ? "
                             "ORDER BY done_at DESC", (start.isoformat(), end.isoformat()))

    def exists_from_source(self, source: str, ref: str) -> bool:
        return bool(self.db.query("SELECT 1 FROM tasks WHERE source = ? AND source_ref = ?", (source, ref)))

    # ---------------------------------------------------------- представления

    def views(self, today: date) -> dict[str, list[dict[str, Any]]]:
        iso = today.isoformat()
        week_end = (today + timedelta(days=7)).isoformat()
        tasks = [decorate(t, today) for t in self.open_tasks()]
        return {
            "overdue": [t for t in tasks if t["due_date"] and t["due_date"] < iso],
            "today": [t for t in tasks if t["due_date"] == iso],
            "upcoming": [t for t in tasks if t["due_date"] and iso < t["due_date"] <= week_end],
            "later": [t for t in tasks if t["due_date"] and t["due_date"] > week_end],
            "inbox": [t for t in tasks if not t["due_date"]],
            "all": tasks,
        }

    def matrix(self, today: date) -> dict[str, list[dict[str, Any]]]:
        """Матрица Эйзенхауэра: важность = приоритет 1–2, срочность = срок в ближайшие 2 дня."""
        soon = (today + timedelta(days=2)).isoformat()
        quadrants: dict[str, list[dict[str, Any]]] = {"do": [], "plan": [], "delegate": [], "drop": []}
        for task in self.open_tasks():
            important = task["priority"] <= 2
            urgent = bool(task["due_date"]) and task["due_date"] <= soon
            key = "do" if important and urgent else "plan" if important else "delegate" if urgent else "drop"
            quadrants[key].append(decorate(task, today))
        return quadrants


def decorate(task: dict[str, Any], today: date) -> dict[str, Any]:
    task = dict(task)
    task["tag_list"] = [t for t in (task.get("tags") or "").split(",") if t]
    task["recur_name"] = recur_name(task.get("recur") or "")
    if task.get("due_date"):
        due = date.fromisoformat(task["due_date"])
        delta = (due - today).days
        task["due_label"] = ("сегодня" if delta == 0 else "завтра" if delta == 1 else "вчера" if delta == -1
                             else f"просрочено {-delta} дн." if delta < 0 else due.strftime("%d.%m"))
        task["overdue"] = delta < 0
    else:
        task["due_label"], task["overdue"] = "", False
    return task


# ------------------------------------------------------------------ авто-планирование

def _dt(day: date, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":")[:2])
    return datetime.combine(day, time(h, m))


def busy_blocks(day: date, lessons: list[dict[str, Any]], tasks: list[dict[str, Any]],
                buffer_min: int = 10) -> list[tuple[datetime, datetime, dict[str, Any]]]:
    blocks = []
    for lesson in lessons:
        if lesson["start"][:10] != day.isoformat():
            continue
        start = datetime.fromisoformat(lesson["start"]) - timedelta(minutes=buffer_min)
        end = datetime.fromisoformat(lesson["end"]) + timedelta(minutes=buffer_min)
        blocks.append((start, end, {"kind": "lesson", **lesson}))
    for task in tasks:
        if task["due_date"] == day.isoformat() and task["due_time"]:
            start = _dt(day, task["due_time"])
            blocks.append((start, start + timedelta(minutes=task["duration"]), {"kind": "task", **task}))
    return sorted(blocks, key=lambda b: b[0])


def free_slots(day: date, wake: str, sleep: str, busy: list[tuple[datetime, datetime, Any]],
               now: datetime | None = None) -> list[tuple[datetime, datetime]]:
    start = _dt(day, wake)
    if now and now.date() == day:
        rounded = now.replace(second=0, microsecond=0)
        rounded += timedelta(minutes=(5 - rounded.minute % 5) % 5)  # к ближайшим 5 минутам вперёд
        start = max(start, rounded)
    end = _dt(day, sleep)
    slots, cursor = [], start
    for b_start, b_end, _ in busy:
        if b_start > cursor:
            slots.append((cursor, min(b_start, end)))
        cursor = max(cursor, b_end)
    if cursor < end:
        slots.append((cursor, end))
    return [(a, b) for a, b in slots if (b - a) >= timedelta(minutes=15)]


def auto_schedule(store: Tasks, day: date, lessons: list[dict[str, Any]], wake: str, sleep: str,
                  now: datetime | None = None) -> dict[str, Any]:
    """Раскладывает задачи без времени (на сегодня, просроченные, затем важные из входящих) по свободным окнам."""
    open_tasks = store.open_tasks()
    iso = day.isoformat()
    for task in open_tasks:  # перепланирование с нуля
        if task["scheduled_start"] and task["scheduled_start"][:10] == iso:
            store.update(task["id"], scheduled_start=None)
    candidates = [t for t in open_tasks if not t["due_time"] and (
        (t["due_date"] and t["due_date"] <= iso) or (not t["due_date"] and t["priority"] <= 2))]
    candidates.sort(key=lambda t: (t["due_date"] is None, t["priority"], t["due_date"] or "", t["id"]))
    slots = [list(s) for s in free_slots(day, wake, sleep, busy_blocks(day, lessons, open_tasks), now)]
    placed, left = [], []
    for task in candidates:
        need = timedelta(minutes=task["duration"])
        for slot in slots:
            if slot[1] - slot[0] >= need:
                store.update(task["id"], scheduled_start=slot[0].isoformat(timespec="minutes"))
                placed.append({**task, "scheduled_start": slot[0].isoformat(timespec="minutes")})
                slot[0] = slot[0] + need + timedelta(minutes=5)  # перерыв между задачами
                break
        else:
            left.append(task)
    return {"placed": placed, "left": left}


def timeline(store: Tasks, day: date, lessons: list[dict[str, Any]], wake: str, sleep: str) -> list[dict[str, Any]]:
    """Лента дня: пары, задачи со временем и запланированные, свободные окна."""
    iso = day.isoformat()
    tasks = store.open_tasks()
    items = []
    for lesson in lessons:
        if lesson["start"][:10] == iso:
            items.append({"kind": "lesson", "start": lesson["start"][11:16], "end": lesson["end"][11:16],
                          "title": lesson["title"], "sub": lesson.get("location", "")})
    for task in tasks:
        start = None
        if task["due_date"] == iso and task["due_time"]:
            start = _dt(day, task["due_time"])
        elif task["scheduled_start"] and task["scheduled_start"][:10] == iso:
            start = datetime.fromisoformat(task["scheduled_start"])
        if start:
            end = start + timedelta(minutes=task["duration"])
            items.append({"kind": "task", "id": task["id"], "start": start.strftime("%H:%M"),
                          "end": end.strftime("%H:%M"), "title": task["title"], "priority": task["priority"],
                          "auto": not task["due_time"]})
    items.sort(key=lambda i: i["start"])
    out, cursor = [], wake
    for item in items:
        if item["start"] > cursor and _minutes(cursor, item["start"]) >= 30:
            out.append({"kind": "free", "start": cursor, "end": item["start"],
                        "title": f"свободно {_fmt_minutes(_minutes(cursor, item['start']))}"})
        out.append(item)
        cursor = max(cursor, item["end"])
    if sleep > cursor and _minutes(cursor, sleep) >= 30:
        out.append({"kind": "free", "start": cursor, "end": sleep,
                    "title": f"свободно {_fmt_minutes(_minutes(cursor, sleep))}"})
    return out


def _minutes(a: str, b: str) -> int:
    ha, ma = (int(x) for x in a.split(":"))
    hb, mb = (int(x) for x in b.split(":"))
    return (hb * 60 + mb) - (ha * 60 + ma)


def _fmt_minutes(total: int) -> str:
    h, m = divmod(total, 60)
    return f"{h} ч {m} мин" if h and m else f"{h} ч" if h else f"{m} мин"
