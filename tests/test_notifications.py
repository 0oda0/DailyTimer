from datetime import date

from dailytimer import __version__, notifications, sync
from dailytimer.storage import Storage

TODAY = date(2026, 10, 5)


def L(day, start, title, loc="А-101"):
    return {"title": title, "start": f"{day}T{start}:00", "end": f"{day}T{start}:00", "location": loc}


def test_schedule_changes():
    old = [L("2026-10-05", "09:30", "Матан"), L("2026-10-06", "11:20", "Сети"),
           L("2026-10-07", "13:00", "Физра"), L("2026-10-08", "09:30", "Право", "Н-458")]
    new = [L("2026-10-05", "09:30", "Матан"),                      # без изменений
           L("2026-10-06", "13:00", "Сети"),                       # перенос
           L("2026-10-08", "09:30", "Право", "онлайн"),            # смена аудитории
           L("2026-10-09", "10:00", "Консультация")]               # новая пара; Физру отменили
    lines = notifications.schedule_changes(old, new, TODAY, TODAY)
    assert lines == [
        "🔀 завтра: Сети перенесли 11:20 → 13:00",
        "❌ ср 07.10 13:00: Физра — отменили",
        "📍 чт 08.10 09:30: Право — теперь онлайн",
        "➕ пт 09.10 10:00: Консультация (А-101)",
    ]
    # Пары, которые просто «въехали» в окно с ходом дней, — не изменения.
    assert notifications.schedule_changes([], [L("2026-10-19", "09:30", "X")], date(2026, 10, 13), date(2026, 10, 5)) == []
    assert notifications.schedule_changes(old, old, TODAY, TODAY) == []


def test_github_receipts_subscriptions_health():
    pr = {"url": "u1", "repo": "a/b", "number": 1, "title": "Fix"}
    assert notifications.github_changes({"review_requests": [pr]}, {"review_requests": [pr]}) == []
    new_pr = {**pr, "url": "u2", "number": 2}
    assert "a/b#2" in notifications.github_changes({"review_requests": [pr]}, {"review_requests": [pr, new_pr]})[0]
    assert notifications.receipt_changes({"receipts": []}, {"receipts": [
        {"uid": "1", "subject": "Чек Пятёрочка", "amount": 450, "currency": "RUB"}]}) == ["🧾 Чек Пятёрочка — 450 RUB"]
    alerts = notifications.subscription_alerts({"items": [
        {"name": "Spotify", "amount": 299, "currency": "RUB", "next_charge": "2026-10-06", "days_left": 1},
        {"name": "Netflix", "next_charge": "2026-10-20", "days_left": 15}]})
    assert len(alerts) == 1 and "завтра спишется Spotify 299 RUB" in alerts[0][1]
    lines = notifications.health_changes({"portal": {"error": None}, "github": {"error": "401"}},
                                         {"portal": "Неверный пароль", "github": None})
    assert lines == ["⚠️ Не получилось обновить личный кабинет вуза: Неверный пароль", "✅ GitHub снова обновляется"]


def test_version_message():
    assert notifications.version_message(__version__) is None
    msg = notifications.version_message("0.3.0")
    assert f"версии {__version__}" in msg and "0.4.0" in msg and "0.2.0" not in msg
    assert "0.4.0" not in notifications.version_message(None)  # первая установка — только текущая


def test_announce_version_once(tmp_path):
    store = Storage(tmp_path)
    sent = []
    notifications.announce_version(store, lambda t: sent.append(t) or True)
    notifications.announce_version(store, lambda t: sent.append(t) or True)
    assert len(sent) == 1
    store2 = Storage(tmp_path / "b")
    notifications.announce_version(store2, lambda t: False)  # не ушло — попробуем при следующем старте
    assert store2.get_snapshot("app_version")["data"] is None


def test_sync_sends_schedule_change(tmp_path, monkeypatch):
    store = Storage(tmp_path)
    store.save_settings({"telegram_bot_token": "x", "ai_mode": "off"})
    today = sync.today_for(store.get_settings())
    day = today.isoformat()
    store.save_snapshot("portal", {"lessons": [L(day, "23:58", "Матан")]})
    sent = []
    monkeypatch.setattr(sync, "notify", lambda s, st, text: sent.append(text) or True)
    sync.sync_all(store)
    assert sent == []  # первая сборка расписания — без уведомлений
    store.save_snapshot("portal", {"lessons": [L(day, "23:59", "Матан")]})
    sync.sync_all(store)
    assert len(sent) == 1 and "Матан перенесли 23:58 → 23:59" in sent[0]
    sync.sync_all(store)
    assert len(sent) == 1  # без изменений — тишина
