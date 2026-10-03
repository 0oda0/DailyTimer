"""Память ассистента: факты о пользователе, которые ИИ учитывает в планах дня и в чате.

Хранится в обычном файле data/memory.md — его можно читать и править руками (на сайте или на сервере).
Формат: по факту на строку «- …»; строки с «#» — заголовки, остальное игнорируется.
"""

from __future__ import annotations

import re
import threading
from datetime import date
from pathlib import Path

from .storage import Storage

HEADER = """# Память DailyTimer
# Что ассистент знает о тебе. По факту на строку, начиная с «- ».
# Эти факты подмешиваются в план дня и в чат с ИИ.
"""
MAX_FACTS = 200
_lock = threading.Lock()


def _normalize(fact: str) -> str:
    fact = re.sub(r"\s+", " ", fact).strip(" -•.\t")
    return fact[:1].upper() + fact[1:] if fact else ""


class Memory:
    def __init__(self, storage: Storage):
        self.path = Path(storage.data_dir) / "memory.md"

    def raw(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8")
        except OSError:
            return HEADER

    def save_raw(self, text: str) -> None:
        with _lock:
            self.path.write_text(text.replace("\r\n", "\n").rstrip() + "\n", encoding="utf-8")

    def facts(self) -> list[str]:
        out = []
        for line in self.raw().splitlines():
            line = line.strip()
            if line.startswith(("- ", "* ", "• ")):
                fact = _normalize(re.sub(r"\s*<!--.*?-->\s*$", "", line[2:]))
                if fact:
                    out.append(fact)
        return out

    def add(self, fact: str, source: str = "") -> bool:
        """Добавляет факт (без дублей). True — если добавлен."""
        fact = _normalize(fact)
        if len(fact) < 3:
            return False
        existing = {f.lower() for f in self.facts()}
        if fact.lower() in existing:
            return False
        with _lock:
            text = self.raw().rstrip("\n")
            note = f"  <!-- {date.today().isoformat()}{', ' + source if source else ''} -->"
            lines = text.splitlines() + [f"- {fact}{note}"]
            facts_idx = [i for i, l in enumerate(lines) if l.strip().startswith(("- ", "* ", "• "))]
            drop = set(facts_idx[:-MAX_FACTS]) if len(facts_idx) > MAX_FACTS else set()  # старые вытесняются
            self.path.write_text("\n".join(l for i, l in enumerate(lines) if i not in drop) + "\n",
                                 encoding="utf-8")
        return True

    def remove(self, query: str) -> list[str]:
        """Удаляет факты, содержащие текст (или по номеру с 1). Возвращает удалённые."""
        query = query.strip().strip("«»\"'").lower()
        if not query:
            return []
        facts = self.facts()
        if query.isdigit() and 1 <= int(query) <= len(facts):
            targets = {facts[int(query) - 1].lower()}
        else:
            targets = {f.lower() for f in facts if query in f.lower()}
        if not targets:
            return []
        removed = []
        with _lock:
            kept = []
            for line in self.raw().splitlines():
                stripped = line.strip()
                if stripped.startswith(("- ", "* ", "• ")):
                    fact = _normalize(re.sub(r"\s*<!--.*?-->\s*$", "", stripped[2:]))
                    if fact.lower() in targets:
                        removed.append(fact)
                        continue
                kept.append(line)
            self.path.write_text("\n".join(kept).rstrip() + "\n", encoding="utf-8")
        return removed

    def prompt_block(self, limit: int = 2000) -> str:
        facts = self.facts()
        if not facts:
            return ""
        text = "\n".join(f"- {f}" for f in facts)
        if len(text) > limit:  # свежие факты важнее — берём с конца
            text = text[-limit:].split("\n", 1)[-1]
        return "Что известно о пользователе (память, учитывай в планах и ответах):\n" + text
