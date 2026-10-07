"""Раздел «Проекты»: сводки по проектам из других чатов с Claude.

Сводка — JSON по промпту REPORT_PROMPT. Её вставляют на странице /projects, проекты сливаются по id
и хранятся в обычном файле data/projects.json. Сайты проектов проверяются на доступность по расписанию.
"""

from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .storage import Storage

STATUSES: dict[str, tuple[str, str]] = {  # код → (подпись, значок)
    "in_progress": ("в работе", "🛠"),
    "testing": ("тестирование", "🧪"),
    "deployed": ("развёрнут", "🚀"),
    "planning": ("планирование", "📝"),
    "idea": ("идея", "💡"),
    "paused": ("на паузе", "⏸"),
    "done": ("готов", "✅"),
    "abandoned": ("заброшен", "🗄"),
}
STATUS_ALIASES = {
    "в работе": "in_progress", "разработка": "in_progress", "active": "in_progress", "wip": "in_progress",
    "тестирование": "testing", "развёрнут": "deployed", "развернут": "deployed", "production": "deployed",
    "prod": "deployed", "live": "deployed", "планирование": "planning", "идея": "idea", "пауза": "paused",
    "на паузе": "paused", "готов": "done", "завершён": "done", "completed": "done", "archived": "abandoned",
}
STALE_DAYS = 14
MAX_CHANGES = 30
MAX_HISTORY = 50

REPORT_PROMPT = """Сделай сводку по всем проектам, над которыми мы работали в этом чате. Она нужна для моего дашборда DailyTimer, поэтому ответь ТОЛЬКО одним блоком JSON строго в формате ниже, без текста до и после.

Правила:
- Опирайся только на то, что реально есть в этом чате. Не выдумывай. Если чего-то не знаешь, ставь null.
- НИКОГДА не включай пароли, токены, API-ключи, логины и другие секреты. Если они нужны для работы, напиши, где они хранятся (например «в .env на сервере»), но не сами значения.
- Даты пиши в формате ГГГГ-ММ-ДД.
- Если проектов несколько, сделай отдельный объект для каждого.

Формат:
{
  "source_chat": "краткое название этого чата / о чём он",
  "generated_at": "ГГГГ-ММ-ДД",
  "projects": [
    {
      "id": "короткий-slug-латиницей",
      "name": "Название проекта",
      "summary": "О чём проект и зачем он нужен, 1–3 предложения",
      "status": "idea | planning | in_progress | testing | deployed | paused | done | abandoned",
      "progress_percent": 0,
      "stage": "На каком этапе сейчас, одной фразой",
      "current_focus": "Над чем работали в последний раз",
      "recent_changes": [
        {"date": "ГГГГ-ММ-ДД", "change": "что сделано"}
      ],
      "next_steps": ["следующий шаг 1", "следующий шаг 2"],
      "blockers": ["что мешает / ждёт решения / известные баги"],
      "location": {
        "repo": "owner/repo или URL репозитория",
        "branch": "ветка",
        "local_path": "путь к папке, если известен",
        "server": "IP или домен сервера, где развёрнут",
        "url": "адрес, по которому открывается (с портом)",
        "deploy_path": "путь на сервере"
      },
      "how_it_works": "Как устроен: основные части, как они связаны, откуда берёт данные, 3–6 предложений",
      "stack": ["язык", "фреймворк", "БД", "сервисы"],
      "run_and_deploy": {
        "run": "команда запуска",
        "update": "команда обновления на сервере",
        "logs": "где смотреть логи"
      },
      "integrations": ["внешние сервисы и API, к которым подключён"],
      "secrets_stored_in": "где лежат ключи/пароли (без значений)",
      "health_check": "как проверить, что работает (URL / команда / что должно ответить)",
      "links": ["полезные ссылки: PR, артефакты, документация"],
      "notes": "всё важное, что не влезло в поля выше"
    }
  ]
}"""

_TEXT_FIELDS = ("name", "summary", "stage", "current_focus", "how_it_works", "secrets_stored_in",
                "health_check", "notes")
_LIST_FIELDS = ("next_steps", "blockers", "stack", "integrations", "links")
_LOCATION_FIELDS = ("repo", "branch", "local_path", "server", "url", "deploy_path")
_RUN_FIELDS = ("run", "update", "logs")

