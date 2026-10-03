"""Генеральная уборка Gmail: удаляет промоакции, рекламу и спам за всё время, а не только свежие письма.

Безопасность:
- письма уходят в «Корзину» Gmail и ещё 30 дней их можно восстановить
  (режим «удалять навсегда» — по отдельной галочке);
- никогда не трогаем письма со звёздочкой, чеки/заказы и то, что мы пометили важным;
- спам сначала проверяется: важное (учёба, чеки, коды, письма от людей) возвращается во «Входящие».
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any, Callable

from . import sorter
from .ai import AIClient
from .connectors import gmail
from .storage import Storage

log = logging.getLogger(__name__)

# Что никогда не удаляем, даже если Gmail считает это промоакцией.
PROTECT = ('-is:starred -label:dt-receipts -label:dt-important -label:dt-rescued '
           '-{чек "кассовый чек" receipt invoice счёт квитанция "ваш заказ" "order confirmation" "подтверждение заказа" '
           'билет ticket бронирование booking "код подтверждения" "verification code"}')

KINDS: dict[str, dict[str, str]] = {
    "promotions": {"title": "Промоакции", "hint": "вкладка «Промоакции» в Gmail",
                   "query": f"category:promotions {PROTECT}"},
    "promo": {"title": "Реклама", "hint": "то, что DailyTimer распознал как рекламу",
              "query": f"label:dt-promo {PROTECT}"},
    "newsletters": {"title": "Рассылки", "hint": "дайджесты и новостные рассылки",
                    "query": f"label:dt-newsletters {PROTECT}"},
    "social": {"title": "Соцсети", "hint": "уведомления VK, Instagram и т.п. (вкладка «Соцсети»)",
               "query": f"category:social {PROTECT}"},
    "spam": {"title": "Спам", "hint": "папка «Спам» — сначала проверяется на важное", "query": ""},
}
DEFAULT_KINDS = ["promotions", "promo", "spam"]
_lock = threading.Lock()


def running() -> bool:
    return _lock.locked()


def _save(storage: Storage, **state: Any) -> None:
    current = storage.get_snapshot("mail_cleanup")["data"] or {}
    storage.save_snapshot("mail_cleanup", {**current, **state})


def scan(storage: Storage, settings: dict[str, Any]) -> dict[str, Any]:
    """Считает, сколько писем каждого вида есть во всём ящике, и показывает примеры."""
    result: dict[str, Any] = {}
    with gmail.GmailClient(settings["gmail_email"], settings["gmail_app_password"]) as client:
        for kind, meta in KINDS.items():
            if kind == "spam":
                if "\\Junk" not in client.folders:
                    continue
                client.select("spam")
                uids = client.search_all()
                samples = client.headers(uids[-5:][::-1])
                client.select("all")
            else:
                uids = client.search_all(meta["query"])
                samples = client.headers(uids[-5:][::-1])
            result[kind] = {"count": len(uids), "samples": samples}
    _save(storage, status="scanned", scan=result, scanned_at=datetime.now().isoformat(timespec="seconds"))
    return result


def _clean_spam(client: gmail.GmailClient, storage: Storage, ai: AIClient | None, limit_check: int = 300) -> dict[str, int]:
    """Проверяет спам на важное (последние limit_check писем — Gmail и так хранит спам 30 дней),
    возвращает важное во «Входящие», остальное — в корзину."""
    if "\\Junk" not in client.folders:
        return {"deleted": 0, "rescued": 0}
    client.select("spam")
    try:
        uids = client.search_all()
        known = storage.mail_analysis([f"spam:{u}" for u in uids])
        to_check = [u for u in uids if f"spam:{u}" not in known][-limit_check:]
        rescued: set[str] = {u for u in uids if (known.get(f"spam:{u}") or {}).get("rescued")}
        mails = list(client.fetch(to_check))
        verdicts = sorter.analyze(mails, ai)
        for mail in mails:
            verdict = verdicts[mail["uid"]]
            rescue = sorter.should_rescue_from_spam(mail, verdict)
            storage.save_mail_analysis(f"spam:{mail['uid']}", {**verdict, "rescued": rescue})
            if rescue:
                rescued.add(mail["uid"])
        for uid in rescued & set(uids):
            client.ensure_label("DT/Rescued")
            client.add_label(uid, "DT/Rescued")
            client.move_to_inbox(uid)
        doomed = [u for u in uids if u not in rescued]
        deleted = client.trash(doomed) if doomed else 0
        return {"deleted": deleted, "rescued": len(rescued & set(uids))}
    finally:
        client.select("all")


def purge(storage: Storage, settings: dict[str, Any], kinds: list[str], ai: AIClient | None,
          notify: Callable[[str], Any] | None = None) -> dict[str, Any]:
    """Удаляет выбранные виды писем во всём ящике. Возвращает {вид: удалено}."""
    if not _lock.acquire(blocking=False):
        return {"error": "Уборка уже идёт"}
    started = datetime.now().isoformat(timespec="seconds")
    result: dict[str, Any] = {}
    try:
        _save(storage, status="running", started_at=started, progress="подключаюсь к Gmail…", result={})
        with gmail.GmailClient(settings["gmail_email"], settings["gmail_app_password"]) as client:
            for kind in kinds:
                if kind not in KINDS:
                    continue
                _save(storage, progress=f"{KINDS[kind]['title']}…", result=result)
                if kind == "spam":
                    spam = _clean_spam(client, storage, ai)
                    result["spam"] = spam["deleted"]
                    result["rescued"] = spam["rescued"]
                else:
                    uids = client.search_all(KINDS[kind]["query"])
                    result[kind] = client.trash(uids) if uids else 0
            if settings.get("gmail_purge_permanent"):
                _save(storage, progress="окончательно удаляю из корзины…", result=result)
                client.purge_trashed()
        total = sum(v for k, v in result.items() if k != "rescued")
        _save(storage, status="done", result=result, total=total, progress="",
              finished_at=datetime.now().isoformat(timespec="seconds"))
        if notify and total:
            parts = [f"{KINDS[k]['title'].lower()}: {v}" for k, v in result.items() if k in KINDS and v]
            text = f"🧹 Уборка почты: удалено {total} писем ({', '.join(parts)})."
            if result.get("rescued"):
                text += f"\n🛟 Из спама возвращено во «Входящие»: {result['rescued']}."
            if not settings.get("gmail_purge_permanent"):
                text += "\nВсё лежит в «Корзине» Gmail ещё 30 дней, если что-то нужно вернуть."
            notify(text)
        return result
    except Exception as exc:
        log.exception("Уборка почты упала")
        _save(storage, status="error", error=str(exc), result=result, progress="")
        return {"error": str(exc), **result}
    finally:
        _lock.release()


def selected_kinds(settings: dict[str, Any]) -> list[str]:
    raw = settings.get("gmail_purge_kinds") or ",".join(DEFAULT_KINDS)
    return [k for k in raw.split(",") if k in KINDS]
