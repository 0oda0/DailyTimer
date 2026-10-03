from datetime import date
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from dailytimer import planner, sorter
from dailytimer.ai import AIClient, AIError, Backend, extract_json
from dailytimer.connectors import feeds, gmail, mtuci, portal, schedule, subscriptions, tg_account
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


def test_rules_analysis():
    assert sorter.rule_analysis({"from": "GitHub <notifications@github.com>", "subject": "PR"})["category"] == "dev"
    promo = sorter.rule_analysis({"from": "shop@x.ru", "subject": "Скидка 50% на всё", "list_unsubscribe": True})
    assert promo["category"] == "promo" and not promo["important"]
    receipt = sorter.rule_analysis({"from": "noreply@ofd.ru", "subject": "Кассовый чек", "snippet": "Итого 450 ₽"})
    assert receipt["category"] == "receipts"
    ask = sorter.rule_analysis({"from": "Иван <ivan@gmail.com>", "subject": "Встреча",
                                "snippet": "Привет! Сможешь завтра в 15:00 созвониться?"})
    assert ask["needs_reply"] and ask["important"] and ask["category"] == "important"
    assert sorter.analyze([{"uid": "1", "from": "friend@gmail.com", "subject": "привет"}], ai=None)["1"]["category"] == "personal"


class FakeAI:
    name = "fake"

    def __init__(self, answer):
        self.answer = answer

    def chat_json(self, system, user):
        return self.answer


def test_ai_analysis_overrides_rules_but_keeps_receipts():
    mails = [
        {"uid": "1", "from": "prof@uni.ru", "subject": "Курсовая", "snippet": "Пришлите работу"},
        {"uid": "2", "from": "noreply@ofd.ru", "subject": "Кассовый чек", "snippet": "450 ₽"},
    ]
    ai = FakeAI({"1": {"c": "study", "imp": 1, "reply": 1, "sum": "Нужно сдать курсовую"},
                 "2": {"c": "promo", "imp": 0, "reply": 0, "sum": "чек"}})
    result = sorter.analyze(mails, ai)
    assert result["1"] == {"category": "study", "important": True, "needs_reply": True,
                           "summary": "Нужно сдать курсовую", "by": "fake"}
    assert result["2"]["category"] == "receipts" and not result["2"]["needs_reply"]


def test_spam_rescue_rules():
    scam = {"category": "personal", "important": False, "needs_reply": False, "by": "rules"}
    assert not sorter.should_rescue_from_spam({}, scam)
    assert sorter.should_rescue_from_spam({}, {**scam, "category": "study"})
    assert sorter.should_rescue_from_spam({}, {**scam, "by": "fake", "important": True})
    assert not sorter.should_rescue_from_spam({}, {**scam, "by": "fake", "category": "promo", "important": True})


def test_portal_heuristic_parse():
    text = """Расписание на неделю
Понедельник, 05.10.2026
1 пара 09:00-10:30 Математический анализ (лекция) ауд. 301
10:40 – 12:10
Программирование
Вторник 06.10
13:00-14:30 Физика"""
    lessons = portal.heuristic_parse(text, MONDAY)
    assert [(l["start"], l["title"]) for l in lessons] == [
        ("2026-10-05T09:00:00", "Математический анализ (лекция) ауд. 301"),
        ("2026-10-05T10:40:00", "Программирование"),
        ("2026-10-06T13:00:00", "Физика"),
    ]


def test_portal_ai_parse():
    ai = FakeAI({"lessons": [{"date": "2026-10-05", "start": "9.00", "end": "10:30", "title": "Матан",
                              "location": "301", "teacher": "Иванов"}, {"bad": 1}]})
    lessons = portal.ai_parse("...", MONDAY, ai)
    assert lessons == [{"title": "Матан", "start": "2026-10-05T09:00:00", "end": "2026-10-05T10:30:00",
                        "location": "301 · Иванов", "source": "portal"}]


def test_ai_chain_falls_back(monkeypatch):
    class Broken(Backend):
        name = "broken"

        def chat(self, *a):
            raise AIError("down")

    class Works(Backend):
        name = "works"

        def chat(self, *a):
            return '{"ok": 1}'

    client = AIClient([Broken(), Works()])
    assert client.chat_json("s", "u") == {"ok": 1} and client.name == "works"
    assert AIClient.from_settings({"ai_mode": "off"}) is None
    names = [b.name for b in AIClient.from_settings({"ai_mode": "auto"}).backends]
    assert names[0].startswith("local:") and "pollinations" in names[1]


