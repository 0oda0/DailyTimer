from datetime import date, datetime

from fastapi.testclient import TestClient

from dailytimer import bot
from dailytimer.assistant import Assistant
from dailytimer.life import Focus, Habits, Journal, week_stats
from dailytimer.storage import Storage
from dailytimer.tasks import Tasks, auto_schedule, next_occurrence, parse_quick, timeline
from dailytimer.web.app import create_app

SAT = date(2026, 10, 3)


def test_quick_add_parsing():
    p = parse_quick("сдать лабу по сетям завтра в 15:00 1ч !1 #учеба", SAT)
    assert (p.title, p.due_date, p.due_time, p.duration, p.priority, p.tags) == (
        "сдать лабу по сетям", "2026-10-04", "15:00", 60, 1, ["учеба"])
    assert parse_quick("созвон в пт в 18", SAT).due_date == "2026-10-09"
    assert parse_quick("отчёт до 15.10", SAT).due_date == "2026-10-15"
    assert parse_quick("курсовая 20 октября 3ч", SAT).duration == 180
    z = parse_quick("зарядка каждый день утром 15м", SAT)
    assert (z.recur, z.due_date, z.due_time, z.duration) == ("daily", "2026-10-03", "09:00", 15)
    assert parse_quick("пробежка по пн", SAT).recur == "weekly:0"
    assert parse_quick("купить молоко", SAT).due_date is None


def test_recurrence():
    assert next_occurrence("daily", SAT) == date(2026, 10, 4)
    assert next_occurrence("weekdays", date(2026, 10, 2)) == date(2026, 10, 5)
    assert next_occurrence("weekly:0", date(2026, 10, 5)) == date(2026, 10, 12)
    assert next_occurrence("monthly", date(2026, 12, 31)) == date(2027, 1, 28)


def test_complete_recurring_creates_next(tmp_path):
    t = Tasks(Storage(tmp_path))
    tid = t.add_quick("зарядка каждый день", SAT)
    new_id = t.complete(tid, SAT)
    assert t.get(tid)["status"] == "done" and t.get(new_id)["due_date"] == "2026-10-04"
    assert t.complete(tid, SAT) is None  # повторное закрытие ничего не делает


def test_auto_schedule_avoids_lessons(tmp_path):
    t = Tasks(Storage(tmp_path))
    t.add_quick("курсовая сегодня 2ч !1", SAT)
    t.add_quick("почта сегодня 30м", SAT)
    t.add_quick("созвон сегодня в 12:00 1ч", SAT)
    lessons = [{"title": "Матан", "start": "2026-10-03T09:30:00", "end": "2026-10-03T11:05:00"}]
    result = auto_schedule(t, SAT, lessons, "09:00", "23:00", now=datetime(2026, 10, 3, 8, 0))
    starts = {x["title"]: x["scheduled_start"][11:16] for x in result["placed"]}
    # 09:00–09:20 мало для 2ч, после пары (11:15) до созвона (12:00) — только 45 мин → курсовая после 13:00
    assert starts["курсовая"] == "13:00" and starts["почта"] == "11:15"
    items = timeline(t, SAT, lessons, "09:00", "23:00")
    kinds = [i["kind"] for i in items]
    assert "lesson" in kinds and "free" in kinds and [i for i in items if i["title"] == "созвон"][0]["start"] == "12:00"


def test_matrix(tmp_path):
    t = Tasks(Storage(tmp_path))
    t.add_quick("пожар сегодня !1", SAT)
    t.add_quick("стратегия !2", SAT)
    t.add_quick("мелочь завтра", SAT)
    t.add_quick("когда-нибудь", SAT)
    m = t.matrix(SAT)
    assert [x["title"] for x in m["do"]] == ["пожар"] and [x["title"] for x in m["plan"]] == ["стратегия"]
    assert [x["title"] for x in m["delegate"]] == ["мелочь"] and [x["title"] for x in m["drop"]] == ["когда-нибудь"]


class FakeAI:
    name = "fake"

    def __init__(self, answer):
        self.answer, self.seen = answer, None

    def chat(self, system, user, temperature=0.4, want_json=False):
        self.seen = (system, user)
        return self.answer