# Секреты, которые чат мог всё-таки вставить, — вырезаются при импорте.
_SECRET_PATTERNS = [
    re.compile(r"(?i)((?:password|passwd|pass|пароль|token|токен|api[_ -]?key|secret|секрет|ключ)"
               r"\s*(?:[:=]|—|-|это)\s*)([^\s,;\"'()]{4,})"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}"
               r"|xox[abp]-[A-Za-z0-9-]{10,}|\d{8,10}:[A-Za-z0-9_-]{30,}|AKIA[0-9A-Z]{16})\b"),
    re.compile(r"(?i)(://[^/\s:@]+:)([^@\s/]+)(@)"),  # логин:пароль в URL
]
_SAFE_SECRET_WORDS = {"null", "none", "нет", "в", "хранится", "хранятся", "env", ".env"}
_lock = threading.Lock()
_URL = re.compile(r"https?://[^\s\"'<>«»,]+")
_SYSTEM_DIRS = ("/opt", "/etc", "/var", "/root", "/home", "/usr", "/tmp", "/dev", "/proc", "/sys", "/bin", "/srv", "/mnt")
_LOCAL = re.compile(r"://(?:localhost|127\.|0\.0\.0\.0|10\.0\.2\.2|\[::1\])")

_TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                     ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r",
                      "s", "t", "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


def slugify(text: str) -> str:
    text = "".join(_TRANSLIT.get(ch, ch) for ch in (text or "").lower())
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:60] or "project"


def _redact(value: str) -> tuple[str, int]:
    count = 0

    def keep_or_hide(m: re.Match) -> str:
        nonlocal count
        if m.lastindex and m.lastindex >= 2:
            secret = m.group(2)
            if secret.lower().strip(".") in _SAFE_SECRET_WORDS or secret.startswith(("«", "/", "~", "[", "$", "<")):
                return m.group(0)
            count += 1
            return m.group(1) + "[скрыто]" + (m.group(3) if m.lastindex >= 3 else "")
        count += 1
        return "[скрыто]"

    for pattern in _SECRET_PATTERNS:
        value = pattern.sub(keep_or_hide, value)
    return value, count


def _text(value: Any, limit: int = 2000) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    value = str(value).strip()
    return value[:limit] if value and value.lower() not in {"null", "none", "n/a"} else None


def _list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [t for t in (_text(v, 500) for v in value) if t][:30]


def _date(value: Any) -> str | None:
    text = _text(value, 40)
    if not text:
        return None
    m = re.search(r"\d{4}-\d{2}-\d{2}", text)
    return m.group(0) if m else None


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """Приводит проект из сводки к единому виду (типы, статус, прогресс, id)."""
    p: dict[str, Any] = {f: _text(raw.get(f)) for f in _TEXT_FIELDS}
    p["name"] = p["name"] or _text(raw.get("id")) or "Без названия"
    p["id"] = slugify(_text(raw.get("id")) or p["name"])
    status = (_text(raw.get("status"), 40) or "").lower().replace("-", "_").replace(" ", "_")
    status = status if status in STATUSES else STATUS_ALIASES.get(status.replace("_", " "), "in_progress")
    p["status"] = status
    try:
        progress = int(float(str(raw.get("progress_percent", raw.get("progress"))).strip(" %")))
        p["progress"] = max(0, min(100, progress))
    except (TypeError, ValueError):
        p["progress"] = 100 if status == "done" else None
    for f in _LIST_FIELDS:
        p[f] = _list(raw.get(f))
    location = raw.get("location") if isinstance(raw.get("location"), dict) else {}
    p["location"] = {f: _text(location.get(f), 500) for f in _LOCATION_FIELDS}
    run = raw.get("run_and_deploy") if isinstance(raw.get("run_and_deploy"), dict) else {}
    p["run_and_deploy"] = {f: _text(run.get(f), 1000) for f in _RUN_FIELDS}
    changes = []
    for item in raw.get("recent_changes") or []:
        if isinstance(item, dict) and _text(item.get("change")):
            changes.append({"date": _date(item.get("date")), "change": _text(item.get("change"), 500)})
        elif isinstance(item, str) and item.strip():
            changes.append({"date": None, "change": item.strip()[:500]})
    p["recent_changes"] = changes
    return p


def _scrub(obj: Any) -> tuple[Any, int]:
    if isinstance(obj, str):
        return _redact(obj)
    if isinstance(obj, list):
        out, total = [], 0
        for v in obj:
            v, n = _scrub(v)
            out.append(v)
            total += n
        return out, total
    if isinstance(obj, dict):
        out, total = {}, 0
        for k, v in obj.items():
            v, n = _scrub(v)
            out[k] = v
            total += n
        return out, total
    return obj, 0


