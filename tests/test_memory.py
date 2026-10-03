from fastapi.testclient import TestClient

from dailytimer import planner, sync
from dailytimer.assistant import Assistant
from dailytimer.memory import Memory
from dailytimer.storage import Storage
from dailytimer.web.app import create_app


def test_memory_file(tmp_path):
    mem = Memory(Storage(tmp_path))
    assert mem.facts() == [] and mem.prompt_block() == ""
    assert mem.add("по средам тренировка в 19:00") and not mem.add("По средам тренировка в 19:00.")
    mem.add("лучше всего работаю вечером", "из чата")
    assert mem.facts() == ["По средам тренировка в 19:00", "Лучше всего работаю вечером"]
    assert "Лучше всего работаю вечером" in mem.prompt_block()
    assert mem.remove("2") == ["Лучше всего работаю вечером"] and mem.remove("нет такого") == []
    mem.save_raw("# заметки\n- Не ставить ничего до 10 утра\nпросто текст\n* Цель: сессия без троек")
    assert mem.facts() == ["Не ставить ничего до 10 утра", "Цель: сессия без троек"]
    assert (tmp_path / "memory.md").read_text().startswith("# заметки")


class FakeAI:
    name = "fake"

    def __init__(self, answer):
        self.answer, self.prompts = answer, []

    def chat(self, system, user, temperature=0.4, want_json=False):
        self.prompts.append((system, user))
        return self.answer


def test_assistant_remembers_and_uses_memory(tmp_path, monkeypatch):
    store = Storage(tmp_path)
    a = Assistant(store)
    store.save_settings({"ai_mode": "off"})
    assert "Запомнил" in a.reply("запомни, что я сова и лучше работаю после 22")
    fake = FakeAI('Понял, учту.\nACTION: {"type": "remember", "fact": "По пятницам подработка с 15 до 20"}')
    monkeypatch.setattr("dailytimer.assistant.AIClient.from_settings", classmethod(lambda cls, s: fake))
    answer = a.reply("кстати, по пятницам я работаю с 15 до 20")
    assert "ACTION" not in answer and "Запомнил: По пятницам подработка" in answer
    assert "Я сова и лучше работаю после 22" in fake.prompts[0][0]  # память в контексте чата
    assert "По пятницам подработка с 15 до 20" in Memory(store).facts()
    assert "Забыл" in a.reply("забудь сова")


def test_daily_plan_gets_memory(tmp_path, monkeypatch):
    store = Storage(tmp_path)
    Memory(store).add("По средам тренировка в 19:00")
    fake = FakeAI("## План")
    monkeypatch.setattr("dailytimer.planner.AIClient.from_settings", classmethod(lambda cls, s: fake))
    sync.build_plan(store)
    system, user = fake.prompts[0]
    assert "По средам тренировка в 19:00" in user and "память" in system.lower()


def test_memory_page(tmp_path, monkeypatch):
    monkeypatch.delenv("DAILYTIMER_PASSWORD", raising=False)
    store = Storage(tmp_path)
    client = TestClient(create_app(store, start_scheduler=False))
    client.post("/memory/add", data={"fact": "не люблю пары в 8 утра"})
    assert "Не люблю пары в 8 утра" in client.get("/memory").text
    client.post("/memory/delete", data={"index": "1"})
    assert Memory(store).facts() == []
    client.post("/memory/raw", data={"raw": "- Факт из файла"})
    assert Memory(store).facts() == ["Факт из файла"]
