"""Чат с ИИ-ассистентом: знает расписание, задачи, почту, Telegram и привычки, умеет действовать.

Простые команды выполняются сразу, без ИИ (работают даже на слабом сервере):
  добавь/напомни …, /add …   — новая задача (быстрый ввод)
  готово …, /done …          — закрыть задачу (по номеру или части названия)
  /today, что сегодня        — сводка дня
  /plan, распланируй день     — разложить задачи по свободным окнам
  /tasks                      — список задач
Всё остальное отвечает ИИ; если он решит что-то сделать, он пишет строки ACTION: {...}.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any

from . import planner, sync
from .ai import AIClient, AIError
from .life import Habits
from .memory import Memory
from .storage import Storage
from .tasks import PRIORITY_NAMES, Tasks, auto_schedule, decorate, parse_quick

log = logging.getLogger(__name__)

HELP = (
    "Я твой ассистент. Можно писать обычным текстом, а ещё:\n"
    "• «добавь сдать лабу завтра в 15 !1 #учеба» — задача\n"
    "• «готово лаба» или «/done 12» — закрыть задачу\n"
    "• «что сегодня» — сводка дня, «распланируй день» — разложу задачи по свободным окнам\n"
    "• «/tasks» — список задач\n"
    "• «запомни, что по средам у меня тренировка в 19:00» — факт о тебе для будущих планов\n"
    "• «что ты обо мне помнишь?», «забудь тренировка»"
)

_ADD = re.compile(r"^\s*(?:/add|добавь(?:\s+задачу)?|напомни(?:\s+мне)?|запиши|задача:)\s*:?\s+(.+)$", re.I | re.S)
_DONE = re.compile(r"^\s*(?:/done|готово|сделал[аи]?|выполнил[аи]?|закрой)\s*:?\s+(.+)$", re.I)
_TODAY = re.compile(r"^\s*(?:/today|/start|что (?:у меня )?сегодня\??|план на сегодня\??)\s*$", re.I)
_PLAN = re.compile(r"^\s*(?:/plan|распланируй(?: мой)? день|разложи задачи)\s*$", re.I)
_TASKS = re.compile(r"^\s*(?:/tasks|мои задачи|список задач)\s*$", re.I)
_REMEMBER = re.compile(r"^\s*(?:/remember|запомни(?:,?\s*что)?)\s*[:,]?\s+(.+)$", re.I | re.S)
_FORGET = re.compile(r"^\s*(?:/forget|забудь(?:,?\s*что)?)\s*[:,]?\s+(.+)$", re.I | re.S)
_RECALL = re.compile(r"^\s*(?:/memory|что ты (?:обо мне )?(?:знаешь|помнишь)(?: обо мне)?\??|моя память)\s*$", re.I)
_HELP = re.compile(r"^\s*(?:/help|помощь|что ты умеешь\??)\s*$", re.I)
_ACTION = re.compile(r"^\s*ACTION:\s*(\{.*\})\s*$", re.M)

SYSTEM = """Ты — личный ассистент-планировщик студента МТУСИ, который ещё и программирует.
Отвечай по-русски, коротко и по делу, с конкретикой из данных ниже. Не выдумывай фактов.
Ты можешь действовать: в конце ответа добавь отдельные строки (по одной на действие):
ACTION: {"type": "add", "text": "<задача в формате быстрого ввода: название, дата, время, длительность, !приоритет, #тег>"}
ACTION: {"type": "done", "id": <номер задачи>}
ACTION: {"type": "move", "id": <номер>, "date": "YYYY-MM-DD", "time": "HH:MM или пусто"}
ACTION: {"type": "plan"}  — разложить задачи на сегодня по свободным окнам
ACTION: {"type": "remember", "fact": "<устойчивый факт о пользователе одной фразой>"}
ACTION: {"type": "forget", "fact": "<часть текста факта, который больше не верен>"}
Задачи добавляй только если пользователь об этом просит или явно согласен.
Если пользователь сообщил о себе что-то устойчивое (режим дня, предпочтения, цели, ограничения,
регулярные дела) — запомни это через remember, без лишних вопросов. Разовые события не запоминай.

Сейчас: {now}.

