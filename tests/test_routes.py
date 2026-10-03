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
