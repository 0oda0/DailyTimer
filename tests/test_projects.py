import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from fastapi.testclient import TestClient

from dailytimer import planner, projects as projects_mod, sync
from dailytimer.projects import Projects, normalize, parse_reports, slugify
from dailytimer.storage import Storage
from dailytimer.web.app import create_app
from dailytimer.web.projects_routes import _repo_slug

REPORT = {
    "source_chat": "Сайт-диагност через ELM327",
    "generated_at": "2026-10-07",
    "projects": [{
        "id": "obd-site",
        "name": "OBD сайт",
        "summary": "Чтение ошибок из браузера",
        "status": "testing",
        "progress_percent": 70,
        "stage": "Первая проверка на машине",
        "recent_changes": [{"date": "2026-10-05", "change": "HTTPS на IP"}],
        "next_steps": ["Проверить на машине"],
        "blockers": ["Не проверено на реальной машине"],
        "location": {"repo": "0oda0/OBD (приватный, https://github.com/0oda0/OBD)", "url": "https://1.2.3.4 (порт 443)",
                     "deploy_path": "/opt/obd (клон в /root/obd)"},
        "health_check": "curl https://1.2.3.4/api/health → {\"ai\": false}",
        "secrets_stored_in": "Ключ API — в /etc/obd.env на сервере, SSH — ключ ~/.ssh/obd",
    }],
}
SECOND = {
    "source_chat": "Сайт школы китайского",
    "projects": [
        {"name": "Сайт школы", "status": "in progress", "progress_percent": "85%",
         "location": {"repo": None, "url": "https://school.example", "deploy_path": "/opt/school"},
         "health_check": "GET /api/health на проде"},
        {"name": "Шаблон", "status": "что-то странное", "progress_percent": 250,
         "location": {"url": "http://localhost:3000 (локально)"},
         "notes": "admin password: hunter2secret, токен бота ghp_abcdefghijklmnopqrstuvwxyz0123"},
    ],
}


def test_parse_several_reports_from_chat_text():
    text = ("Вот первый\n```json\n" + json.dumps(REPORT, ensure_ascii=False) + "\n```\nи второй: "
            + json.dumps(SECOND, ensure_ascii=False))
    reports = parse_reports(text)
    assert [len(r["projects"]) for r in reports] == [1, 2]
    try:
        parse_reports("просто текст {без json")
        raise AssertionError
    except ValueError as exc:
        assert "JSON" in str(exc)


def test_normalize_and_slug():
    p = normalize(SECOND["projects"][0])
    assert p["id"] == "sayt-shkoly" and p["status"] == "in_progress" and p["progress"] == 85
    odd = normalize(SECOND["projects"][1])
    assert odd["status"] == "in_progress" and odd["progress"] == 100
    assert normalize({"name": "X", "status": "done"})["progress"] == 100
    assert slugify("D.N.A. Detailing") == "d-n-a-detailing"


def test_import_merge_history_and_secrets(tmp_path):
    store = Projects(Storage(tmp_path))
    result = store.import_text(json.dumps(REPORT, ensure_ascii=False) + json.dumps(SECOND, ensure_ascii=False))
    assert result["added"] == ["OBD сайт", "Сайт школы", "Шаблон"] and result["redacted"] == 2
    tpl = store.get("shablon")
    assert "hunter2secret" not in tpl["notes"] and "ghp_" not in tpl["notes"] and "[скрыто]" in tpl["notes"]
    obd = store.get("obd-site")
    assert obd["secrets_stored_in"] == REPORT["projects"][0]["secrets_stored_in"]  # пути к секретам не трогаем

    update = {"source_chat": "Сайт-диагност через ELM327", "generated_at": "2026-10-09", "projects": [{
        "id": "obd-site", "name": "OBD сайт", "status": "deployed", "progress_percent": 90,
        "stage": "Работает на машине", "location": {"branch": "main"},
        "recent_changes": [{"date": "2026-10-08", "change": "Быстрое подключение"},
                           {"date": "2026-10-05", "change": "HTTPS на IP"}],
    }]}
    assert store.import_text(json.dumps(update, ensure_ascii=False))["updated"] == ["OBD сайт"]
    obd = store.get("obd-site")
    assert obd["status"] == "deployed" and obd["summary"] == "Чтение ошибок из браузера"  # пустое не затирает
    assert obd["location"]["branch"] == "main" and obd["location"]["deploy_path"].startswith("/opt/obd")
    assert [c["change"] for c in obd["recent_changes"]] == ["Быстрое подключение", "HTTPS на IP"]
    assert [h["status"] for h in obd["history"]] == ["testing", "deployed"] and obd["report_date"] == "2026-10-09"
    assert [p["status"] for p in store.all()] == ["in_progress", "in_progress", "deployed"]


