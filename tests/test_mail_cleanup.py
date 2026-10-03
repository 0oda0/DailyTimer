from dailytimer import mail_cleanup, sync
from dailytimer.connectors import gmail
from dailytimer.storage import Storage


class FakeGmail:
    """Имитация ящика: «Вся почта» и «Спам», поиск по нашим запросам."""

    def __init__(self, *a, **k):
        self.folders = {"\\All": '"[Gmail]/All Mail"', "\\Junk": '"[Gmail]/Spam"', "\\Trash": '"[Gmail]/Trash"'}
        self.current = "all"
        self.boxes = FakeGmail.boxes
        self.trashed: list[str] = FakeGmail.trashed
        self.inbox: list[str] = FakeGmail.inbox
        self.purged = FakeGmail.purged

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def select(self, which):
        self.current = which

    def search_all(self, query=""):
        box = self.boxes[self.current]
        alive = [m for m in box if m["uid"] not in self.trashed]
        if not query:
            return [m["uid"] for m in alive]
        if "starred" in query:
            alive = [m for m in alive if "starred" not in m["tags"]]
        if "in:inbox" in query:
            alive = [m for m in alive if m["uid"] not in FakeGmail.archived]
        if "category:purchases" in query:
            return [m["uid"] for m in alive if "purchases" in m["tags"]]
        if "category:updates" in query:
            return [m["uid"] for m in alive if m["tags"] & {"updates", "social"}]
        if "category:promotions" in query:
            return [m["uid"] for m in alive if "promotions" in m["tags"]]
        if "dt-promo" in query:
            return [m["uid"] for m in alive if "dt-promo" in m["tags"]]
        if "in:inbox" in query:
            return [m["uid"] for m in alive]
        return []

    def headers_batch(self, uids):
        return [{"uid": m["uid"], "from": m.get("from", "x@y.ru"), "subject": m.get("subject", ""), "snippet": "",
                 "list_unsubscribe": "unsub" in m["tags"], "in_inbox": True}
                for m in self.boxes[self.current] if m["uid"] in uids]

    def bulk(self, uids, label=None, archive=False, read=False):
        if archive:
            FakeGmail.archived.update(uids)
        if label:
            FakeGmail.labels.setdefault(label, set()).update(uids)
        return len(uids)

    def headers(self, uids):
        return [{"uid": u, "from": "x", "subject": f"тема {u}"} for u in uids]

    def fetch(self, uids):
        for m in self.boxes[self.current]:
            if m["uid"] in uids:
                yield {"uid": m["uid"], "from": m.get("from", "spam@x.ru"), "subject": m.get("subject", "WIN"),
                       "snippet": m.get("snippet", ""), "list_unsubscribe": False}

    def trash(self, uids):
        self.trashed.extend(uids)
        return len(uids)

    def purge_trashed(self):
        self.purged.append(True)

    def ensure_label(self, label):
        pass

    def add_label(self, uid, label):
        pass

    def move_to_inbox(self, uid):
        self.inbox.append(uid)
        return True


def setup(monkeypatch, tmp_path):
    FakeGmail.boxes = {
        "all": [{"uid": str(i), "tags": {"promotions"}} for i in range(1, 6)]
               + [{"uid": "6", "tags": {"promotions", "starred"}}, {"uid": "7", "tags": {"dt-promo"}},
                  {"uid": "8", "tags": {"personal"}}],
        "spam": [{"uid": "s1", "tags": set(), "subject": "Казино"},
                 {"uid": "s2", "tags": set(), "from": "Деканат <dekanat@mtuci.ru>", "subject": "Экзамен перенесён",
                  "snippet": "Экзамен по матанализу переносится"}],
    }
    FakeGmail.trashed, FakeGmail.inbox, FakeGmail.purged = [], [], []
    FakeGmail.archived, FakeGmail.labels = set(), {}
    monkeypatch.setattr(gmail, "GmailClient", FakeGmail)
    store = Storage(tmp_path)
    store.save_settings({"gmail_email": "me@gmail.com", "gmail_app_password": "x", "ai_mode": "off"})
    return store


def test_protect_query():
    q = mail_cleanup.KINDS["promotions"]["query"]
    assert "category:promotions" in q and "-is:starred" in q and "-label:dt-receipts" in q and "чек" in q


