"""Привычки со стриками, фокус-сессии (помодоро), дневник дня и статистика недели.

Идеи: трекер привычек и помодоро (TickTick, Habitica), вечерний ритуал «закрыть день» (Sunsama),
дневник настроения (Daylio), еженедельный обзор (GTD).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from .storage import Storage

MOODS = {1: "😞", 2: "😕", 3: "😐", 4: "🙂", 5: "😄"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Habits:
    def __init__(self, storage: Storage):
        self.db = storage

    def add(self, name: str, icon: str = "✅", per_week: int = 7) -> int:
        return self.db.execute("INSERT INTO habits(name, icon, per_week, created_at) VALUES(?, ?, ?, ?)",
                               (name.strip(), icon or "✅", max(1, min(7, per_week)), _now()))

    def archive(self, habit_id: int) -> None:
        self.db.execute("UPDATE habits SET archived = 1 WHERE id = ?", (habit_id,))

    def toggle(self, habit_id: int, day: date) -> bool:
        """Отмечает/снимает отметку; возвращает новое состояние."""
        if self.db.query("SELECT 1 FROM habit_log WHERE habit_id = ? AND day = ?", (habit_id, day.isoformat())):
            self.db.execute("DELETE FROM habit_log WHERE habit_id = ? AND day = ?", (habit_id, day.isoformat()))
            return False
        self.db.execute("INSERT INTO habit_log(habit_id, day) VALUES(?, ?)", (habit_id, day.isoformat()))
        return True

    def overview(self, today: date) -> list[dict[str, Any]]:
        habits = self.db.query("SELECT * FROM habits WHERE archived = 0 ORDER BY id")
        since = (today - timedelta(days=60)).isoformat()
        logs = self.db.query("SELECT habit_id, day FROM habit_log WHERE day >= ?", (since,))
        done: dict[int, set[str]] = {}
        for row in logs:
            done.setdefault(row["habit_id"], set()).add(row["day"])
        week_start = today - timedelta(days=today.weekday())
        result = []
        for habit in habits:
            days = done.get(habit["id"], set())
            streak, cursor = 0, today if today.isoformat() in days else today - timedelta(days=1)
            while cursor.isoformat() in days:
                streak += 1
                cursor -= timedelta(days=1)
            last7 = [(today - timedelta(days=i)) for i in range(6, -1, -1)]
            result.append({
                **habit,
                "done_today": today.isoformat() in days,
                "streak": streak,
                "week_count": sum(1 for d in days if d >= week_start.isoformat()),
                "last7": [{"day": d.isoformat(), "label": "пн вт ср чт пт сб вс".split()[d.weekday()],
                           "done": d.isoformat() in days} for d in last7],
            })
        return result


class Focus:
    def __init__(self, storage: Storage):
        self.db = storage

    def log(self, minutes: int, task_id: int | None = None, kind: str = "focus") -> int:
        return self.db.execute("INSERT INTO focus_sessions(task_id, started_at, minutes, kind) VALUES(?, ?, ?, ?)",
                               (task_id, _now(), max(1, int(minutes)), kind))

    def minutes_on(self, day: date) -> int:
        rows = self.db.query("SELECT COALESCE(SUM(minutes), 0) AS m FROM focus_sessions "
                             "WHERE kind = 'focus' AND substr(started_at, 1, 10) = ?", (day.isoformat(),))
        return rows[0]["m"]

    def sessions_on(self, day: date) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT f.*, t.title FROM focus_sessions f LEFT JOIN tasks t ON t.id = f.task_id "
            "WHERE substr(f.started_at, 1, 10) = ? ORDER BY f.id DESC", (day.isoformat(),))


class Journal:
    def __init__(self, storage: Storage):
        self.db = storage

    def get(self, day: date) -> dict[str, Any]:
        rows = self.db.query("SELECT * FROM journal WHERE day = ?", (day.isoformat(),))
        return rows[0] if rows else {"day": day.isoformat(), "mood": None, "wins": "", "notes": "", "tomorrow": ""}

    def save(self, day: date, mood: int | None, wins: str, notes: str, tomorrow: str) -> None:
        self.db.execute(
            "INSERT INTO journal(day, mood, wins, notes, tomorrow, updated_at) VALUES(?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(day) DO UPDATE SET mood = excluded.mood, wins = excluded.wins, notes = excluded.notes, "
            "tomorrow = excluded.tomorrow, updated_at = excluded.updated_at",
            (day.isoformat(), mood, wins, notes, tomorrow, _now()),
        )

    def recent(self, days: int = 14) -> list[dict[str, Any]]:
        return self.db.query("SELECT * FROM journal ORDER BY day DESC LIMIT ?", (days,))


def week_stats(storage: Storage, today: date) -> dict[str, Any]:
    """Статистика за последние 7 дней: выполненные задачи, фокус, привычки, настроение."""
    days = [today - timedelta(days=i) for i in range(6, -1, -1)]
    start = days[0].isoformat()
    done = storage.query("SELECT substr(done_at, 1, 10) AS d, COUNT(*) AS n FROM tasks "
                         "WHERE status = 'done' AND substr(done_at, 1, 10) >= ? GROUP BY d", (start,))
    focus = storage.query("SELECT substr(started_at, 1, 10) AS d, SUM(minutes) AS m FROM focus_sessions "
                          "WHERE kind = 'focus' AND substr(started_at, 1, 10) >= ? GROUP BY d", (start,))
    habits = storage.query("SELECT day AS d, COUNT(*) AS n FROM habit_log WHERE day >= ? GROUP BY day", (start,))
    moods = storage.query("SELECT day AS d, mood FROM journal WHERE day >= ?", (start,))
    by = lambda rows, key: {r["d"]: r[key] for r in rows}  # noqa: E731
    done_m, focus_m, habit_m, mood_m = by(done, "n"), by(focus, "m"), by(habits, "n"), by(moods, "mood")
    series = [{
        "day": d.isoformat(), "label": "пн вт ср чт пт сб вс".split()[d.weekday()],
        "done": done_m.get(d.isoformat(), 0), "focus": focus_m.get(d.isoformat(), 0) or 0,
        "habits": habit_m.get(d.isoformat(), 0), "mood": mood_m.get(d.isoformat()),
    } for d in days]
    return {
        "series": series,
        "total_done": sum(s["done"] for s in series),
        "total_focus": sum(s["focus"] for s in series),
        "max_done": max([s["done"] for s in series] + [1]),
        "max_focus": max([s["focus"] for s in series] + [1]),
    }
