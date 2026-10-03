"""Уведомления в Telegram: утренний план и «срочное в почте».

Настройка: создай бота у @BotFather, вставь токен и напиши боту /start —
chat_id подхватится автоматически.
"""

from __future__ import annotations

from typing import Any

import httpx

API = "https://api.telegram.org/bot{token}/{method}"


class TelegramError(RuntimeError):
    pass


def _call(token: str, method: str, **params: Any) -> Any:
    try:
        resp = httpx.post(API.format(token=token, method=method), json=params, timeout=30)
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise TelegramError(f"Telegram недоступен: {exc}") from exc
    if not data.get("ok"):
        raise TelegramError(f"Telegram: {data.get('description')}")
    return data["result"]


def discover_chat_id(token: str) -> str | None:
    """Ищет чат, где пользователь написал боту /start."""
    for update in reversed(_call(token, "getUpdates", limit=50)):
        message = update.get("message") or {}
        if message.get("chat", {}).get("type") == "private":
            return str(message["chat"]["id"])
    return None


def send(token: str, chat_id: str, text: str) -> None:
    for start in range(0, len(text), 4000):  # лимит Telegram — 4096 символов
        _call(token, "sendMessage", chat_id=chat_id, text=text[start : start + 4000], disable_web_page_preview=True)