def parse_reports(text: str) -> list[dict[str, Any]]:
    """Находит в тексте JSON-сводки (можно вставить несколько подряд, в ```json``` или без)
    и возвращает список сводок вида {"source_chat", "generated_at", "projects": [...]}."""
    text = (text or "").strip()
    decoder = json.JSONDecoder()
    found: list[Any] = []
    i = 0
    while i < len(text):
        start = min((j for j in (text.find("{", i), text.find("[", i)) if j >= 0), default=-1)
        if start < 0:
            break
        try:
            obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            i = start + 1
            continue
        found.append(obj)
        i = end
    reports = []
    for obj in found:
        if isinstance(obj, dict) and isinstance(obj.get("projects"), list):
            reports.append(obj)
        elif isinstance(obj, list) and any(isinstance(x, dict) and x.get("name") for x in obj):
            reports.append({"projects": obj})
        elif isinstance(obj, dict) and obj.get("name"):
            reports.append({"projects": [obj]})
    if not reports:
        raise ValueError("Не нашёл JSON-сводку. Вставь ответ чата целиком — блок, который начинается с «{».")
    return reports


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Projects:
    def __init__(self, storage: Storage):
        self.path = Path(storage.data_dir) / "projects.json"

    # ---------------------------------------------------------------- хранение

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data.get("projects"), dict) else {"projects": {}}
        except (OSError, ValueError, AttributeError):
            return {"projects": {}}

    def _save(self, data: dict[str, Any]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def get(self, project_id: str) -> dict[str, Any] | None:
        return self._load()["projects"].get(project_id)

    def all(self) -> list[dict[str, Any]]:
        order = list(STATUSES)
        items = list(self._load()["projects"].values())
        items.sort(key=lambda p: p.get("updated_at") or "", reverse=True)
        items.sort(key=lambda p: order.index(p["status"]) if p.get("status") in order else len(order))
        for p in items:
            p["stale"] = _days_since(p.get("report_date") or p.get("updated_at")) > STALE_DAYS
        return items

    def delete(self, project_id: str) -> bool:
        with _lock:
            data = self._load()
            removed = data["projects"].pop(project_id, None)
            self._save(data)
        return removed is not None

    # ---------------------------------------------------------------- импорт

    def import_text(self, text: str, replace_id: str | None = None) -> dict[str, Any]:
        """Сливает сводки в хранилище. replace_id — правка одного проекта (поля заменяются целиком)."""
        reports = parse_reports(text)
        result: dict[str, Any] = {"added": [], "updated": [], "redacted": 0}
        with _lock:
            data = self._load()
            for report in reports:
                report, redacted = _scrub(report)
                result["redacted"] += redacted
                source = _text(report.get("source_chat"), 200)
                report_date = _date(report.get("generated_at")) or date.today().isoformat()
                for raw in report["projects"]:
                    if not isinstance(raw, dict):
                        continue
                    project = normalize(raw)
                    if replace_id:
                        project["id"] = replace_id
                    old = data["projects"].get(project["id"])
                    merged = self._merge(old, project, replace=bool(replace_id))
                    merged["source_chat"] = source or (old or {}).get("source_chat")
                    merged["report_date"] = report_date
                    data["projects"][project["id"]] = merged
                    result["updated" if old else "added"].append(merged["name"])
                    if replace_id:
                        break
                if replace_id:
                    break
            self._save(data)
        return result

    @staticmethod
    def _merge(old: dict[str, Any] | None, new: dict[str, Any], replace: bool) -> dict[str, Any]:
        now = _now()
        if not old:
            return {**new, "created_at": now, "updated_at": now, "health": None,
                    "history": [{"at": now, "status": new["status"], "progress": new["progress"], "stage": new["stage"]}]}
        merged = dict(old)
        for key, value in new.items():
            if key == "recent_changes":
                continue
            if replace or value not in (None, [], {}) and not (isinstance(value, dict) and not any(value.values())):
                if isinstance(value, dict) and not replace:  # location / run_and_deploy: дополняем по полям
                    value = {**(old.get(key) or {}), **{k: v for k, v in value.items() if v}}
                merged[key] = value
        seen = set()
        changes = []
        source = new["recent_changes"] if replace else new["recent_changes"] + (old.get("recent_changes") or [])
        for c in source:
            key = (c.get("date"), c["change"].lower())
            if key not in seen:
                seen.add(key)
                changes.append(c)
        changes.sort(key=lambda c: c.get("date") or "", reverse=True)
        merged["recent_changes"] = changes[:MAX_CHANGES]
        history = list(old.get("history") or [])
        point = {"status": merged["status"], "progress": merged.get("progress"), "stage": merged.get("stage")}
        if not history or {k: history[-1].get(k) for k in point} != point:
            history.append({"at": now, **point})
        merged["history"] = history[-MAX_HISTORY:]
        merged["updated_at"] = now
        return merged

    # ---------------------------------------------------------------- проверка сайтов

    @staticmethod
    def health_url(project: dict[str, Any]) -> str | None:
        """Адрес для проверки: полный URL из health_check, иначе путь из него («GET /api/health»)
        на адресе проекта, иначе сам адрес проекта. Локальные адреса (localhost и т.п.) пропускаются."""
        check = project.get("health_check") or ""
        urls = [u for u in _URL.findall(check) if not _LOCAL.search(u)]
        if urls:
            return urls[0].rstrip(".,;)")
        base = next((u.rstrip(".,;)") for u in _URL.findall((project.get("location") or {}).get("url") or "")
                     if not _LOCAL.search(u)), None)
        if not base:
            return None
        paths = [m for m in re.findall(r"(?:^|[\s(«\"';])(/[\w./-]*\w)", check) if not m.startswith(_SYSTEM_DIRS)]
        paths.sort(key=lambda m: not m.startswith(("/api", "/health", "/status")))
        if paths:
            return re.match(r"https?://[^/]+", base).group(0) + paths[0]
        return base

    def check_health(self, timeout: float = 8.0) -> list[tuple[dict[str, Any], bool]]:
        """Проверяет сайты проектов. Возвращает [(проект, ok)] для тех, у кого состояние изменилось."""
        targets = {p["id"]: url for p in self._load()["projects"].values() if (url := self.health_url(p))}

        def probe(url: str) -> dict[str, Any]:
            started = time.monotonic()
            try:
                r = httpx.get(url, timeout=timeout, follow_redirects=True, verify=False)
                ok = r.status_code < 500
                out = {"ok": ok, "code": r.status_code, "error": None if ok else f"HTTP {r.status_code}"}
            except httpx.HTTPError as exc:
                out = {"ok": False, "code": None, "error": type(exc).__name__ + (f": {exc}" if str(exc) else "")}
            return {**out, "url": url, "ms": int((time.monotonic() - started) * 1000), "at": _now()}

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = dict(zip(targets, pool.map(probe, targets.values())))
        changed = []
        with _lock:
            data = self._load()
            for pid, health in results.items():
                project = data["projects"].get(pid)
                if not project:
                    continue
                before = (project.get("health") or {}).get("ok")
                if before is not None and before != health["ok"]:
                    changed.append((project, health["ok"]))
                if health["ok"] or before is False:  # время, с которого лежит, не сбрасываем
                    health["down_since"] = None if health["ok"] else (project.get("health") or {}).get("down_since")
                else:
                    health["down_since"] = health["at"]
                project["health"] = health
            self._save(data)
        return changed

    # ---------------------------------------------------------------- для ИИ

    def prompt_block(self, limit: int = 1500) -> str:
        lines = []
        for p in self.all():
            if p["status"] in {"done", "abandoned"}:
                continue
            label = STATUSES[p["status"]][0]
            progress = f", {p['progress']}%" if p.get("progress") is not None else ""
            line = f"- {p['name']} [{label}{progress}]"
            if p.get("stage"):
                line += f": {p['stage']}"
            if p.get("next_steps"):
                line += f"; дальше: {p['next_steps'][0]}"
            if p.get("blockers"):
                line += f"; мешает: {p['blockers'][0]}"
            if (p.get("health") or {}).get("ok") is False:
                line += "; сайт не отвечает"
            lines.append(line)
        if not lines:
            return ""
        text = "\n".join(lines)
        if len(text) > limit:
            text = text[:limit].rsplit("\n", 1)[0]
        return "Проекты пользователя (учитывай при планировании, но не выдумывай задачи сверх этого):\n" + text


def _days_since(stamp: str | None) -> float:
    if not stamp:
        return 0
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return 0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - moment).total_seconds() / 86400


def health_message(changes: list[tuple[dict[str, Any], bool]]) -> str:
    lines = []
    for project, ok in changes:
        h = project.get("health") or {}
        if ok:
            lines.append(f"✅ {project['name']} снова отвечает ({h.get('url')})")
        else:
            lines.append(f"🔴 {project['name']} не отвечает: {h.get('error') or 'нет ответа'} ({h.get('url')})")
    return "Проекты:\n" + "\n".join(lines) if lines else ""