Данные пользователя:
{context}
"""


class Assistant:
    def __init__(self, storage: Storage):
        self.db = storage
        self.tasks = Tasks(storage)

    # ---------------------------------------------------------------- история

    def history(self, channel: str = "web", limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM chat WHERE channel = ? ORDER BY id DESC LIMIT ?", (channel, limit))
        return list(reversed(rows))

    def clear(self, channel: str = "web") -> None:
        self.db.execute("DELETE FROM chat WHERE channel = ?", (channel,))

    def _save(self, role: str, content: str, channel: str) -> None:
        self.db.execute("INSERT INTO chat(role, content, channel, created_at) VALUES(?, ?, ?, ?)",
                        (role, content, channel, datetime.now().isoformat(timespec="seconds")))

    # ---------------------------------------------------------------- ответ

    def reply(self, text: str, channel: str = "web") -> str:
        text = text.strip()
        if not text:
            return ""
        self._save("user", text, channel)
        answer = self._answer(text, channel)
        self._save("assistant", answer, channel)
        return answer

    def _answer(self, text: str, channel: str) -> str:
        settings = self.db.get_settings()
        today = sync.today_for(settings)
        if _HELP.match(text):
            return HELP
        if m := _ADD.match(text):
            return self._add(m.group(1), today)
        if m := _DONE.match(text):
            return self._done(m.group(1), today)
        if _TODAY.match(text):
            return self.today_summary(today)
        if _PLAN.match(text):
            return self._plan(today)
        if _TASKS.match(text):
            return self._list(today)
        if m := _REMEMBER.match(text):
            return self._remember(m.group(1))
        if m := _FORGET.match(text):
            return self._forget(m.group(1))
        if _RECALL.match(text):
            return self._recall()

        ai = AIClient.from_settings(settings)
        if ai is None:
            return "ИИ выключен в настройках, но команды работают.\n\n" + HELP
        system = SYSTEM.replace("{now}", datetime.now().strftime("%d.%m.%Y %H:%M, %A")).replace(
            "{context}", self.context(today))
        dialog = "\n".join(
            f"{'Пользователь' if m['role'] == 'user' else 'Ассистент'}: {m['content']}"
            for m in self.history(channel, limit=12)
        )
        try:
            raw = ai.chat(system, dialog, temperature=0.4)
        except AIError as exc:
            log.warning("Чат: ИИ недоступен: %s", exc)
            return "ИИ сейчас недоступен (модель, возможно, ещё скачивается). Команды работают:\n\n" + HELP
        return self._apply_actions(raw, today)

    # ---------------------------------------------------------------- действия

    def _apply_actions(self, raw: str, today: date) -> str:
        done_notes = []
        for match in _ACTION.finditer(raw):
            try:
                action = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            kind = action.get("type")
            try:
                if kind == "add" and action.get("text"):
                    done_notes.append(self._add(str(action["text"]), today))
                elif kind == "done" and action.get("id") is not None:
                    done_notes.append(self._done(str(action["id"]), today))
                elif kind == "move" and action.get("id") is not None:
                    task = self.tasks.get(int(action["id"]))
                    if task:
                        self.tasks.update(task["id"], due_date=action.get("date") or task["due_date"],
                                          due_time=action.get("time") or None, scheduled_start=None)
                        done_notes.append(f"📅 Перенёс «{task['title']}» на {action.get('date')} {action.get('time') or ''}".strip())
                elif kind == "plan":
                    done_notes.append(self._plan(today))
                elif kind == "remember" and action.get("fact"):
                    done_notes.append(self._remember(str(action["fact"]), source="из чата"))
                elif kind == "forget" and action.get("fact"):
                    done_notes.append(self._forget(str(action["fact"])))
            except (ValueError, TypeError) as exc:
                log.warning("Не удалось выполнить действие %s: %s", action, exc)
        text = _ACTION.sub("", raw).strip()
        return "\n\n".join(x for x in [text, *done_notes] if x)

    def _remember(self, fact: str, source: str = "") -> str:
        if Memory(self.db).add(fact, source):
            return f"🧠 Запомнил: {fact.strip()}"
        return "🧠 Это я уже помню."

    def _forget(self, query: str) -> str:
        removed = Memory(self.db).remove(query)
        if not removed:
            return f"Не нашёл в памяти «{query.strip()}». Посмотреть всё: «что ты обо мне помнишь?»"
        return "🗑 Забыл: " + "; ".join(removed)

    def _recall(self) -> str:
        facts = Memory(self.db).facts()
        if not facts:
            return "Пока ничего о тебе не помню. Расскажи: «запомни, что …»"
        return "🧠 Что я о тебе помню:\n" + "\n".join(f"{i}. {f}" for i, f in enumerate(facts, 1)) + \
            "\n\nУдалить: «забудь <номер или слово>»"

    def _add(self, text: str, today: date) -> str:
        parsed = parse_quick(text, today)
        task_id = self.tasks.add(**parsed.as_dict())
        when = ""
        if parsed.due_date:
            when = " — " + decorate({"due_date": parsed.due_date}, today)["due_label"]
            if parsed.due_time:
                when += f" в {parsed.due_time}"
        extra = []
        if parsed.priority < 4:
            extra.append(PRIORITY_NAMES[parsed.priority])
        if parsed.recur:
            extra.append(decorate({"recur": parsed.recur}, today)["recur_name"])
        tail = f" ({', '.join(extra)})" if extra else ""
        return f"✅ Добавил задачу #{task_id}: «{parsed.title}»{when}{tail}"

    def _done(self, query: str, today: date) -> str:
        task = self.tasks.find(query)
        if not task:
            return f"Не нашёл открытую задачу «{query}»."
        next_id = self.tasks.complete(task["id"], today)
        note = f" Следующая — #{next_id}." if next_id else ""
        return f"🎉 Готово: «{task['title']}».{note}"

    def _plan(self, today: date) -> str:
        settings = self.db.get_settings()
        lessons = (self.db.get_snapshot("schedule")["data"] or {}).get("lessons", [])
        result = auto_schedule(self.tasks, today, lessons, settings.get("wake_time") or "08:00",
                               settings.get("sleep_time") or "23:30", now=datetime.now())
        if not result["placed"] and not result["left"]:
            return "На сегодня нечего раскладывать — задач без времени нет."
        lines = ["🗓 Разложил задачи по свободным окнам:"]
        lines += [f"• {t['scheduled_start'][11:16]} {t['title']} ({t['duration']} мин)" for t in result["placed"]]
        if result["left"]:
            lines.append("Не влезли сегодня: " + ", ".join(t["title"] for t in result["left"]))
        return "\n".join(lines)

    def _list(self, today: date) -> str:
        views = self.tasks.views(today)
        lines = []
        for key, title in (("overdue", "Просрочено"), ("today", "Сегодня"), ("upcoming", "На неделе"), ("inbox", "Входящие")):
            if views[key]:
                lines.append(f"{title}:")
                lines += [f"  #{t['id']} {t['title']}" + (f" — {t['due_label']}" if t["due_label"] and key != "today" else "")
                          + (f" {t['due_time']}" if t["due_time"] else "") for t in views[key][:10]]
        return "\n".join(lines) or "Задач нет 🎉 Добавь: «добавь …»"

    def today_summary(self, today: date) -> str:
        settings = self.db.get_settings()
        data = sync.collect(self.db)
        lessons = [l for l in (data.get("schedule") or {}).get("lessons", []) if l["start"].startswith(today.isoformat())]
        views = self.tasks.views(today)
        lines = [f"📅 {today.strftime('%d.%m')}, {['пн','вт','ср','чт','пт','сб','вс'][today.weekday()]}"]
        weather = (data.get("weather") or {}).get("days") or []
        if weather:
            w = weather[0]
            lines.append(f"🌤 {w['text']}, {w['min']}…{w['max']}°C" + (" — возьми зонт" if w["rain"] >= 50 else ""))
        if lessons:
            lines.append("🎓 Пары:")
            lines += [f"  {l['start'][11:16]}–{l['end'][11:16]} {l['title']}" for l in lessons]
        else:
            lines.append("🎓 Пар нет")
        if views["overdue"]:
            lines.append(f"⚠️ Просрочено: " + ", ".join(t["title"] for t in views["overdue"][:5]))
        if views["today"]:
            lines.append("✅ Задачи:")
            lines += [f"  #{t['id']} {t['due_time'] + ' ' if t['due_time'] else ''}{t['title']}" for t in views["today"][:10]]
        mails = (data.get("gmail") or {}).get("messages", [])
        replies = [m for m in mails if m.get("needs_reply")]
        if replies:
            lines.append(f"✉️ Ждут ответа письма: " + ", ".join(m["from"].split("<")[0].strip() for m in replies[:5]))
        tg_wait = [c["name"] for c in (data.get("telegram") or {}).get("chats", []) if c.get("waiting")]
        if tg_wait:
            lines.append("💬 Ждут ответа в Telegram: " + ", ".join(tg_wait[:5]))
        habits = [h for h in Habits(self.db).overview(today) if not h["done_today"]]
        if habits:
            lines.append("🔁 Привычки: " + ", ".join(f"{h['icon']} {h['name']}" for h in habits))
        return "\n".join(lines)

    def context(self, today: date) -> str:
        settings = self.db.get_settings()
        data = sync.collect(self.db)
        parts = [planner.build_context({**data, "memory": Memory(self.db).prompt_block()}, settings, today)]
        views = self.tasks.views(today)
        task_lines = []
        for key, title in (("overdue", "просрочено"), ("today", "сегодня"), ("upcoming", "на неделе"),
                           ("inbox", "без срока"), ("later", "позже")):
            for t in views[key][:12]:
                when = f"{t['due_date'] or ''} {t['due_time'] or ''}".strip()
                task_lines.append(f"- #{t['id']} [{title}] {t['title']} {when} p{t['priority']}"
                                  + (f" ({t['recur_name']})" if t["recur_name"] else ""))
        parts.append("\nЗадачи:\n" + ("\n".join(task_lines) if task_lines else "нет"))
        habits = Habits(self.db).overview(today)
        if habits:
            parts.append("\nПривычки: " + ", ".join(
                f"{h['name']} ({'сделано' if h['done_today'] else 'не сделано'} сегодня, серия {h['streak']})" for h in habits))
        lessons = (data.get("schedule") or {}).get("lessons", [])
        week = [l for l in lessons if today.isoformat() < l["start"][:10] <= (today + timedelta(days=7)).isoformat()]
        if week:
            parts.append("\nПары на неделе:\n" + "\n".join(f"- {l['start'][:16].replace('T', ' ')} {l['title']}" for l in week[:25]))
        return "\n".join(parts)[:7000]
