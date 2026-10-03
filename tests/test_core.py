from datetime import date
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from dailytimer import planner, sorter
from dailytimer.ai import extract_json
from dailytimer.connectors import gmail, schedule, subscriptions
from dailytimer.storage import Storage
from dailytimer.web.app import create_app, render_markdown

MONDAY = date(2026, 10, 5)


def test_storage_encrypts_secrets(tmp_path):
    store = Storage(tmp_path)
    store.save_settings({"github_token": "ghp_secret", "timezone": "Europe/Moscow"})
    raw = (tmp_path / "dailytimer.db").read_bytes()
    assert b"ghp_secret" not in raw
    assert Storage(tmp_path).get_settings()["github_token"] == "ghp_secret"


def test_snapshot_error_keeps_last_data(tmp_path):
    store = Storage(tmp_path)
    store.save_snapshot("github", {"login": "me"})
    store.save_snapshot("github", None, error="boom")
    snap = store.get_snapshot("github")
    assert snap["data"] == {"login": "me"} and snap["error"] == "boom"


def test_manual_schedule_and_parity():
    text = "пн 09:00-10:30 Матанализ, ауд. 301\nвт 10:40-12:10 Физика чёт\nвт 10:40-12:10 Химия нечёт"
    lessons = schedule.parse_manual(text, MONDAY, 7)
    titles = [l["title"] for l in lessons]
    assert "Матанализ, ауд. 301" in titles
    # 06.10.2026 — неделя 41, нечётная
    assert "Химия" in titles and "Физика" not in titles


def test_ics_recurring():
    ics = b"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:1
SUMMARY:Algorithms
LOCATION:A-101
DTSTART;TZID=Europe/Moscow:20260907T090000
DTEND;TZID=Europe/Moscow:20260907T103000
RRULE:FREQ=WEEKLY
END:VEVENT
END:VCALENDAR"""
    lessons = schedule.parse_ics(ics, MONDAY, 7, ZoneInfo("Europe/Moscow"))
    assert lessons == [
        {"title": "Algorithms", "start": "2026-10-05T09:00:00", "end": "2026-10-05T10:30:00",
         "location": "A-101", "source": "ics"}
    ]


def test_rules_classification():
    assert sorter.classify_by_rules({"from": "GitHub <notifications@github.com>", "subject": "PR"}) == "dev"
    assert sorter.classify_by_rules({"from": "shop@x.ru", "subject": "Скидка 50% на всё"}) == "promo"
    result = sorter.classify([{"uid": "1", "from": "friend@gmail.com", "subject": "привет"}], ai=None)
    assert result == {"1": "other"}


def test_subscription_detection():
    mails = [{"from": "Spotify <no-reply@spotify.com>", "subject": "Ваш чек", "snippet": "Списано 299 ₽",
              "date": "2026-09-20T10:00:00+03:00"}]
    found = subscriptions.from_emails(mails)
    assert found[0]["name"] == "Spotify" and found[0]["amount"] == 299 and found[0]["currency"] == "RUB"
    manual = subscriptions.parse_manual("ChatGPT 20 USD 3", MONDAY)
    merged = subscriptions.merge(manual, found, MONDAY)
    assert {s["name"] for s in merged} == {"Spotify", "ChatGPT"}
    assert next(s for s in merged if s["name"] == "ChatGPT")["next_charge"] == "2026-11-03"


def test_amount_formats():
    assert subscriptions.find_amount("Total: $9.99") == (9.99, "USD")
    assert subscriptions.find_amount("Сумма 1 490,00 руб.") == (1490.0, "RUB")


def test_parse_message_html_only():
    raw = (b"From: =?utf-8?b?0KPQvdC40LLQtdGA?= <u@uni.edu>\r\nSubject: Test\r\n"
           b"Content-Type: text/html; charset=utf-8\r\n\r\n<p>Hello <b>world</b></p><style>x{}</style>")
    mail = gmail.parse_message("7", raw)
    assert mail["from"].startswith("Универ") and mail["snippet"] == "Hello world"


def test_rule_based_plan():
    data = {
        "schedule": {"lessons": [{"title": "Матан", "start": "2026-10-05T09:00:00", "end": "2026-10-05T10:30:00"}]},
        "github": {"review_requests": [{"repo": "a/b", "number": 1, "title": "Fix", "url": "https://x"}]},
        "gmail": {"messages": []},
        "subscriptions": {"items": []},
    }
    text, engine = planner.make_plan(data, {"wake_time": "08:00", "sleep_time": "23:00"}, MONDAY)
    assert engine == "rules" and "Матан" in text and "a/b#1" in text


def test_extract_json():
    assert extract_json('Вот:\n```json\n{"1": "promo"}\n```') == {"1": "promo"}


def test_markdown_escapes_html():
    out = render_markdown("## Hi\n- <script>x</script> **bold**")
    assert "<script>" not in out and "<strong>bold</strong>" in out


def test_web_auth_and_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DAILYTIMER_PASSWORD", "pw")
    client = TestClient(create_app(Storage(tmp_path), start_scheduler=False))
    assert client.get("/").status_code == 401
    auth = ("admin", "pw")
    assert client.get("/", auth=auth).status_code == 200
    resp = client.post("/settings", auth=auth, data={"github_token": "zz_secret_tok", "sync_interval_minutes": "30"},
                       follow_redirects=False)
    assert resp.status_code == 303
    page = client.get("/settings", auth=auth).text
    assert "zz_secret_tok" not in page and "••••••••" in page
    client.post("/settings", auth=auth, data={"github_token": "••••••••"})
    assert Storage(tmp_path).get_settings()["github_token"] == "zz_secret_tok"
    assert client.post("/plan", auth=auth).status_code == 200


def test_web_without_password_is_open(tmp_path, monkeypatch):
    monkeypatch.delenv("DAILYTIMER_PASSWORD", raising=False)
    client = TestClient(create_app(Storage(tmp_path), start_scheduler=False))
    assert client.get("/").status_code == 200
