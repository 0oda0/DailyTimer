"""SQLite-хранилище: настройки (секреты шифруются), снимки данных сервисов, планы."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

DATA_DIR = Path(os.environ.get("DAILYTIMER_DATA", Path.cwd() / "data"))

# Поля настроек, которые хранятся только в зашифрованном виде.
SECRET_FIELDS = {
    "gmail_app_password", "github_token", "ai_api_key", "schedule_password", "telegram_bot_token",
    "tg_api_hash", "tg_session", "tg_pending",
}

DEFAULT_SETTINGS: dict[str, Any] = {
    "timezone": "Europe/Moscow",
    "sync_interval_minutes": 15,
    "plan_time": "07:00",
    "wake_time": "08:00",
    "sleep_time": "23:30",
    # Gmail
    "gmail_email": "",
    "gmail_app_password": "",
    "gmail_apply_labels": True,
    "gmail_cleanup": True,
    "gmail_receipts_archive": True,
    "gmail_rescue_spam": True,
    # GitHub
    "github_token": "",
    # Расписание: личный кабинет (основной путь), ICS, ручная таблица
    "schedule_portal_url": "",
    "schedule_page_url": "",
    "schedule_login": "",
    "schedule_password": "",
    "schedule_refresh_hours": 6,
    "mtuci_group": "",
    "schedule_ics_url": "",
    "schedule_manual": "",
    "calendars_extra": "",
    # Подписки
    "subscriptions_manual": "",
    # ИИ: auto = локальная модель на сервере, если не ответила — бесплатное облако без ключа
    "ai_mode": "auto",
    "ai_local_url": "",
    "ai_local_model": "",
    "ai_custom_url": "",
    "ai_custom_model": "",
    "ai_api_key": "",
    # Telegram-аккаунт (чтение чатов)
    "tg_api_id": "",
    "tg_api_hash": "",
    "tg_phone": "",
    "tg_session": "",
    "tg_pending": "",
    "tg_name": "",
    # Дополнительно
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "telegram_notify_mail": True,
    "weather_city": "",
    "rss_feeds": "",
    "codeforces": False,
    # Планер
    "calendar_token": "",
    "evening_time": "21:30",
    "remind_lessons": True,
    "remind_tasks": True,
    "telegram_chat_bot": True,
    # Уведомления об изменениях
    "notify_schedule": True,
    "notify_github": True,
    "notify_money": True,
    "notify_receipts": False,
    "notify_errors": True,
    "notify_app_updates": True,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Storage:
    def __init__(self, data_dir: Path | None = None):
        self.data_dir = Path(data_dir or DATA_DIR)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._fernet = Fernet(self._load_key())
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.data_dir / "dailytimer.db", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS snapshots (
                source TEXT PRIMARY KEY, payload TEXT NOT NULL, error TEXT, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS plans (
                day TEXT PRIMARY KEY, content TEXT NOT NULL, engine TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sorted_mail (
                uid TEXT PRIMARY KEY, category TEXT NOT NULL, sorted_at TEXT NOT NULL, analysis TEXT
            );
            CREATE TABLE IF NOT EXISTS receipts (
                uid TEXT PRIMARY KEY, day TEXT, sender TEXT, subject TEXT, amount REAL, currency TEXT
            );
            CREATE TABLE IF NOT EXISTS notified (key TEXT PRIMARY KEY, at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL, notes TEXT DEFAULT '',
                due_date TEXT, due_time TEXT, duration INTEGER DEFAULT 30,
                priority INTEGER DEFAULT 4, project TEXT DEFAULT '', tags TEXT DEFAULT '',
                recur TEXT DEFAULT '', remind INTEGER DEFAULT 1,
                scheduled_start TEXT, status TEXT DEFAULT 'open',
                source TEXT DEFAULT '', source_ref TEXT DEFAULT '', parent_id INTEGER,
                created_at TEXT NOT NULL, done_at TEXT
            );
            CREATE TABLE IF NOT EXISTS habits (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, icon TEXT DEFAULT '✅',
                per_week INTEGER DEFAULT 7, archived INTEGER DEFAULT 0, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS habit_log (habit_id INTEGER, day TEXT, PRIMARY KEY (habit_id, day));
            CREATE TABLE IF NOT EXISTS focus_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER, started_at TEXT NOT NULL,
                minutes INTEGER NOT NULL, kind TEXT DEFAULT 'focus'
            );
            CREATE TABLE IF NOT EXISTS journal (
                day TEXT PRIMARY KEY, mood INTEGER, wins TEXT DEFAULT '', notes TEXT DEFAULT '',
                tomorrow TEXT DEFAULT '', updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chat (
                id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT NOT NULL, content TEXT NOT NULL,
                channel TEXT DEFAULT 'web', created_at TEXT NOT NULL
            );
            """
        )
        try:  # базы от первой версии
            self._db.execute("ALTER TABLE sorted_mail ADD COLUMN analysis TEXT")
        except sqlite3.OperationalError:
            pass

    # ---- общий доступ для модулей задач, привычек и т.п. -----------------

    def query(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for row in self._db.execute(sql, params).fetchall()]

    def execute(self, sql: str, params: tuple | list = ()) -> int:
        """Выполняет запрос в транзакции, возвращает lastrowid (или число строк)."""
        with self._lock, self._db:
            cur = self._db.execute(sql, params)
            return cur.lastrowid or cur.rowcount

    def _load_key(self) -> bytes:
        env_key = os.environ.get("DAILYTIMER_SECRET_KEY")
        if env_key:
            return env_key.encode()
        key_file = self.data_dir / "secret.key"
        if not key_file.exists():
            key_file.write_bytes(Fernet.generate_key())
            key_file.chmod(0o600)
        return key_file.read_bytes().strip()

    # ---- settings -------------------------------------------------------

    def get_settings(self) -> dict[str, Any]:
        settings = dict(DEFAULT_SETTINGS)
        with self._lock:
            rows = self._db.execute("SELECT key, value FROM settings").fetchall()
        for row in rows:
            value = row["value"]
            if row["key"] in SECRET_FIELDS and value:
                try:
                    value = self._fernet.decrypt(value.encode()).decode()
                except InvalidToken:
                    value = ""
            settings[row["key"]] = json.loads(value) if row["key"] not in SECRET_FIELDS else value
        return settings

    def save_settings(self, updates: dict[str, Any]) -> None:
        with self._lock, self._db:
            for key, value in updates.items():
                if key not in DEFAULT_SETTINGS:
                    continue
                if key in SECRET_FIELDS:
                    stored = self._fernet.encrypt(str(value).encode()).decode() if value else ""
                else:
                    stored = json.dumps(value, ensure_ascii=False)
                self._db.execute(
                    "INSERT INTO settings(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, stored),
                )

    # ---- snapshots ------------------------------------------------------

    def save_snapshot(self, source: str, payload: Any, error: str | None = None) -> None:
        with self._lock, self._db:
            if error and payload is None:
                # Сохраняем последние удачные данные, обновляем только ошибку.
                cur = self._db.execute(
                    "UPDATE snapshots SET error = ?, updated_at = ? WHERE source = ?",
                    (error, _now(), source),
                )
                if cur.rowcount:
                    return
                payload = {}
            self._db.execute(
                "INSERT INTO snapshots(source, payload, error, updated_at) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(source) DO UPDATE SET payload = excluded.payload, "
                "error = excluded.error, updated_at = excluded.updated_at",
                (source, json.dumps(payload, ensure_ascii=False, default=str), error, _now()),
            )

    def get_snapshot(self, source: str) -> dict[str, Any]:
        with self._lock:
            row = self._db.execute("SELECT * FROM snapshots WHERE source = ?", (source,)).fetchone()
        if not row:
            return {"data": None, "error": None, "updated_at": None}
        return {"data": json.loads(row["payload"]), "error": row["error"], "updated_at": row["updated_at"]}

    # ---- plans ----------------------------------------------------------

    def save_plan(self, day: str, content: str, engine: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO plans(day, content, engine, created_at) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(day) DO UPDATE SET content = excluded.content, "
                "engine = excluded.engine, created_at = excluded.created_at",
                (day, content, engine, _now()),
            )

    def get_plan(self, day: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM plans WHERE day = ?", (day,)).fetchone()
        return dict(row) if row else None

    # ---- mail sorting log ----------------------------------------------

    def mail_analysis(self, uids: list[str]) -> dict[str, dict[str, Any]]:
        if not uids:
            return {}
        marks = ",".join("?" * len(uids))
        with self._lock:
            rows = self._db.execute(
                f"SELECT uid, category, analysis FROM sorted_mail WHERE uid IN ({marks})", uids
            ).fetchall()
        return {
            row["uid"]: json.loads(row["analysis"]) if row["analysis"] else {"category": row["category"]}
            for row in rows
        }

    def save_mail_analysis(self, uid: str, analysis: dict[str, Any]) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO sorted_mail(uid, category, sorted_at, analysis) VALUES(?, ?, ?, ?)",
                (uid, analysis["category"], _now(), json.dumps(analysis, ensure_ascii=False)),
            )

    # ---- receipts ledger -----------------------------------------------

    def add_receipt(self, uid: str, day: str | None, sender: str, subject: str,
                    amount: float | None, currency: str | None) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO receipts(uid, day, sender, subject, amount, currency) VALUES(?, ?, ?, ?, ?, ?)",
                (uid, day, sender, subject, amount, currency),
            )

    def receipts(self, since: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM receipts WHERE day >= ? ORDER BY day DESC", (since,)
            ).fetchall()
        return [dict(row) for row in rows]

    # ---- notifications --------------------------------------------------

    def first_time(self, key: str) -> bool:
        """True, если по этому ключу ещё не уведомляли (и помечает его)."""
        with self._lock, self._db:
            cur = self._db.execute("INSERT OR IGNORE INTO notified(key, at) VALUES(?, ?)", (key, _now()))
            return cur.rowcount == 1
