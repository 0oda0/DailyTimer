"""Расписание из личного кабинета вуза: логин+пароль → браузер без окна → текст страницы → пары.

Работает с большинством кабинетов без доработок: сам находит поля логина и пароля,
умеет двухшаговый вход (сначала логин, потом пароль — как в Keycloak/SSO).
Пары из текста страницы достаёт ИИ, а если его нет — эвристический разбор.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ..ai import AIClient, AIError

log = logging.getLogger(__name__)

USER_FIELD = (
    "input[type=email], input[autocomplete=username], input[name*=user i], input[name*=login i], "
    "input[id*=user i], input[id*=login i], input[name*=mail i], input[type=text], input:not([type])"
)
PASS_FIELD = "input[type=password]"
SUBMIT = "button[type=submit], input[type=submit], button:has-text('Войти'), button:has-text('Вход'), button:has-text('Log in'), button:has-text('Sign in')"


class PortalError(RuntimeError):
    pass


def _first_visible(page: Any, selector: str) -> Any:
    for handle in page.query_selector_all(selector):
        try:
            if handle.is_visible() and handle.is_enabled():
                return handle
        except Exception:
            continue
    return None


def _submit(page: Any, field: Any) -> None:
    button = _first_visible(page, SUBMIT)
    if button:
        button.click()
    else:
        field.press("Enter")
    _settle(page)


def _login(page: Any, username: str, password: str) -> None:
    pwd = _first_visible(page, PASS_FIELD)
    user = _first_visible(page, USER_FIELD)
    if user is None and pwd is None:
        # Возможно, на странице есть кнопка «Войти», ведущая на форму.
        link = _first_visible(page, "a:has-text('Войти'), a:has-text('Вход'), button:has-text('Войти')")
        if link is None:
            return  # формы нет — видимо, уже вошли
        link.click()
        _settle(page)
        pwd, user = _first_visible(page, PASS_FIELD), _first_visible(page, USER_FIELD)
    if user is not None:
        user.fill(username)
    if pwd is None:  # двухшаговый вход
        _submit(page, user)
        page.wait_for_selector(PASS_FIELD, state="visible", timeout=20_000)
        pwd = _first_visible(page, PASS_FIELD)
    pwd.fill(password)
    _submit(page, pwd)
    if _first_visible(page, PASS_FIELD) is not None:
        raise PortalError("Кабинет не пустил: проверь логин и пароль (или там капча/вход по коду)")


def _settle(page: Any, timeout: int = 20_000) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        page.wait_for_timeout(3000)


def _needs_login(page: Any) -> bool:
    if _first_visible(page, PASS_FIELD) is not None:
        return True
    return bool(re.search(r"(login|auth|sso|signin|sign-in|cas/|oauth|vhod)", page.url, re.I))


def scrape_text(login_url: str, schedule_url: str, username: str, password: str, state_file: str | None = None) -> str:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise PortalError("Не установлен Playwright (в Docker-образе он есть)") from exc
    if not (login_url or schedule_url):
        raise PortalError("Укажи адрес личного кабинета")
    has_state = bool(state_file) and Path(state_file).exists()
    with sync_playwright() as p:
        browser = p.chromium.launch(
            args=["--no-sandbox", "--disable-dev-shm-usage"],
            executable_path=os.environ.get("CHROMIUM_PATH") or None,
        )
        try:
            context_args: dict[str, Any] = {"locale": "ru-RU", "viewport": {"width": 1400, "height": 2000}}
            if has_state:
                context_args["storage_state"] = state_file
            context = browser.new_context(**context_args)
            page = context.new_page()
            page.goto(schedule_url or login_url, wait_until="domcontentloaded", timeout=60_000)
            _settle(page)
            if _needs_login(page):
                _login(page, username, password)
            elif login_url and schedule_url and not has_state:
                page.goto(login_url, wait_until="domcontentloaded", timeout=60_000)
                _settle(page)
                if _needs_login(page):
                    _login(page, username, password)
            if schedule_url and page.url.rstrip("/") != schedule_url.rstrip("/"):
                page.goto(schedule_url, wait_until="domcontentloaded", timeout=60_000)
                _settle(page, 30_000)
            page.wait_for_timeout(2500)  # SPA дорисовывают расписание после загрузки
            texts = [page.inner_text("body")]
            for frame in page.frames[1:]:
                try:
                    texts.append(frame.inner_text("body"))
                except Exception:
                    continue
            if state_file:
                context.storage_state(path=state_file)  # чтобы не логиниться каждый раз
            return "\n".join(texts)
        finally:
            browser.close()


# ---------------------------------------------------------------- разбор текста

_WEEKDAYS = {
    "понедельник": 0, "вторник": 1, "среда": 2, "среду": 2, "четверг": 3, "пятница": 4, "пятницу": 4,
    "суббота": 5, "субботу": 5, "воскресенье": 6, "пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6,
}
_MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "ма": 5, "июн": 6, "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}
_DATE_NUM = re.compile(r"\b(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\b")
_DATE_TXT = re.compile(r"\b(\d{1,2})\s+(янв|фев|мар|апр|ма[яй]|июн|июл|авг|сен|окт|ноя|дек)\w*", re.I)
_TIME = re.compile(r"\b(\d{1,2})[:.](\d{2})\s*[-–—]\s*(\d{1,2})[:.](\d{2})\b")
_WEEKDAY_RE = re.compile(r"^\s*(" + "|".join(sorted(_WEEKDAYS, key=len, reverse=True)) + r")\b", re.I)


def _year_for(month: int, today: date) -> int:
    # Расписание смотрит вперёд: декабрь при январском «сегодня» — прошлый год, и наоборот.
    if month - today.month > 6:
        return today.year - 1
    if today.month - month > 6:
        return today.year + 1
    return today.year


def heuristic_parse(text: str, today: date, days: int = 14) -> list[dict[str, Any]]:
    lessons: list[dict[str, Any]] = []
    current: date | None = None
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for i, line in enumerate(lines):
        num, txt = _DATE_NUM.search(line), _DATE_TXT.search(line)
        if num and not _TIME.search(line[: num.end() + 1]):
            day, month, year = int(num.group(1)), int(num.group(2)), num.group(3)
            if 1 <= month <= 12 and 1 <= day <= 31:
                y = int(year) + (2000 if year and len(year) == 2 else 0) if year else _year_for(month, today)
                try:
                    current = date(y, month, day)
                except ValueError:
                    pass
        elif txt:
            month = next(v for k, v in _MONTHS.items() if txt.group(2).lower().startswith(k))
            try:
                current = date(_year_for(month, today), month, int(txt.group(1)))
            except ValueError:
                pass
        elif _WEEKDAY_RE.match(line) and not _TIME.search(line):
            wd = _WEEKDAYS[_WEEKDAY_RE.match(line).group(1).lower()]
            current = today + timedelta(days=(wd - today.weekday()) % 7)
        tm = _TIME.search(line)
        if not tm or current is None:
            continue
        rest = (line[: tm.start()] + " " + line[tm.end() :]).strip(" |,;-–")
        rest = re.sub(r"\s+", " ", re.sub(r"^\d+\s*(пара)?\s*", "", rest)).strip()
        if len(rest) < 3 and i + 1 < len(lines):
            rest = lines[i + 1]
        h1, m1, h2, m2 = (int(x) for x in tm.groups())
        lessons.append(
            {
                "title": rest[:120] or "Занятие",
                "start": f"{current.isoformat()}T{h1:02d}:{m1:02d}:00",
                "end": f"{current.isoformat()}T{h2:02d}:{m2:02d}:00",
                "location": "",
                "source": "portal",
            }
        )
    horizon = (today + timedelta(days=days)).isoformat()
    return [l for l in lessons if today.isoformat() <= l["start"][:10] <= horizon]


_SYSTEM = (
    "Тебе дан текст страницы с расписанием занятий из личного кабинета университета. "
    "Извлеки все занятия. Ответь строго JSON: "
    '{"lessons": [{"date": "YYYY-MM-DD", "start": "HH:MM", "end": "HH:MM", "title": "предмет и тип занятия", '
    '"location": "аудитория/корпус или ссылка", "teacher": "преподаватель"}]}. '
    "Если дата указана днём недели — вычисли её относительно сегодняшней даты. Ничего не выдумывай."
)


def _hhmm(value: Any) -> str:
    match = re.search(r"(\d{1,2})\D(\d{2})", str(value or ""))
    if not match:
        raise ValueError(f"нет времени в {value!r}")
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def ai_parse(text: str, today: date, ai: AIClient) -> list[dict[str, Any]]:
    compact = re.sub(r"\n\s*\n+", "\n", text)[:9000]
    answer = ai.chat_json(_SYSTEM, f"Сегодня {today.isoformat()} ({today.strftime('%A')}).\n\n{compact}")
    items = answer.get("lessons", []) if isinstance(answer, dict) else answer
    lessons = []
    for item in items or []:
        try:
            day = date.fromisoformat(item["date"])
            start = _hhmm(item["start"])
            end = _hhmm(item.get("end")) if item.get("end") else start
        except (KeyError, ValueError, TypeError):
            continue
        location = " · ".join(x for x in (item.get("location"), item.get("teacher")) if x)
        lessons.append(
            {
                "title": str(item.get("title") or "Занятие")[:120],
                "start": f"{day.isoformat()}T{start}:00",
                "end": f"{day.isoformat()}T{end}:00",
                "location": location,
                "source": "portal",
            }
        )
    return lessons


def fetch(settings: dict[str, Any], today: date, ai: AIClient | None, state_file: str | None = None) -> dict[str, Any]:
    if not settings.get("schedule_login") or not settings.get("schedule_password"):
        raise PortalError("Не указаны логин и пароль от личного кабинета")
    text = scrape_text(
        settings.get("schedule_portal_url", "").strip(),
        settings.get("schedule_page_url", "").strip(),
        settings["schedule_login"],
        settings["schedule_password"],
        state_file,
    )
    lessons, engine = [], "rules"
    if ai:
        try:
            lessons, engine = ai_parse(text, today, ai), ai.name
        except AIError as exc:
            log.warning("ИИ не разобрал расписание: %s", exc)
    if not lessons:
        lessons, engine = heuristic_parse(text, today), "rules"
    return {"lessons": sorted(lessons, key=lambda l: l["start"]), "engine": engine, "page_preview": text[:3000]}