def test_health_url_from_messy_reports():
    url = Projects.health_url
    assert url(normalize(REPORT["projects"][0])) == "https://1.2.3.4/api/health"
    assert url(normalize(SECOND["projects"][0])) == "https://school.example/api/health"
    assert url(normalize(SECOND["projects"][1])) is None  # localhost с сервера проверять бессмысленно
    assert url(normalize({"name": "a", "health_check": "curl -s localhost:8080 на сервере вернёт 200",
                          "location": {"url": "http://1.2.3.4:8080/"}})) == "http://1.2.3.4:8080/"
    assert url(normalize({"name": "c", "health_check": 'curl -s -o /dev/null localhost:8080 вернёт 200; /api/settings отдаёт JSON',
                          "location": {"url": "http://1.2.3.4:8080/"}})) == "http://1.2.3.4:8080/api/settings"
    assert url(normalize({"name": "b", "health_check": "systemctl status; логи в /var/log/b",
                          "location": {"url": "https://b.example"}})) == "https://b.example"


def test_repo_slug():
    assert _repo_slug("0oda0/OBD (приватный, https://github.com/0oda0/OBD)") == "0oda0/obd"
    assert _repo_slug("https://github.com/0oda0/D.N.A.-Detailing (версия до коммита)") == "0oda0/d.n.a.-detailing"
    assert _repo_slug("0oda0/HSKLearnMobil") == "0oda0/hsklearnmobil" and _repo_slug(None) == ""


class _Handler(BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):  # noqa: N802
        self.send_response(type(self).status)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def test_health_check_notifies_on_change(tmp_path, monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    storage = Storage(tmp_path)
    store = Projects(storage)
    # 127.0.0.1 считается локальным адресом, поэтому подменяем выбор адреса
    monkeypatch.setattr(Projects, "health_url", staticmethod(lambda p: f"http://127.0.0.1:{server.server_port}/"))
    store.import_text(json.dumps({"projects": [{"name": "Сайт"}]}, ensure_ascii=False))
    sent = []
    monkeypatch.setattr(sync, "notify", lambda s, settings, text: sent.append(text))
    sync.check_projects(storage)
    assert store.get("sayt")["health"]["ok"] and sent == []  # первая проверка — без уведомления
    _Handler.status = 502
    sync.check_projects(storage)
    health = store.get("sayt")["health"]
    assert not health["ok"] and health["error"] == "HTTP 502" and health["down_since"]
    assert "🔴 Сайт не отвечает" in sent[-1]
    sync.check_projects(storage)
    assert store.get("sayt")["health"]["down_since"] == health["down_since"] and len(sent) == 1
    _Handler.status = 200
    sync.check_projects(storage)
    assert "✅ Сайт снова отвечает" in sent[-1]
    server.shutdown()


def test_projects_in_plan_and_chat_context(tmp_path):
    storage = Storage(tmp_path)
    Projects(storage).import_text(json.dumps(REPORT, ensure_ascii=False))
    block = Projects(storage).prompt_block()
    assert "OBD сайт [тестирование, 70%]: Первая проверка на машине; дальше: Проверить на машине" in block
    context = planner.build_context({"projects": block}, {}, __import__("datetime").date(2026, 10, 7))
    assert "OBD сайт" in context


def test_projects_pages(tmp_path, monkeypatch):
    monkeypatch.delenv("DAILYTIMER_PASSWORD", raising=False)
    monkeypatch.setattr(projects_mod.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(projects_mod.httpx.ConnectError("нет сети")))
    storage = Storage(tmp_path)
    storage.save_snapshot("server_projects", {"projects": [
        {"name": "OBD", "path": "/opt/obd", "github": "0oda0/obd", "branch": "main", "commit": "abc1234def", "behind": 2}]})
    client = TestClient(create_app(storage, start_scheduler=False))
    page = client.get("/projects").text
    assert "Пока пусто" in page and "Скопировать промпт" in page and 'href="/projects"' in page

    r = client.post("/projects/import", data={"report": "```json\n" + json.dumps(REPORT, ensure_ascii=False) + "\n```"})
    assert r.status_code == 200 and "Добавлено: OBD сайт" in r.text
    assert "сервер отстаёт на 2" in r.text  # связка с разделом «Сервер»
    r = client.post("/projects/check")
    assert "Сайты проверены" in r.text and "ConnectError" in r.text
    assert "Не нашёл JSON" in client.post("/projects/import", data={"report": "мусор"}).text

    detail = client.get("/projects/obd-site").text
    assert "Первая проверка на машине" in detail and "HTTPS на IP" in detail and "/opt/obd" in detail
    edited = json.loads(json.dumps({**REPORT["projects"][0], "stage": "Исправлено вручную", "blockers": []}))
    r = client.post("/projects/obd-site/edit", data={"report": json.dumps(edited, ensure_ascii=False)})
    assert "Сохранено" in r.text and "Исправлено вручную" in r.text
    assert Projects(storage).get("obd-site")["blockers"] == []
    assert client.get("/api/projects").json()["projects"][0]["id"] == "obd-site"
    assert client.get("/projects/nope").status_code == 404
    r = client.post("/projects/obd-site/delete")
    assert "Удалён проект «OBD сайт»" in r.text and Projects(storage).get("obd-site") is None
