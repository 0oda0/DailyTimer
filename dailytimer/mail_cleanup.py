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


# ------------------------------------------------------------------ полный разбор всего ящика

RECEIPTS_QUERY = ('in:inbox -is:starred (category:purchases OR {чек "кассовый чек" receipt invoice квитанция '
                  '"ваш заказ" "order confirmation" "подтверждение заказа" "оплата прошла" "payment received"})')
LOW_VALUE_QUERY = "in:inbox -is:starred older_than:3d (category:updates OR category:social OR category:forums)"
PRIMARY_QUERY = "in:inbox -is:starred older_than:3d"
TRIAGE_LIMIT = 3000  # писем «Входящих» за один проход; остальное — в следующий раз


def triage(storage: Storage, settings: dict[str, Any], ai: AIClient | None,
           notify: Callable[[str], Any] | None = None) -> dict[str, Any]:
    """Полный разбор: удалить мусор, убрать чеки и оповещения, разобрать всё, что осталось во «Входящих»."""
    if not _lock.acquire(blocking=False):
        return {"error": "Разбор почты уже идёт"}
    result: dict[str, Any] = {}
    kept: list[dict[str, Any]] = []
    try:
        _save(storage, status="running", started_at=datetime.now().isoformat(timespec="seconds"),
              progress="подключаюсь к Gmail…", result={}, error="")
        with gmail.GmailClient(settings["gmail_email"], settings["gmail_app_password"]) as client:
            # 1. Мусор за всё время — в корзину.
            for kind in selected_kinds(settings):
                _save(storage, progress=f"удаляю: {KINDS[kind]['title'].lower()}…", result=result)
                if kind == "spam":
                    spam = _clean_spam(client, storage, ai)
                    result["spam"], result["rescued"] = spam["deleted"], spam["rescued"]
                else:
                    uids = client.search_all(KINDS[kind]["query"])
                    result[kind] = client.trash(uids) if uids else 0

            # 2. Покупки и чеки — в ярлык DT/Receipts, из «Входящих» убрать.
            _save(storage, progress="собираю чеки и покупки…", result=result)
            uids = client.search_all(RECEIPTS_QUERY)
            result["receipts"] = client.bulk(uids, label="DT/Receipts", archive=True, read=True) if uids else 0

            # 3. Оповещения, соцсети, форумы старше 3 дней — в архив и прочитанными.
            _save(storage, progress="убираю оповещения и уведомления…", result=result)
            uids = client.search_all(LOW_VALUE_QUERY)
            result["archived"] = client.bulk(uids, label="DT/Other", archive=True, read=True) if uids else 0

            # 4. Всё остальное во «Входящих» — разбираем по отправителю и теме.
            uids = client.search_all(PRIMARY_QUERY)[-TRIAGE_LIMIT:]
            result["left_in_inbox_before"] = len(uids)
            _save(storage, progress=f"читаю «Входящие»: {len(uids)} писем…", result=result)
            mails = client.headers_batch(uids)
            verdicts: dict[str, dict[str, Any]] = {}
            for start in range(0, len(mails), 40):
                chunk = mails[start : start + 40]
                # Только правила: ИИ на тысячах писем занимал бы процессор и память сервера часами
                # (на 3 ГБ из-за этого переставало грузиться расписание). Свежую почту ИИ разбирает отдельно.
                verdicts.update(sorter.analyze(chunk, None))
                _save(storage, progress=f"разбираю «Входящие»: {min(start + 40, len(mails))} из {len(mails)}…",
                      result=result)
            buckets: dict[str, list[str]] = {"trash": [], "receipts": [], "archive": []}
            for mail in mails:
                verdict = verdicts.get(mail["uid"]) or sorter.rule_analysis(mail)
                storage.save_mail_analysis(mail["uid"], {**verdict, "actions": ["разбор ящика"]})
                category = verdict["category"]
                if verdict["important"] or verdict["needs_reply"] or category in {"personal", "study", "security", "important", "dev"}:
                    kept.append({**mail, **verdict})
                elif category == "promo":
                    buckets["trash"].append(mail["uid"])
                elif category == "receipts":
                    buckets["receipts"].append(mail["uid"])
                else:
                    buckets["archive"].append(mail["uid"])
            if buckets["trash"]:
                result["promo"] = result.get("promo", 0) + client.trash(buckets["trash"])
            if buckets["receipts"]:
                result["receipts"] += client.bulk(buckets["receipts"], label="DT/Receipts", archive=True, read=True)
            if buckets["archive"]:
                result["archived"] += client.bulk(buckets["archive"], archive=True, read=True)
            result["kept"] = len(kept)

            if settings.get("gmail_purge_permanent"):
                client.purge_trashed()
        deleted = sum(result.get(k, 0) for k in KINDS)
        _save(storage, status="done", result=result, total=deleted, progress="",
              finished_at=datetime.now().isoformat(timespec="seconds"),
              kept=[{"from": m["from"], "subject": m["subject"], "needs_reply": m.get("needs_reply")}
                    for m in kept[:30]])
        if notify:
            lines = [f"📥 Разобрал всю почту:",
                     f"🗑 удалено (реклама, промо, спам): {deleted}",
                     f"🧾 чеки и покупки → DT/Receipts: {result.get('receipts', 0)}",
                     f"📦 оповещения и рассылки → в архив: {result.get('archived', 0)}",
                     f"📌 оставлено во «Входящих» как личное и важное: {len(kept)}"]
            if result.get("rescued"):
                lines.append(f"🛟 из спама возвращено: {result['rescued']}")
            notify("\n".join(lines))
        return result
    except Exception as exc:
        log.exception("Разбор почты упал")
        _save(storage, status="error", error=f"{type(exc).__name__}: {exc}", result=result, progress="")
        return {"error": str(exc), **result}
    finally:
        _lock.release()
