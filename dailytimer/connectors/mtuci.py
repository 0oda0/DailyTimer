"""Личный кабинет МТУСИ (lk.mtuci.ru): точное расписание через внутренний JSON API.

Вход — встроенным браузером (у кабинета антибот-проверка на JS и Keycloak), затем в той же
сессии: профиль студента → группа → /api/timetable/get по месяцам.
Формат API взят из открытой библиотеки github.com/derived-functor/mtuci_private_api.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)

BASE = "https://lk.mtuci.ru"
TYPES = {
    "Лекции": "лекция",
    "Практические занятия": "практика",
    "Лабораторные работы": "лаба",
    "Зачет": "зачёт",
    "Дифференцированный зачет": "дифзачёт",
    "Экзамен": "экзамен",
    "Консультация": "консультация",
}


class MtuciError(RuntimeError):
    pass


def is_mtuci(url: str) -> bool:
    return (urlparse(url or "").hostname or "").endswith("mtuci.ru")


def parse_timetable(payload: dict[str, Any], start: date, days: int) -> list[dict[str, Any]]:
    if payload.get("status") != "success":
        raise MtuciError(f"Кабинет вернул ошибку расписания: {payload.get('errors') or payload.get('status')}")
    lessons = []
    horizon = start + timedelta(days=days)
    for day_key, items in (payload.get("data", {}).get("days") or {}).items():
        try:
            day = datetime.strptime(day_key, "%d.%m.%Y").date()
        except ValueError:
            continue
        if not start <= day < horizon:
            continue
        for item in items or []:
            kind = TYPES.get(item.get("UF_TYPE", ""), (item.get("UF_TYPE") or "").lower())
            title = item.get("UF_DISCIPLINE") or "Занятие"
            if kind:
                title += f" ({kind})"
            if str(item.get("UF_IS_RETAKE", "0")) == "1":
                title += " — пересдача"
            where = ", ".join(item.get("UF_AUDIENCE") or [])
            if str(item.get("UF_IS_ONLINE", "0")) == "1":
                where = f"онлайн{' · ' + where if where else ''}"
            teachers = ", ".join(item.get("UF_TEACHER") or [])
            lessons.append(
                {
                    "title": title,
                    "start": f"{day.isoformat()}T{item.get('UF_TIME_START', '00:00')}:00",
                    "end": f"{day.isoformat()}T{item.get('UF_TIME_END', '00:00')}:00",
                    "location": " · ".join(x for x in (where, teachers) if x),
                    "number": item.get("UF_NUMBER"),
                    "source": "mtuci",
                }
            )
    return sorted(lessons, key=lambda l: l["start"])


def parse_group(payload: dict[str, Any]) -> str | None:
    blocks = payload.get("data", {}).get("Ответ", {}).get("МассивБлоков") or []
    for block in blocks:
        group = (block.get("ПереченьЗначений", {}).get("Группа") or {}).get("name")
        if group:
            return group
    return None


def _login(page: Any, username: str, password: str) -> None:
    page.goto(BASE + "/", wait_until="domcontentloaded", timeout=60_000)
    # Антибот-страница сама перезагружается после JS-проверки — просто ждём форму или кабинет.
    try:
        page.wait_for_function(
            "() => document.querySelector('input[name=password]') || "
            "(!location.pathname.startsWith('/bvzauth') && window.lkConfig)",
            timeout=60_000,
        )
    except Exception as exc:
        raise MtuciError("Кабинет МТУСИ не открылся (антибот-проверка или сайт недоступен)") from exc
    if page.query_selector("input[name=password]"):
        page.fill("input[name=username]", username)
        page.fill("input[name=password]", password)
        remember = page.query_selector("input[name=rememberMe]")
        if remember and not remember.is_checked():
            remember.check()
        page.click("#kc-login, input[type=submit], button[type=submit]")
        try:
            page.wait_for_url(lambda url: "/bvzauth/" not in url, timeout=45_000)
        except Exception as exc:
            error = page.query_selector(".kc-feedback-text, .alert-error, #input-error")
            message = error.inner_text().strip() if error else "кабинет не принял логин или пароль"
            raise MtuciError(f"Вход в ЛК МТУСИ не удался: {message}") from exc
    try:
        page.wait_for_load_state("networkidle", timeout=30_000)
    except Exception:
        pass  # SPA может держать соединения открытыми — куки уже есть


def fetch(settings: dict[str, Any], today: date, days: int = 14, state_file: str | None = None) -> dict[str, Any]:
    username, password = settings.get("schedule_login", ""), settings.get("schedule_password", "")
    if not username or not password:
        raise MtuciError("Не указаны логин и пароль от ЛК МТУСИ")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise MtuciError("Не установлен Playwright (в Docker-образе он есть)") from exc

    with sync_playwright() as p:
        browser = p.chromium.launch(
            args=["--no-sandbox", "--disable-dev-shm-usage"],
            executable_path=os.environ.get("CHROMIUM_PATH") or None,
        )
        try:
            args: dict[str, Any] = {"locale": "ru-RU"}
            if state_file and Path(state_file).exists():
                args["storage_state"] = state_file
            context = browser.new_context(**args)
            page = context.new_page()
            _login(page, username, password)
            api = context.request

            group = (settings.get("mtuci_group") or "").strip()
            profile: dict[str, Any] = {}
            if not group:
                resp = api.post(
                    BASE + "/ilk/x/getProcessor",
                    data={"processor": "iEmployee_card", "referrer": "/student/profile/student_card",
                          "role": "student", "НомерСтраницы": 0},
                )
                if not resp.ok:
                    raise MtuciError(f"Не удалось получить профиль студента ({resp.status}) — укажи группу вручную")
                profile = resp.json()
                group = parse_group(profile)
                if not group:
                    raise MtuciError("В профиле не нашлась группа — укажи её вручную в настройках")

            months = sorted({(today + timedelta(days=d)).month for d in range(days)})
            lessons: list[dict[str, Any]] = []
            for month in months:
                resp = api.get(BASE + "/api/timetable/get", params={"value": group, "month": month - 1, "type": "group"})
                if not resp.ok:
                    raise MtuciError(f"Кабинет не отдал расписание ({resp.status})")
                lessons += parse_timetable(resp.json(), today, days)
            if state_file:
                context.storage_state(path=state_file)
        finally:
            browser.close()

    name = (profile.get("inputParams", {}).get("ФизическоеЛицо") or {}).get("name") if profile else None
    unique = {(l["start"], l["title"]): l for l in lessons}
    return {"lessons": sorted(unique.values(), key=lambda l: l["start"]), "group": group, "student": name,
            "engine": "mtuci-api"}
