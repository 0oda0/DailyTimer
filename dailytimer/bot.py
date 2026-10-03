"""Telegram-бот: двусторонний чат с ассистентом и напоминания.

Бот отвечает только владельцу — тому, кто первым написал ему /start (chat_id сохраняется).
"""

from __future__ import annotations

import logging
import threading
import time as time_mod
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from . import sync
from .assistant import HELP, Assistant
from .storage import Storage
from .tasks import Tasks

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"


def _call(token: str, method: str, http_timeout: float = 40, **params: Any) -> Any:
    resp = httpx.post(API.format(token=token, method=method), json=params, timeout=http_timeout)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description"))
    return data["result"]


def handle_update(storage: Storage, update: dict[str, Any], assistant: Assistant) -> tuple[str, str] | None:
    """Возвращает (chat_id, ответ) или None, если отвечать не нужно."""
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    text = (message.get("text") or "").strip()
    if chat.get("type") != "private" or not text:
        return None
    chat_id = str(chat["id"])
    settings = storage.get_settings()
    owner = settings.get("telegram_chat_id")
    if not owner:
        storage.save_settings({"telegram_chat_id": chat_id})
        owner = chat_id
    if chat_id != owner:
        return chat_id, "Это личный бот DailyTimer, он отвечает только владельцу."
    if text in {"/start", "/help"}:
        return chat_id, "Привет! Я DailyTimer 👋\n\n" + HELP + "\n\nИли просто спроси что-нибудь."
    if not settings.get("telegram_chat_bot"):
        return chat_id, "Чат с ассистентом в боте выключен в настройках."
    return chat_id, assistant.reply(text, channel="telegram")


class BotPoller:
    """Долгий опрос getUpdates в фоновом потоке."""

    def __init__(self, storage: Storage):
        self.storage = storage
        self.assistant = Assistant(storage)
        self.offset = 0
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._loop, name="telegram-bot", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            token = self.storage.get_settings().get("telegram_bot_token")
            if not token:
                self._stop.wait(30)
                continue
            try:
                updates = _call(token, "getUpdates", http_timeout=40, offset=self.offset, timeout=25)
            except Exception as exc:  # сеть, неверный токен, конфликт с другим экземпляром
                log.warning("Telegram-бот: %s", exc)
                self._stop.wait(15)
                continue
            for update in updates:
                self.offset = max(self.offset, update["update_id"] + 1)
                try:
                    result = handle_update(self.storage, update, self.assistant)
                    if result:
                        chat_id, text = result
                        _call(token, "sendChatAction", chat_id=chat_id, action="typing")
                        for start in range(0, len(text), 4000):
                            _call(token, "sendMessage", chat_id=chat_id, text=text[start:start + 4000],
                                  disable_web_page_preview=True)
                except Exception:
                    log.exception("Не удалось обработать сообщение бота")


# ------------------------------------------------------------------ напоминания

def due_reminders(storage: Storage, now: datetime) -> list[tuple[str, str]]:
    """Список (ключ, текст) напоминаний, которые пора отправить. Ключ — для защиты от повторов."""
    settings = storage.get_settings()
    today = now.date()
    out: list[tuple[str, str]] = []
    if settings.get("remind_lessons"):
        for lesson in (storage.get_snapshot("schedule")["data"] or {}).get("lessons", []):
            start = datetime.fromisoformat(lesson["start"])
            if timedelta(0) < start - now <= timedelta(minutes=15):
                where = f"\n📍 {lesson['location']}" if lesson.get("location") else ""
                out.append((f"lesson:{lesson['start']}:{lesson['title']}",
                            f"🎓 Через {int((start - now).total_seconds() // 60) + 1} мин: {lesson['title']}{where}"))
    if settings.get("remind_tasks"):
        for task in Tasks(storage).open_tasks():
            start = None
            if task["due_date"] == today.isoformat() and task["due_time"]:
                start = datetime.fromisoformat(f"{task['due_date']}T{task['due_time']}")
                lead = timedelta(minutes=10)
            elif task["scheduled_start"] and task["scheduled_start"][:10] == today.isoformat():
                start = datetime.fromisoformat(task["scheduled_start"])
                lead = timedelta(minutes=1)
            if start and task["remind"] and timedelta(minutes=-1) < start - now <= lead:
                out.append((f"task:{task['id']}:{start.isoformat()}",
                            f"⏰ {start:%H:%M} — {task['title']} ({task['duration']} мин)\nГотово? Напиши «готово {task['id']}»"))
    return out


def send_reminders(storage: Storage, now: datetime | None = None) -> int:
    now = now or datetime.now()
    sent = 0
    settings = storage.get_settings()
    for key, text in due_reminders(storage, now):
        if storage.first_time(f"remind:{key}"):
            sync.notify(storage, settings, text)
            sent += 1
    return sent


def evening_digest(storage: Storage) -> str:
    today = sync.today_for(storage.get_settings())
    tasks = Tasks(storage)
    done = tasks.done_between(today, today)
    views = tasks.views(today)
    left = views["today"] + views["overdue"]
    lines = [f"🌙 Итоги дня: выполнено {len(done)}"]
    if done:
        lines.append("✔️ " + ", ".join(t["title"] for t in done[:8]))
    if left:
        lines.append(f"Осталось {len(left)}: " + ", ".join(t["title"] for t in left[:8]))
        lines.append("Перенести на завтра можно в разделе «Итоги» или напиши мне.")
    lines.append("Как прошёл день? Запиши пару строк в «Итогах» 📔")
    return "\n".join(lines)
