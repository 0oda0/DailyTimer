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
SECRET_FIELDS = {"gmail_app_password", "github_token", "ai_api_key", "schedule_password"}

DEFAULT_SETTINGS: dict[str, Any] = {
    "timezone": "Europe/Moscow",
    "sync_interval_minutes": 15,
    "plan_time": "07:00",
    "gmail_email": "",
    "gmail_app_password": "",
    "gmail_apply_labels": True,
    "gmail_archive_promo": False,
    "github_token": "",
    "schedule_ics_url": "",
    "schedule_manual": "",
    "schedule_login": "",
    "schedule_password": "",
    "subscriptions_manual": "",
    "ai_provider": "gemini",
    "ai_api_key": "",
    "ai_model": "",
    "wake_time": "08:00",
    "sleep_time": "23:30",
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
                uid TEXT PRIMARY KEY, category TEXT NOT NULL, sorted_at TEXT NOT NULL
            );
            """
        )

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

    def sorted_uids(self, uids: list[str]) -> dict[str, str]:
        if not uids:
            return {}
        marks = ",".join("?" * len(uids))
        with self._lock:
            rows = self._db.execute(
                f"SELECT uid, category FROM sorted_mail WHERE uid IN ({marks})", uids
            ).fetchall()
        return {row["uid"]: row["category"] for row in rows}

    def mark_sorted(self, uid: str, category: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO sorted_mail(uid, category, sorted_at) VALUES(?, ?, ?)",
                (uid, category, _now()),
            )
