"""Личный Telegram-аккаунт: непрочитанные чаты, кто ждёт ответа, упоминания в группах.

Вход как в обычном клиенте: номер → код из Telegram → (облачный пароль, если включён).
Нужны api_id и api_hash приложения — их Telegram выдаёт бесплатно за минуту на my.telegram.org.
Сессия хранится в базе зашифрованной; читаем только, ничего не отправляем.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any


class TgError(RuntimeError):
    pass


def _client(api_id: str, api_hash: str, session: str = "") -> Any:
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError as exc:
        raise TgError("Не установлен telethon (в Docker-образе он есть)") from exc
    if not str(api_id).strip().isdigit() or not api_hash:
        raise TgError("Укажи api_id и api_hash с my.telegram.org")
    return TelegramClient(StringSession(session or None), int(api_id), api_hash.strip(),
                          device_model="DailyTimer", app_version="1.0")


def send_code(api_id: str, api_hash: str, phone: str) -> tuple[str, str]:
    """Отправляет код входа. Возвращает (временная сессия, phone_code_hash)."""

    async def run() -> tuple[str, str]:
        client = _client(api_id, api_hash)
        await client.connect()
        try:
            sent = await client.send_code_request(phone)
            return client.session.save(), sent.phone_code_hash
        except Exception as exc:
            raise TgError(f"Telegram не отправил код: {exc}") from exc
        finally:
            await client.disconnect()

    return asyncio.run(run())


def sign_in(api_id: str, api_hash: str, pending_session: str, phone: str, code: str,
            phone_code_hash: str, password: str = "") -> tuple[str, str]:
    """Завершает вход. Возвращает (постоянная сессия, имя аккаунта)."""
    from telethon.errors import SessionPasswordNeededError

    async def run() -> tuple[str, str]:
        client = _client(api_id, api_hash, pending_session)
        await client.connect()
        try:
            try:
                await client.sign_in(phone=phone, code=code.strip(), phone_code_hash=phone_code_hash)
            except SessionPasswordNeededError:
                if not password:
                    raise TgError("У аккаунта включён облачный пароль — введи его вместе с кодом")
                await client.sign_in(password=password)
            me = await client.get_me()
            name = " ".join(x for x in (me.first_name, me.last_name) if x) or me.username or phone
            return client.session.save(), name
        except TgError:
            raise
        except Exception as exc:
            raise TgError(f"Вход в Telegram не удался: {exc}") from exc
        finally:
            await client.disconnect()

    return asyncio.run(run())


def _is_muted(dialog: Any) -> bool:
    mute_until = getattr(getattr(dialog.dialog, "notify_settings", None), "mute_until", None)
    return bool(mute_until and mute_until > datetime.now(timezone.utc))


def summarize_dialog(dialog: Any) -> dict[str, Any] | None:
    unread = dialog.unread_count or 0
    mentions = getattr(dialog, "unread_mentions_count", 0) or 0
    if not unread and not mentions:
        return None
    entity = dialog.entity
    if dialog.is_user:
        kind = "bot" if getattr(entity, "bot", False) else "user"
    else:
        kind = "group" if dialog.is_group else "channel"
    muted = _is_muted(dialog)
    if kind == "channel" or (muted and not mentions):
        return None  # каналы и заглушённые чаты — шум
    message = dialog.message
    text = (getattr(message, "message", "") or "[медиа]") if message else ""
    return {
        "name": dialog.name or "Без имени",
        "kind": kind,
        "unread": unread,
        "mentions": mentions,
        "text": text.replace("\n", " ")[:200],
        "date": message.date.isoformat() if message and message.date else None,
        "waiting": kind == "user" and message is not None and not message.out,
    }


def fetch(api_id: str, api_hash: str, session: str, limit: int = 80) -> dict[str, Any]:
    if not session:
        raise TgError("Telegram-аккаунт не подключён")

    async def run() -> dict[str, Any]:
        client = _client(api_id, api_hash, session)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise TgError("Сессия Telegram истекла — войди заново")
            chats = [c for c in (summarize_dialog(d) for d in await client.get_dialogs(limit=limit)) if c]
        finally:
            await client.disconnect()
        chats.sort(key=lambda c: (not c["waiting"], not c["mentions"], c["kind"] == "bot", -(c["unread"])))
        return {
            "chats": chats,
            "waiting": sum(1 for c in chats if c["waiting"]),
            "mentions": sum(c["mentions"] for c in chats),
        }

    return asyncio.run(run())