def test_scan_and_purge(monkeypatch, tmp_path):
    store = setup(monkeypatch, tmp_path)
    found = mail_cleanup.scan(store, store.get_settings())
    assert found["promotions"]["count"] == 5 and found["promo"]["count"] == 1 and found["spam"]["count"] == 2
    sent = []
    result = mail_cleanup.purge(store, store.get_settings(), ["promotions", "promo", "spam"], None, sent.append)
    assert result == {"promotions": 5, "promo": 1, "spam": 1, "rescued": 1}
    assert "6" not in FakeGmail.trashed and "8" not in FakeGmail.trashed  # звёздочка и личное — не трогаем
    assert FakeGmail.inbox == ["s2"] and "s1" in FakeGmail.trashed       # деканат спасён, казино удалено
    assert "удалено 7 писем" in sent[0] and "возвращено во «Входящие»: 1" in sent[0] and "Корзине" in sent[0]
    assert FakeGmail.purged == []
    snap = store.get_snapshot("mail_cleanup")["data"]
    assert snap["status"] == "done" and snap["total"] == 7


def test_permanent_and_first_purge(monkeypatch, tmp_path):
    store = setup(monkeypatch, tmp_path)
    store.save_settings({"gmail_purge_permanent": True})
    monkeypatch.setattr(sync, "notify", lambda *a: True)
    sync.maybe_first_purge(store)
    assert FakeGmail.purged == [True] and {"1", "2", "3", "4", "5", "7", "s1"} <= set(FakeGmail.trashed)
    FakeGmail.trashed.clear()
    sync.maybe_first_purge(store)  # второй раз автоматически не запускается
    assert FakeGmail.trashed == []


def test_full_triage_like_real_inbox(monkeypatch, tmp_path):
    store = setup(monkeypatch, tmp_path)
    FakeGmail.boxes["all"] += [
        {"uid": "p1", "tags": {"purchases"}, "subject": "Заказ доставлен"},
        {"uid": "u1", "tags": {"updates"}, "subject": "Новое в приложении"},
        {"uid": "u2", "tags": {"social"}, "subject": "Вас отметили"},
        {"uid": "n1", "tags": {"unsub"}, "from": "news@habr.com", "subject": "Дайджест недели"},
        {"uid": "a1", "tags": {"unsub"}, "from": "shop@x.ru", "subject": "Скидки 70% только сегодня"},
        {"uid": "f1", "tags": set(), "from": "Аня <anya@gmail.com>", "subject": "Сможешь завтра созвониться?"},
        {"uid": "k1", "tags": set(), "from": "Деканат <dekanat@mtuci.ru>", "subject": "Экзамен перенесён"},
    ]
    sent = []
    result = mail_cleanup.triage(store, store.get_settings(), None, sent.append)
    assert "error" not in result, result
    assert {"1", "2", "3", "4", "5", "7", "a1", "s1"} <= set(FakeGmail.trashed)   # промо, реклама, спам
    assert "6" not in FakeGmail.trashed                                           # со звёздочкой
    assert {"p1"} <= FakeGmail.labels["DT/Receipts"] and "p1" in FakeGmail.archived
    assert {"u1", "u2", "n1"} <= FakeGmail.archived                                # оповещения, рассылки
    assert not {"f1", "k1", "8"} & (FakeGmail.archived | set(FakeGmail.trashed))   # личное и учёба остаются
    snap = store.get_snapshot("mail_cleanup")["data"]
    assert snap["status"] == "done" and snap["result"]["kept"] >= 2
    assert any("Сможешь" in m["subject"] and m["needs_reply"] for m in snap["kept"])
    assert sent and "Разобрал всю почту" in sent[0]


def test_daily_promo_goes_to_trash(tmp_path):
    store = Storage(tmp_path)
    calls = []

    class Client:
        def add_label(self, *a): pass
        def trash(self, uids): calls.append(("trash", uids))
        def archive(self, uid): calls.append(("archive", uid))
        def mark_read(self, uid): pass

    settings = {"gmail_apply_labels": True, "gmail_trash_promo": True, "gmail_cleanup": True}
    promo = {"category": "promo", "important": False, "needs_reply": False}
    news = {"category": "newsletters", "important": False, "needs_reply": False}
    mail = {"uid": "1", "subject": "Скидки", "snippet": "", "from": "shop", "in_inbox": True}
    assert sync._apply_actions(Client(), mail, promo, settings, store)["actions"] == ["удалено"]
    assert sync._apply_actions(Client(), {**mail, "uid": "2"}, news, settings, store)["actions"] == ["убрано"]
    assert calls == [("trash", ["1"]), ("archive", "2")]