def test_assistant_commands_and_ai_actions(tmp_path, monkeypatch):
    store = Storage(tmp_path)
    a = Assistant(store)
    assert "Добавил задачу #1" in a.reply("добавь сдать лабу завтра в 15 !1 #учеба")
    assert "сдать лабу" in a.reply("/tasks")
    fake = FakeAI('Добавлю и закрою.\nACTION: {"type": "add", "text": "купить хлеб сегодня"}\n'
                  'ACTION: {"type": "done", "id": 1}')
    monkeypatch.setattr("dailytimer.assistant.AIClient.from_settings", classmethod(lambda cls, s: fake))
    answer = a.reply("купи хлеб и закрой лабу")
    assert "ACTION" not in answer and "Добавил задачу #2" in answer and "Готово: «сдать лабу»" in answer
    assert "#1 [на неделе] сдать лабу" in fake.seen[0] and "Пользователь: купи хлеб" in fake.seen[1]  # контекст и история
    assert [m["role"] for m in a.history()] == ["user", "assistant"] * 3


def test_habits_focus_journal(tmp_path):
    store = Storage(tmp_path)
    h = Habits(store)
    hid = h.add("Зарядка", "🏃", 5)
    h.toggle(hid, date(2026, 10, 1)); h.toggle(hid, date(2026, 10, 2)); h.toggle(hid, SAT)
    info = h.overview(SAT)[0]
    assert info["streak"] == 3 and info["done_today"] and info["week_count"] == 3
    assert h.toggle(hid, SAT) is False and h.overview(SAT)[0]["streak"] == 2  # серия до вчера сохраняется
    Focus(store).log(25)
    Journal(store).save(SAT, 4, "сдал лабу", "", "")
    assert Journal(store).get(SAT)["mood"] == 4
    assert week_stats(store, datetime.now().date())["total_focus"] == 25


def test_bot_owner_only_and_reminders(tmp_path):
    store = Storage(tmp_path)
    store.save_settings({"ai_mode": "off"})
    a = Assistant(store)
    msg = lambda chat, text: {"message": {"chat": {"id": chat, "type": "private"}, "text": text}}  # noqa: E731
    assert bot.handle_update(store, msg(111, "/start"), a)[1].startswith("Привет")
    assert store.get_settings()["telegram_chat_id"] == "111"
    assert "только владельцу" in bot.handle_update(store, msg(222, "/tasks"), a)[1]
    assert "Добавил" in bot.handle_update(store, msg(111, "добавь созвон сегодня в 12:00"), a)[1]
    store.save_snapshot("schedule", {"lessons": [
        {"title": "Матан", "start": f"{SAT}T09:30:00", "end": f"{SAT}T11:05:00", "location": "А-101"}]})
    Tasks(store).add_quick("созвон 2 сегодня в 12:00", SAT)
    reminders = bot.due_reminders(store, datetime(2026, 10, 3, 9, 20))
    assert any("Матан" in text and "А-101" in text for _, text in reminders)
    assert any("созвон" in text for _, text in bot.due_reminders(store, datetime(2026, 10, 3, 11, 52)))
    assert bot.due_reminders(store, datetime(2026, 10, 3, 14, 0)) == []


def test_pages_and_calendar(tmp_path, monkeypatch):
    monkeypatch.delenv("DAILYTIMER_PASSWORD", raising=False)
    store = Storage(tmp_path)
    store.save_settings({"ai_mode": "off"})
    client = TestClient(create_app(store, start_scheduler=False))
    client.post("/tasks/add", data={"text": "лаба сегодня в 15:00 !1 #учеба"})
    client.post("/habits/add", data={"name": "Зарядка", "icon": "🏃", "per_week": "7"})
    for url in ("/", "/tasks", "/tasks?view=matrix", "/chat", "/habits", "/focus", "/review", "/manifest.webmanifest"):
        resp = client.get(url)
        assert resp.status_code == 200, url
    assert "лаба" in client.get("/").text and "Зарядка" in client.get("/").text
    reply = client.post("/api/chat", json={"text": "что сегодня"}).json()
    assert "лаба" in reply["reply"]
    assert client.post("/api/focus", json={"minutes": 25}).json()["today"] == 25
    link = client.get("/calendar/link").text
    assert client.get("/calendar.ics?token=wrong").status_code == 403
    ics = client.get(link[link.index("/calendar.ics"):]).text
    assert "BEGIN:VCALENDAR" in ics and "лаба" in ics and "TRIGGER:-PT10M" in ics
    client.post("/review/save", data={"day": "2026-10-03", "mood": "5", "wins": "ok",
                                      "tomorrow": "семинар в 10", "make_tasks": "on"})
    assert any(t["title"] == "семинар" and t["due_date"] == "2026-10-04" for t in Tasks(store).open_tasks())
