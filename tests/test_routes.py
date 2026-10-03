"""Каждая кнопка, форма и fetch() в шаблонах должны вести на существующий маршрут с нужным методом."""

import re
from pathlib import Path

from fastapi.testclient import TestClient
from starlette.routing import Match

from dailytimer.storage import Storage
from dailytimer.web.app import create_app

TEMPLATES = Path(__file__).resolve().parents[1] / "dailytimer" / "web" / "templates"


def referenced_urls():
    found = set()
    for path in TEMPLATES.glob("*.html"):
        text = path.read_text()
        for form in re.finditer(r"<form\b[^>]*>", text):
            tag = form.group(0)
            action = re.search(r'action="([^"]*)"', tag)
            method = re.search(r'method="([^"]*)"', tag)
            if action and action.group(1):
                found.add(((method.group(1) if method else "get").upper(), action.group(1), path.name))
        for m in re.finditer(r'formaction="([^"]+)"', text):
            found.add(("POST", m.group(1), path.name))
        for m in re.finditer(r"fetch\('([^'?]+)", text):
            method = "POST" if "method: 'POST'" in text[m.start():m.start() + 200] else "GET"
            found.add((method, m.group(1), path.name))
        for m in re.finditer(r'href="(/[^"?#]*)', text):
            found.add(("GET", m.group(1), path.name))
    return found


def normalize(url):
    url = re.sub(r"\{\{[^}]*\}\}", "1", url)          # /tasks/{{ t.id }}/done → /tasks/1/done
    url = url.split("?")[0] or "/"
    if url != "/" and url.endswith("/"):                # '/api/server/jobs/' + id в JS
        url += "study"
    return url


def test_every_template_url_has_a_route(tmp_path, monkeypatch):
    monkeypatch.delenv("DAILYTIMER_PASSWORD", raising=False)
    app = create_app(Storage(tmp_path), start_scheduler=False)
    missing = []
    for method, url, template in sorted(referenced_urls()):
        path = normalize(url)
        if path.startswith("/static"):
            continue
        scope = {"type": "http", "path": path, "method": method}
        ok = any(route.matches(scope)[0] == Match.FULL for route in app.routes)
        if not ok:
            missing.append(f"{method} {url} ({template})")
    assert not missing, "Нет маршрутов для: " + ", ".join(missing)


def test_sync_button_works(tmp_path, monkeypatch):
    monkeypatch.delenv("DAILYTIMER_PASSWORD", raising=False)
    client = TestClient(create_app(Storage(tmp_path), start_scheduler=False))
    resp = client.post("/sync", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/")


def test_blocking_work_runs_outside_event_loop(tmp_path, monkeypatch):
    """Telegram-вход, чат и действия на сервере не должны выполняться в цикле событий сервера:
    там asyncio.run() падает, а долгий ответ ИИ замораживает весь сайт."""
    import asyncio

    from dailytimer.connectors import tg_account, vps

    def needs_own_loop(*args, **kwargs):
        asyncio.run(asyncio.sleep(0))  # упадёт, если нас вызвали внутри работающего цикла
        return ("session", "hash")

    monkeypatch.setattr(tg_account, "send_code", needs_own_loop)
    monkeypatch.setattr("dailytimer.assistant.Assistant.reply", lambda self, text, ch: needs_own_loop() and "ok")
    monkeypatch.setattr(vps.Agent, "update", lambda self, path: needs_own_loop() and "job1")
    monkeypatch.delenv("DAILYTIMER_PASSWORD", raising=False)
    store = Storage(tmp_path)
    store.save_settings({"server_agent_token": "t"})
    client = TestClient(create_app(store, start_scheduler=False), raise_server_exceptions=True)
    resp = client.post("/telegram/account/code", data={"tg_api_id": "1", "tg_api_hash": "h", "tg_phone": "+7"},
                       follow_redirects=False)
    assert resp.status_code == 303 and "error" not in resp.headers["location"]
    assert client.post("/api/chat", json={"text": "привет"}).json()["reply"] == "ok"
    assert client.post("/api/server/update", json={"path": "/x"}).json() == {"job": "job1"}


def test_ai_page_warns_when_model_too_big(tmp_path, monkeypatch):
    import dailytimer.web.app as web_app

    monkeypatch.delenv("DAILYTIMER_PASSWORD", raising=False)
    monkeypatch.setattr(web_app, "server_ram_gb", lambda: 2.9)
    store = Storage(tmp_path)
    store.save_settings({"ai_local_model": "qwen2.5:3b"})
    page = TestClient(create_app(store, start_scheduler=False)).get("/settings/ai").text
    assert "2.9 ГБ памяти" in page and "qwen2.5:1.5b</b>" in page
    store.save_settings({"ai_local_model": "qwen2.5:1.5b"})
    page = TestClient(create_app(store, start_scheduler=False)).get("/settings/ai").text
    assert "ГБ памяти, а модели" not in page


def test_schedule_page_and_refresh(tmp_path, monkeypatch):
    import time

    from dailytimer import sync

    monkeypatch.delenv("DAILYTIMER_PASSWORD", raising=False)
    store = Storage(tmp_path)
    store.save_settings({"schedule_login": "a@edu.mtuci.ru", "schedule_password": "x",
                         "schedule_portal_url": "https://lk.mtuci.ru/student/schedule"})
    day = sync.today_for(store.get_settings()).isoformat()
    calls = []

    def fake_portal(settings, today, ai, state):
        calls.append(1)
        return {"group": "БВТ2401", "lessons": [{"title": "Матанализ (лекция)", "start": f"{day}T09:30:00",
                                                 "end": f"{day}T11:05:00", "location": "А-101"}]}

    monkeypatch.setattr(sync, "fetch_portal", fake_portal)
    client = TestClient(create_app(store, start_scheduler=False))
    assert "Пар нет" in client.get("/schedule").text
    assert client.post("/schedule/refresh", follow_redirects=False).status_code == 303
    for _ in range(50):
        if store.get_snapshot("schedule")["data"]:
            break
        time.sleep(0.05)
    page = client.get("/schedule").text
    assert calls and "Матанализ (лекция)" in page and "А-101" in page and "БВТ2401" in page and "сегодня" in page


def test_portal_retries_transient_errors_but_not_bad_password(tmp_path, monkeypatch):
    from datetime import date

    import pytest

    from dailytimer import sync

    monkeypatch.setattr("dailytimer.ai.server_ram_gb", lambda: 16.0)
    calls = []

    def flaky(settings, today, ai, state):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("Timeout 60000ms exceeded")  # браузер не успел на перегруженном сервере
        return {"lessons": [{"title": "X"}]}

    monkeypatch.setattr(sync, "_fetch_portal_once", flaky)
    state = tmp_path / "session.json"
    state.write_text("{}")
    assert sync.fetch_portal({}, date(2026, 10, 5), None, str(state)) == {"lessons": [{"title": "X"}]}
    assert len(calls) == 2 and not state.exists()  # протухшая сессия удалена перед повтором

    def bad_password(settings, today, ai, state):
        calls.append(1)
        raise RuntimeError("Вход в ЛК МТУСИ не удался: неверный логин или пароль")

    calls.clear()
    monkeypatch.setattr(sync, "_fetch_portal_once", bad_password)
    with pytest.raises(RuntimeError):
        sync.fetch_portal({}, date(2026, 10, 5), None, str(state))
    assert len(calls) == 1
