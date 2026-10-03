"""Разделы страницы «Подключения»: какие поля в каком разделе и как показать статус."""

from __future__ import annotations

from typing import Any

SECTIONS: dict[str, dict[str, Any]] = {
    "study": {
        "icon": "🎓", "title": "Учёба",
        "about": "Расписание из личного кабинета вуза, календари",
        "fields": ["schedule_portal_url", "schedule_page_url", "schedule_login", "schedule_password",
                   "schedule_refresh_hours", "mtuci_group", "schedule_ics_url", "schedule_manual"],
    },
    "mail": {
        "icon": "📧", "title": "Почта",
        "about": "Gmail: сортировка, уборка, чеки, проверка спама",
        "fields": ["gmail_email", "gmail_app_password", "gmail_apply_labels", "gmail_cleanup",
                   "gmail_receipts_archive", "gmail_rescue_spam"],
    },
    "social": {
        "icon": "💬", "title": "Соцсети и мессенджеры",
        "about": "Telegram: чаты, которые ждут ответа; уведомления от бота",
        "fields": ["telegram_bot_token", "telegram_notify_mail", "telegram_chat_bot", "remind_lessons", "remind_tasks",
                   "notify_schedule", "notify_github", "notify_money", "notify_receipts", "notify_errors",
                   "notify_app_updates"],
    },
    "dev": {
        "icon": "🐙", "title": "Разработка",
        "about": "GitHub, контесты Codeforces",
        "fields": ["github_token", "codeforces"],
    },
    "money": {
        "icon": "💳", "title": "Финансы и подписки",
        "about": "Подписки и чеки — находятся сами по почте",
        "fields": ["subscriptions_manual"],
    },
    "ai": {
        "icon": "🤖", "title": "ИИ",
        "about": "Локальная модель на сервере, без ключей",
        "fields": ["ai_mode", "ai_local_model", "ai_custom_url", "ai_custom_model", "ai_api_key", "ai_local_url"],
    },
    "other": {
        "icon": "⚙️", "title": "Режим дня и прочее",
        "about": "Подъём, отбой, время плана, погода, другие календари, новости",
        "fields": ["timezone", "wake_time", "sleep_time", "plan_time", "evening_time", "sync_interval_minutes",
                   "weather_city", "calendars_extra", "rss_feeds"],
    },
}


def section_status(key: str, s: dict[str, Any], snaps: dict[str, dict[str, Any]]) -> tuple[str, str]:
    """(уровень ok|warn|off, текст) для карточки раздела."""

    def snap_state(source: str, ok_text: str) -> tuple[str, str]:
        snap = snaps.get(source) or {}
        if snap.get("error"):
            return "warn", f"ошибка: {snap['error'][:80]}"
        return "ok", ok_text

    if key == "study":
        if s.get("schedule_login") and (s.get("schedule_portal_url") or s.get("schedule_page_url")):
            data = (snaps.get("portal") or {}).get("data") or {}
            group = f", группа {data['group']}" if data.get("group") else ""
            return snap_state("portal", f"кабинет подключён{group}")
        if s.get("schedule_ics_url") or s.get("schedule_manual"):
            return "ok", "расписание подключено"
        return "off", "не подключено"
    if key == "mail":
        return snap_state("gmail", s["gmail_email"]) if s.get("gmail_email") else ("off", "не подключено")
    if key == "social":
        parts = []
        if s.get("tg_session"):
            parts.append(f"Telegram: {s.get('tg_name') or 'подключён'}")
        if s.get("telegram_bot_token"):
            parts.append("бот уведомлений")
        if not parts:
            return "off", "не подключено"
        return snap_state("telegram", ", ".join(parts)) if s.get("tg_session") else ("ok", ", ".join(parts))
    if key == "dev":
        return snap_state("github", "GitHub подключён") if s.get("github_token") else ("off", "не подключено")
    if key == "money":
        items = ((snaps.get("subscriptions") or {}).get("data") or {}).get("items", [])
        return ("ok", f"подписок найдено: {len(items)}") if items else ("off", "появятся после подключения почты")
    if key == "ai":
        mode = {"auto": "локальная + облако", "local": "локальная", "cloud": "облако", "off": "выключен"}
        return ("off" if s.get("ai_mode") == "off" else "ok"), mode.get(s.get("ai_mode") or "auto", "")
    extras = [name for name, field in (("погода", "weather_city"), ("календари", "calendars_extra"),
                                       ("новости", "rss_feeds")) if s.get(field)]
    return "ok", (", ".join(extras) if extras else "режим дня")
