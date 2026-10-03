import os
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
import vps_agent  # noqa: E402

from dailytimer import notifications  # noqa: E402
from dailytimer.connectors import vps  # noqa: E402
from dailytimer.storage import Storage  # noqa: E402
from dailytimer.web.app import create_app  # noqa: E402


def git(*args, cwd):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True,
                   capture_output=True)


@pytest.fixture
def projects(tmp_path):
    """Корень с проектом, отстающим от origin на 1 коммит, плюс мусорные репозитории."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    seed = tmp_path / "seed"
    git("clone", "-q", str(origin), str(seed), cwd=tmp_path)
    (seed / "README").write_text("v1")
    git("add", ".", cwd=seed); git("commit", "-qm", "v1", cwd=seed); git("push", "-q", "origin", "HEAD:main", cwd=seed)
    root = tmp_path / "srv"
    root.mkdir()
    app = root / "myapp"
    git("clone", "-q", str(origin), str(app), cwd=tmp_path)
    (seed / "README").write_text("v2")
    git("commit", "-qam", "Новая фича", cwd=seed); git("push", "-q", "origin", "HEAD:main", cwd=seed)
    (root / "nvm").mkdir(); git("init", "-q", cwd=root / "nvm")                 # служебный — пропустить
    (app / "vendor").mkdir(); git("init", "-q", cwd=app / "vendor")             # вложенный — пропустить
    os.environ["AGENT_ROOTS"] = str(root)
    yield app
    os.environ.pop("AGENT_ROOTS", None)


@pytest.fixture
def agent_server():
    os.environ["AGENT_TOKEN"] = "secret-token"
    server = vps_agent.ThreadingHTTPServer(("127.0.0.1", 0), vps_agent.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_proc_net_parsing():
    tcp = ("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
           "   0: 00000000:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 12345 1\n"
           "   1: 0100007F:0CEA 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 23456 1\n"
           "   2: 0100007F:0CEA 0100007F:D431 01 00000000:00000000 00:00000000 00000000     0        0 0 1\n")
    rows = vps_agent.parse_proc_net(tcp, "tcp")
    assert [(r["ip"], r["port"]) for r in rows] == [("0.0.0.0", 8080), ("127.0.0.1", 3306)]
    assert vps_agent._hex_addr("00000000000000000000000001000000:0050") == ("::1", 80)
    assert vps_agent.github_slug("git@github.com:0oda0/DailyTimer.git") == "0oda0/DailyTimer"
    assert vps_agent.github_slug("https://github.com/0oda0/DailyTimer") == "0oda0/DailyTimer"


def test_discovery_and_project_info(projects):
    assert vps_agent.discover_projects() == [str(projects)]
    info = vps_agent.project_info(str(projects), fetch=True)
    assert info["behind"] == 1 and info["ahead"] == 0 and info["branch"] == "main"
    assert info["upstream"]["message"] == "Новая фича" and info["commit"]["message"] == "v1"
    assert info["update_method"] == "git pull" and not info["dirty"]


def test_agent_http_and_update_via_dailytimer(projects, agent_server, tmp_path, monkeypatch):
    import httpx
    assert httpx.get(agent_server + "/status").status_code == 401
    client = vps.Agent(agent_server, "secret-token")
    status = client.status()
    assert status["cpus"] >= 1 and status["memory"]["total"] > 0 and isinstance(status["ports"], list)
    assert client.projects(fetch=True)[0]["behind"] == 1
    with pytest.raises(vps.AgentError):
        client.update("/etc")  # не проект — отказ
    with pytest.raises(vps.AgentError):
        vps.Agent(agent_server, "wrong").status()

    # Обновление через DailyTimer, как кнопкой на сайте.
    monkeypatch.delenv("DAILYTIMER_PASSWORD", raising=False)
    monkeypatch.setattr(vps, "github_status", lambda *a: {})
    store = Storage(tmp_path / "dt")
    store.save_settings({"server_agent_url": agent_server, "server_agent_token": "secret-token"})
    web = TestClient(create_app(store, start_scheduler=False))
    page = web.get("/server").text
    assert "Процессор" in page and "Порты" in page
    web.post("/server/check")
    for _ in range(50):
        if (store.get_snapshot("server_projects")["data"] or {}).get("projects"):
            break
        time.sleep(0.1)
    assert "отстаёт на 1" in web.get("/server").text
    job_id = web.post("/api/server/update", json={"path": str(projects)}).json()["job"]
    for _ in range(100):
        job = web.get(f"/api/server/jobs/{job_id}").json()
        if job["status"] != "running":
            break
        time.sleep(0.1)
    assert job["status"] == "ok", job["log"]
    assert (projects / "README").read_text() == "v2"
    assert vps_agent.project_info(str(projects))["behind"] == 0


def test_update_without_upstream_tracking(projects, agent_server):
    """Ветка без настроенного отслеживания (как /opt/hsklearn): git pull падал, теперь обновляется."""
    git("branch", "--unset-upstream", cwd=projects)
    plain = subprocess.run(["git", "pull", "--ff-only"], cwd=projects, capture_output=True, text=True)
    assert plain.returncode != 0 and "no tracking information" in plain.stderr  # воспроизведение
    client = vps.Agent(agent_server, "secret-token")
    job_id = client.update(str(projects))
    for _ in range(100):
        job = client.job(job_id)
        if job["status"] != "running":
            break
        time.sleep(0.1)
    assert job["status"] == "ok", job["log"]
    assert (projects / "README").read_text() == "v2"


def test_server_notifications():
    old = {"ports": [{"proto": "tcp", "port": 22, "public": True, "process": "sshd"}],
           "containers": [{"name": "db", "state": "running", "status": "Up"}]}
    new = {"ports": [{"proto": "tcp", "port": 22, "public": True, "process": "sshd"},
                     {"proto": "tcp", "port": 5432, "public": True, "container": "db"},
                     {"proto": "tcp", "port": 6379, "public": False, "process": "redis"}],
           "containers": [{"name": "db", "state": "exited", "status": "Exited (1)"}]}
    assert notifications.server_changes(old, new) == [
        "🔓 Открылся публичный порт 5432/tcp — db", "🛑 Контейнер db остановился: Exited (1)"]
    disks = {"disks": [{"mount": "/", "percent": 93.5, "free": 2 * 1024 ** 3}, {"mount": "/data", "percent": 40, "free": 1}]}
    alerts = notifications.disk_alerts(disks, 90, date(2026, 10, 3))
    assert len(alerts) == 1 and "заполнен на 93.5%" in alerts[0][1]
    old_p = [{"path": "/a", "name": "a", "behind": 0, "github": "u/a", "ci": {"id": 1, "conclusion": "success"}}]
    new_p = [{"path": "/a", "name": "a", "behind": 2, "upstream": {"message": "fix"}, "github": "u/a",
              "ci": {"id": 2, "conclusion": "failure", "name": "CI", "sha": "abc", "url": "u"}}]
    lines = notifications.project_changes(old_p, new_p)
    assert lines[0].startswith("⬆️ Для a есть обновление (2 коммит.)") and lines[1].startswith("❌ CI упал в u/a")
