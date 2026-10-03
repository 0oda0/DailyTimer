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
        if not query:
            return [m["uid"] for m in box if m["uid"] not in self.trashed]
        tag = "promotions" if "category:promotions" in query else "dt-promo" if "dt-promo" in query else "?"
        return [m["uid"] for m in box if tag in m["tags"] and "starred" not in m["tags"] and m["uid"] not in self.trashed]

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
    assert FakeGmail.purged == [True] and len(FakeGmail.trashed) == 7
    FakeGmail.trashed.clear()
    sync.maybe_first_purge(store)  # второй раз автоматически не запускается
    assert FakeGmail.trashed == []


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
