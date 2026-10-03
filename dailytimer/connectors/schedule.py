"""Расписание университета.

Источники (можно комбинировать):
1. ICS-ссылка — большинство вузов и сервисов (RUZ, Modeus, Google/Outlook-календарь, Яндекс)
   отдают расписание в формате iCalendar.
2. Ручная таблица в настройках, по строке на пару:
   «пн 09:00-10:30 Матанализ, ауд. 301» (дни: пн вт ср чт пт сб вс; можно «чёт/нечёт» в конце).
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import icalendar

DAYS = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6}
_LINE = re.compile(
    r"^\s*(пн|вт|ср|чт|пт|сб|вс)\s+(\d{1,2}[:.]\d{2})\s*-\s*(\d{1,2}[:.]\d{2})\s+(.+?)(?:\s+(чёт|чет|нечёт|нечет))?\s*$",
    re.I,
)


class ScheduleError(RuntimeError):
    pass


def _t(value: str) -> time:
    h, m = value.replace(".", ":").split(":")
    return time(int(h), int(m))


def parse_manual(text: str, start: date, days: int = 7) -> list[dict[str, Any]]:
    lessons = []
    for line in (text or "").splitlines():
        match = _LINE.match(line)
        if not match:
            continue
        day, t1, t2, title, parity = match.groups()
        for offset in range(days):
            current = start + timedelta(days=offset)
            if current.weekday() != DAYS[day.lower()]:
                continue
            if parity:
                even_week = current.isocalendar().week % 2 == 0
                if parity.lower().startswith("ч") != even_week:
                    continue
            lessons.append(
                {
                    "title": title.strip(),
                    "start": datetime.combine(current, _t(t1)).isoformat(),
                    "end": datetime.combine(current, _t(t2)).isoformat(),
                    "location": "",
                    "source": "manual",
                }
            )
    return lessons


def parse_ics(content: bytes | str, start: date, days: int, tz: ZoneInfo) -> list[dict[str, Any]]:
    cal = icalendar.Calendar.from_ical(content)
    window_start = datetime.combine(start, time.min, tz)
    window_end = window_start + timedelta(days=days)
    lessons = []
    for event in cal.walk("VEVENT"):
        dtstart = event.decoded("DTSTART")
        dtend = event.decoded("DTEND") if event.get("DTEND") else None
        if not isinstance(dtstart, datetime):  # событие на весь день
            dtstart = datetime.combine(dtstart, time.min)
        dtstart = dtstart.astimezone(tz) if dtstart.tzinfo else dtstart.replace(tzinfo=tz)
        if dtend is not None:
            if not isinstance(dtend, datetime):
                dtend = datetime.combine(dtend, time.min)
            dtend = dtend.astimezone(tz) if dtend.tzinfo else dtend.replace(tzinfo=tz)
        duration = (dtend - dtstart) if dtend else timedelta(hours=1, minutes=30)
        for occurrence in _occurrences(event, dtstart, window_start, window_end):
            lessons.append(
                {
                    "title": str(event.get("SUMMARY", "Занятие")),
                    "start": occurrence.replace(tzinfo=None).isoformat(),
                    "end": (occurrence + duration).replace(tzinfo=None).isoformat(),
                    "location": str(event.get("LOCATION", "")),
                    "source": "ics",
                }
            )
    return lessons


def _occurrences(event: Any, dtstart: datetime, lo: datetime, hi: datetime) -> list[datetime]:
    rule = event.get("RRULE")
    if not rule:
        return [dtstart] if lo <= dtstart < hi else []
    from dateutil.rrule import rrulestr  # зависимость icalendar

    rrule = rrulestr(rule.to_ical().decode(), dtstart=dtstart)
    exdates = set()
    for ex in event.get("EXDATE", []) if isinstance(event.get("EXDATE"), list) else [event.get("EXDATE")]:
        if ex is not None:
            exdates.update(d.dt for d in ex.dts)
    return [d for d in rrule.between(lo, hi, inc=True) if d not in exdates]


def fetch(settings: dict[str, Any], today: date, days: int = 7) -> dict[str, Any]:
    tz = ZoneInfo(settings.get("timezone") or "Europe/Moscow")
    lessons = parse_manual(settings.get("schedule_manual", ""), today, days)
    url = settings.get("schedule_ics_url", "").strip()
    if url:
        auth = None
        if settings.get("schedule_login"):
            auth = (settings["schedule_login"], settings.get("schedule_password", ""))
        try:
            resp = httpx.get(url.replace("webcal://", "https://"), auth=auth, timeout=30, follow_redirects=True)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ScheduleError(f"Не удалось скачать расписание: {exc}") from exc
        lessons += parse_ics(resp.content, today, days, tz)
    if not url and not lessons:
        raise ScheduleError("Расписание не настроено")
    lessons.sort(key=lambda lesson: lesson["start"])
    return {"lessons": lessons}