def test_rss_parse():
    rss = b"""<?xml version="1.0"?><rss><channel><item><title>Hello</title><link>https://a</link>
    <description>&lt;p&gt;Text&lt;/p&gt;</description></item></channel></rss>"""
    atom = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>A</title>
    <link href="https://b"/><updated>2026</updated></entry></feed>"""
    assert feeds.parse_feed(rss)[0] == {"title": "Hello", "url": "https://a", "date": "", "summary": "Text"}
    assert feeds.parse_feed(atom)[0]["url"] == "https://b"


def test_special_folders():
    lines = [b'(\\HasNoChildren \\All) "/" "[Gmail]/&BBIEQQRP- &BD8EPgRHBEIEMA-"',
             b'(\\HasNoChildren \\Junk) "/" "[Gmail]/Spam"', b'(\\HasNoChildren) "/" "INBOX"']
    folders = gmail._special_folders(lines)
    assert folders["\\Junk"] == '"[Gmail]/Spam"' and folders["\\All"].startswith('"[Gmail]/&BBI')


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
    resp = client.post("/settings/dev", auth=auth, data={"github_token": "zz_secret_tok"},
                       follow_redirects=False)
    assert resp.status_code == 303
    page = client.get("/settings/dev", auth=auth).text
    assert "zz_secret_tok" not in page and "••••••••" in page
    client.post("/settings/dev", auth=auth, data={"github_token": "••••••••"})
    assert Storage(tmp_path).get_settings()["github_token"] == "zz_secret_tok"
    assert client.post("/plan", auth=auth).status_code == 200


def test_web_without_password_is_open(tmp_path, monkeypatch):
    monkeypatch.delenv("DAILYTIMER_PASSWORD", raising=False)
    client = TestClient(create_app(Storage(tmp_path), start_scheduler=False))
    assert client.get("/").status_code == 200


def test_settings_sections_keep_other_checkboxes(tmp_path, monkeypatch):
    monkeypatch.delenv("DAILYTIMER_PASSWORD", raising=False)
    client = TestClient(create_app(Storage(tmp_path), start_scheduler=False))
    overview = client.get("/settings").text
    for title in ("Учёба", "Почта", "Соцсети и мессенджеры", "Разработка", "Финансы и подписки", "ИИ"):
        assert title in overview
    for section in ("study", "mail", "social", "dev", "money", "ai", "other"):
        assert client.get(f"/settings/{section}").status_code == 200
    assert client.get("/settings/nope").status_code == 404
    # Сохранение раздела «Учёба» не должно снимать галочки почты.
    client.post("/settings/study", data={"schedule_portal_url": "https://lk.mtuci.ru/student/schedule",
                                         "schedule_login": "a@edu.mtuci.ru", "schedule_password": "pw"})
    saved = Storage(tmp_path).get_settings()
    assert saved["gmail_cleanup"] and saved["schedule_password"] == "pw"
    client.post("/settings/mail", data={"gmail_email": "x@gmail.com"})
    assert not Storage(tmp_path).get_settings()["gmail_cleanup"]
    assert "кабинет подключён" in client.get("/settings").text


MTUCI_TIMETABLE = {
    "status": "success",
    "data": {"days": {
        "05.10.2026": [
            {"UF_DISCIPLINE": "Основы права", "UF_TIME_START": "13:00", "UF_TIME_END": "14:30",
             "UF_TEACHER": ["Иванова Н.В."], "UF_AUDIENCE": ["Н-458"], "UF_TYPE": "Лекции",
             "UF_IS_RETAKE": "0", "UF_IS_ONLINE": "1", "UF_NUMBER": "3"},
            {"UF_DISCIPLINE": "Матанализ", "UF_TIME_START": "09:30", "UF_TIME_END": "11:05",
             "UF_TEACHER": [], "UF_AUDIENCE": ["А-101"], "UF_TYPE": "Экзамен", "UF_IS_RETAKE": "1"},
        ],
        "30.09.2026": [{"UF_DISCIPLINE": "Прошлое", "UF_TIME_START": "09:30", "UF_TIME_END": "11:05"}],
        "06.10.2026": [],
    }},
}


def test_mtuci_timetable_parse():
    lessons = mtuci.parse_timetable(MTUCI_TIMETABLE, MONDAY, 14)
    assert [(l["start"], l["title"], l["location"]) for l in lessons] == [
        ("2026-10-05T09:30:00", "Матанализ (экзамен) — пересдача", "А-101"),
        ("2026-10-05T13:00:00", "Основы права (лекция)", "онлайн · Н-458 · Иванова Н.В."),
    ]
    assert mtuci.is_mtuci("https://lk.mtuci.ru/student/schedule") and not mtuci.is_mtuci("https://lk.hse.ru")
    profile = {"data": {"Ответ": {"МассивБлоков": [{"ПереченьЗначений": {"Группа": {"name": "БВТ2401"}}}]}}}
    assert mtuci.parse_group(profile) == "БВТ2401" and mtuci.parse_group({}) is None
    try:
        mtuci.parse_timetable({"status": "error"}, MONDAY, 7)
        raise AssertionError("ожидалась ошибка")
    except mtuci.MtuciError:
        pass


def test_tg_dialog_summary():
    from datetime import datetime, timezone
    from types import SimpleNamespace as NS

    def dialog(name, unread, *, user=True, group=False, out=False, mentions=0, muted=False, bot=False):
        mute = datetime(2100, 1, 1, tzinfo=timezone.utc) if muted else None
        return NS(name=name, unread_count=unread, unread_mentions_count=mentions, is_user=user, is_group=group,
                  entity=NS(bot=bot), dialog=NS(notify_settings=NS(mute_until=mute)),
                  message=NS(message="Привет, ты где?", out=out, date=datetime(2026, 10, 3, tzinfo=timezone.utc)))

    friend = tg_account.summarize_dialog(dialog("Аня", 2))
    assert friend["waiting"] and friend["kind"] == "user" and friend["text"] == "Привет, ты где?"
    assert tg_account.summarize_dialog(dialog("Аня", 0)) is None
    assert not tg_account.summarize_dialog(dialog("Я ответил", 1, out=True))["waiting"]
    assert tg_account.summarize_dialog(dialog("Канал", 50, user=False)) is None
    assert tg_account.summarize_dialog(dialog("Флуд", 99, user=False, group=True, muted=True)) is None
    assert tg_account.summarize_dialog(dialog("Группа", 9, user=False, group=True, muted=True, mentions=1))["mentions"] == 1
    assert tg_account.summarize_dialog(dialog("Бот", 1, bot=True))["kind"] == "bot"
