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
